"""Benchmark ownership, environment, cleanup and report safety, without real resources."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from aegisctl.container_resources import ProjectInventory

from scripts.benchmarks import http_workload, report, resources, run

REPOSITORY = Path(__file__).resolve().parents[2]
PROFILE = {
    "version": 1,
    "package": "2A.1",
    "seed": 20260914,
    "entries": 1000000,
    "wideFolderChildren": 50000,
    "users": 10,
    "warmupSeconds": 300,
    "measurementSeconds": 900,
    "pageSize": 100,
    "mix": {"directoryPages": 60, "alternateSorts": 15, "details": 10,
            "basicFilters": 10, "indexStatus": 5},
    "wideFolderListShareMinimum": 0.25,
    "coldRestartRecordedSeparately": True,
    "workerLoad": "none_for_catalog_seed_mode",
    "referenceHost": {"cpuClass": "x86_64_Intel_N100", "cores": 4, "ramGiB": 16},
    "memoryLimitsMiB": {"postgres": 3072, "web": 1024, "operations": 1024,
                        "indexer": 1024, "media": 2048, "gateway": 256},
    "storageCalibration": {"databaseRandomReadIops": 20000,
                           "databaseSequentialMBps": 300,
                           "originalSequentialMBps": 150,
                           "originalMetadataOpsPerSecond": 150},
    "referenceCertification": "requires_calibrated_reference_host",
}
TOKEN = "0123456789ab"
PROJECT = f"aegis-bench-{TOKEN}"


def private_directory() -> Path:
    return Path(tempfile.mkdtemp(prefix="aegis-phase2a-reports.", dir="/tmp")).resolve()


def test_versioned_profile_is_exactly_the_planned_reference_workload() -> None:
    assert resources.load_profile() == PROFILE
    raw = json.loads(resources.PROFILE_PATH.read_text(encoding="utf-8"))
    assert raw == PROFILE


@pytest.mark.parametrize("change", [
    {"version": 2}, {"surprise": 1}, {"users": 0}, {"pageSize": 251},
    {"mix": {"directoryPages": 61, "alternateSorts": 15, "details": 10,
             "basicFilters": 10, "indexStatus": 5}},
    {"wideFolderListShareMinimum": 0.1},
])
def test_profile_validation_refuses_drift(tmp_path: Path, change: dict[str, Any]) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(PROFILE | change), encoding="utf-8")
    with pytest.raises(resources.BenchmarkResourceError, match="profile"):
        resources.load_profile(path)


def test_report_directory_must_be_fresh_owned_private_and_outside_roots() -> None:
    accepted = private_directory()
    try:
        assert resources.validate_report_dir(str(accepted)) == accepted
        nested = accepted / "nested"
        nested.mkdir(mode=0o700)
        with pytest.raises(resources.BenchmarkResourceError, match="report directory"):
            resources.validate_report_dir(accepted)  # not empty
        nested.rmdir()
        accepted.chmod(0o755)
        with pytest.raises(resources.BenchmarkResourceError, match="report directory"):
            resources.validate_report_dir(accepted)
        accepted.chmod(0o700)
        link = accepted.with_name(accepted.name + "-link")
        link.symlink_to(accepted)
        try:
            with pytest.raises(resources.BenchmarkResourceError, match="report directory"):
                resources.validate_report_dir(link)
        finally:
            link.unlink()
        with pytest.raises(resources.BenchmarkResourceError, match="report directory"):
            resources.validate_report_dir(accepted, forbidden=(accepted.parent,))
    finally:
        accepted.rmdir()
    for value in ("/", "relative/path", str(REPOSITORY), str(REPOSITORY / "scripts"),
                  "/tmp/aegis-phase2a-reports.missing"):
        with pytest.raises(resources.BenchmarkResourceError, match="report directory"):
            resources.validate_report_dir(value)


def test_benchmark_environment_ignores_inherited_production_and_database_settings() -> None:
    inherited = {
        "PATH": "/usr/bin", "HOME": "/home/runner",
        "AEGIS_ENV": "production", "DJANGO_SETTINGS_MODULE": "aegis.settings.production",
        "AEGIS_DB_HOST": "operator-db", "AEGIS_DB_PASSWORD": "operator-secret",
        "DATABASE_URL": "postgres://operator@db/aegis", "PGHOST": "operator-db",
        "PGPASSWORD": "operator-secret", "DOCKER_HOST": "tcp://remote:2375",
        "COMPOSE_FILE": "/srv/operator/compose.yaml", "COMPOSE_PROJECT_NAME": "aegis",
        "AEGIS_MOUNT_MANIFEST": "/srv/operator/manifest.json",
    }
    work = Path("/tmp/aegis-bench.abcdefgh")
    env = resources.benchmark_environment_variables(
        inherited, work=work, project=PROJECT, http_port=40001, db_port=40002,
        database=f"aegis_bench_{TOKEN}",
    )
    serialized = json.dumps(env)
    assert "operator" not in serialized and "remote" not in serialized
    assert "DATABASE_URL" not in env and "PGHOST" not in env and "DOCKER_HOST" not in env
    assert env["AEGIS_ENV"] == "test"
    assert env["DJANGO_SETTINGS_MODULE"] == "aegis.settings.test"
    assert env["AEGIS_HTTP_PORT"] == "127.0.0.1:40001"
    assert env["AEGIS_PUBLIC_URL"] == "http://127.0.0.1:40001"
    assert env["AEGIS_TEST_SECRET_DIR"] == str(work / "secrets")
    assert env["AEGIS_DB_NAME"] == f"aegis_bench_{TOKEN}"
    assert env["PATH"] == "/usr/bin"


def test_runner_never_accepts_an_arbitrary_database_or_project() -> None:
    for extra in (["--database-url", "postgres://x/y"], ["--project", "aegis"],
                  ["--db-host", "x"]):
        with pytest.raises(SystemExit):
            run.parse_arguments(["catalog", "--report-dir", "/tmp/x", *extra])
    parsed = run.parse_arguments(
        ["catalog", "--entries", "1000000", "--wide-folder", "50000", "--report-dir", "/tmp/x"],
    )
    assert (parsed.entries, parsed.wide_folder) == (1000000, 50000)
    for value in ("aegis", "aegis-phase1-e2e", "aegis-bench-", "aegis-bench-XYZ",
                  "aegis-bench-0123456789ab-extra"):
        with pytest.raises(resources.BenchmarkResourceError, match="project"):
            resources.project_name(value.removeprefix("aegis-bench-")
                                   if value.startswith("aegis-bench-") else value)
    assert resources.project_name(TOKEN) == PROJECT


def compose_config() -> dict[str, Any]:
    limits = PROFILE["memoryLimitsMiB"]
    services: dict[str, Any] = {}
    for role in ("postgres", "migrate", "web", "operations", "indexer", "media", "gateway"):
        service: dict[str, Any] = {
            "read_only": True, "cap_drop": ["ALL"], "networks": {"backend": None},
            "volumes": [],
        }
        if role in limits:
            service["mem_limit"] = str(limits[role] * 1024 * 1024)
            service["cpuset"] = "0-3"
        if role in ("indexer", "operations", "media", "gateway"):
            service["volumes"] = [{"type": "bind", "source": "/tmp/aegis-bench.x/roots/a",
                                   "target": "/srv/aegis/roots/bench-main",
                                   "read_only": True}]
        services[role] = service
    services["gateway"]["networks"] = {"backend": None, "edge": None, "tls-hop": None}
    services["gateway"]["ports"] = [{"host_ip": "127.0.0.1", "published": "40001"}]
    services["postgres"]["ports"] = [{"host_ip": "127.0.0.1", "published": "40002"}]
    services["postgres"]["volumes"] = [{"type": "volume", "source": "postgres-data",
                                        "target": "/var/lib/postgresql"}]
    return {
        "name": PROJECT,
        "services": services,
        "networks": {"backend": {"internal": True}},
        "volumes": {"postgres-data": {"name": f"{PROJECT}_postgres-data"}},
    }


def test_compose_gate_accepts_the_owned_stack() -> None:
    resources.check_compose_config(compose_config(), project=PROJECT, profile=PROFILE)


def test_compose_gate_accepts_only_the_selected_cpu_placement() -> None:
    config = compose_config()
    for service in config["services"].values():
        if service.pop("cpuset", None) is not None:
            service["cpus"] = 4
    resources.check_compose_config(config, project=PROJECT, profile=PROFILE, cpu=("cpus", 4.0))
    with pytest.raises(resources.BenchmarkResourceError, match="CPU"):
        resources.check_compose_config(config, project=PROJECT, profile=PROFILE)
    del config["services"]["web"]["cpus"]
    with pytest.raises(resources.BenchmarkResourceError, match="CPU"):
        resources.check_compose_config(config, project=PROJECT, profile=PROFILE,
                                       cpu=("cpus", 4.0))


@pytest.mark.parametrize("drift", [
    "operator-volume", "external-volume", "public-port", "writable-root", "missing-limit",
    "wrong-project", "web-originals",
])
def test_compose_gate_refuses_operator_or_unowned_resources(drift: str) -> None:
    config = compose_config()
    if drift == "operator-volume":
        config["volumes"]["postgres-data"]["name"] = "aegis_postgres-data"
    elif drift == "external-volume":
        config["volumes"]["postgres-data"]["external"] = True
    elif drift == "public-port":
        config["services"]["gateway"]["ports"][0]["host_ip"] = "0.0.0.0"
    elif drift == "writable-root":
        config["services"]["indexer"]["volumes"][0]["read_only"] = False
    elif drift == "missing-limit":
        del config["services"]["postgres"]["mem_limit"]
    elif drift == "wrong-project":
        config["name"] = "aegis"
    else:
        config["services"]["web"]["volumes"] = config["services"]["indexer"]["volumes"]
    with pytest.raises(resources.BenchmarkResourceError):
        resources.check_compose_config(config, project=PROJECT, profile=PROFILE)


class FakeEngine:
    """A tiny Docker CLI double holding containers, networks and volumes by label."""

    def __init__(self) -> None:
        self.resources: dict[str, dict[str, Any]] = {}
        self.removed: list[str] = []
        self.ignore_removal = False

    def add(self, kind: str, handle: str, *, project: str = PROJECT,
            name: str | None = None, created: str = "2026-10-01T00:00:00Z") -> None:
        labels = {"com.docker.compose.project": project}
        if kind == "container":
            info = {"Id": handle, "Created": created, "Name": f"/{name or handle}",
                    "Image": "sha256:abc", "Config": {"Labels": labels}}
        elif kind == "network":
            info = {"Id": handle, "Name": name or handle, "Created": created,
                    "Driver": "bridge", "Internal": True, "Labels": labels,
                    "Scope": "local"}
        else:
            info = {"Name": name or handle, "CreatedAt": created, "Driver": "local",
                    "Mountpoint": f"/var/lib/docker/volumes/{handle}", "Options": {},
                    "Scope": "local", "Labels": labels}
        self.resources[handle] = {"kind": kind, "info": info, "project": project}

    def __call__(self, arguments: list[str], environment: Mapping[str, str],
                 ) -> subprocess.CompletedProcess[str]:
        del environment
        command = arguments[1:]
        output = ""
        if command[-2:-1] == ["--filter"] or "--filter" in command:
            kind = command[0]
            project = command[-1].split("=", 2)[2]
            output = "\n".join(
                handle for handle, item in self.resources.items()
                if item["kind"] == kind and item["project"] == project
            )
        elif command[1:2] == ["inspect"]:
            item = self.resources.get(command[2])
            if item is None:
                return subprocess.CompletedProcess(arguments, 1, "", "missing")
            output = json.dumps([item["info"]])
        elif command[:2] == ["rm", "--force"] or command[1:2] == ["rm"]:
            handle = command[-1]
            self.removed.append(handle)
            if not self.ignore_removal:
                match = [key for key, item in self.resources.items()
                         if key == handle or item["info"].get("Name") == handle]
                for key in match:
                    del self.resources[key]
        return subprocess.CompletedProcess(arguments, 0, output, "")


def owned_engine() -> FakeEngine:
    engine = FakeEngine()
    engine.add("container", "c" * 64, name=f"{PROJECT}-web-1")
    engine.add("network", "n" * 64, name=f"{PROJECT}_backend")
    engine.add("volume", f"{PROJECT}_postgres-data")
    return engine


def test_exact_resource_cleanup_removes_only_recorded_identities() -> None:
    engine = owned_engine()
    engine.add("container", "f" * 64, project="aegis", name="aegis-postgres-1")
    engine.add("volume", "aegis_postgres-data", project="aegis")
    inventory = resources.record_resources(PROJECT, {"AEGIS_CONTAINER_ENGINE": "docker"},
                                           runner=engine)
    assert len(inventory.resources) == 3
    resources.cleanup_resources(inventory, {"AEGIS_CONTAINER_ENGINE": "docker"}, runner=engine)
    assert "aegis_postgres-data" in engine.resources and "f" * 64 in engine.resources
    assert sorted(engine.removed) == sorted(["c" * 64, "n" * 64, f"{PROJECT}_postgres-data"])


def test_cleanup_refuses_replaced_or_unknown_resources_before_any_removal() -> None:
    engine = owned_engine()
    env = {"AEGIS_CONTAINER_ENGINE": "docker"}
    inventory = resources.record_resources(PROJECT, env, runner=engine)
    engine.add("container", "c" * 64, name=f"{PROJECT}-web-1", created="2026-10-02T00:00:00Z")
    with pytest.raises(Exception, match=r"changed|unknown"):
        resources.cleanup_resources(inventory, env, runner=engine)
    assert engine.removed == []
    engine = owned_engine()
    inventory = resources.record_resources(PROJECT, env, runner=engine)
    engine.add("volume", "d" * 12)
    with pytest.raises(Exception, match=r"changed|unknown"):
        resources.cleanup_resources(inventory, env, runner=engine)
    assert engine.removed == []


def test_operator_volume_and_foreign_projects_are_never_recordable() -> None:
    engine = FakeEngine()
    engine.add("volume", "aegis_postgres-data")
    with pytest.raises(resources.BenchmarkResourceError, match="operator"):
        resources.record_resources(PROJECT, {"AEGIS_CONTAINER_ENGINE": "docker"}, runner=engine)
    with pytest.raises(resources.BenchmarkResourceError, match="project"):
        resources.record_resources("aegis", {"AEGIS_CONTAINER_ENGINE": "docker"},
                                   runner=FakeEngine())
    with pytest.raises(resources.BenchmarkResourceError, match="project"):
        resources.cleanup_resources(ProjectInventory("aegis", ()),
                                    {"AEGIS_CONTAINER_ENGINE": "docker"}, runner=FakeEngine())


def test_incomplete_cleanup_is_reported_not_assumed() -> None:
    engine = owned_engine()
    env = {"AEGIS_CONTAINER_ENGINE": "docker"}
    inventory = resources.record_resources(PROJECT, env, runner=engine)
    engine.ignore_removal = True
    with pytest.raises(Exception, match="incomplete"):
        resources.cleanup_resources(inventory, env, runner=engine)


def test_workspace_cleanup_removes_only_recorded_entries_and_preserves_unknowns() -> None:
    workspace = resources.Workspace.create()
    try:
        workspace.directory("roots")
        workspace.secret("secrets-file", "generated-value")
        assert (workspace.path / "secrets-file").stat().st_mode & 0o777 == 0o600
        stranger = workspace.path / "roots" / "unknown"
        stranger.write_text("retain", encoding="ascii")
        with pytest.raises(resources.BenchmarkResourceError, match="unknown"):
            workspace.cleanup()
        assert stranger.read_text(encoding="ascii") == "retain"
        stranger.unlink()
        replaced = workspace.path / "secrets-file"
        replaced.unlink()
        replaced.write_text("other", encoding="ascii")
        with pytest.raises(resources.BenchmarkResourceError, match="changed"):
            workspace.cleanup()
        replaced.unlink()
        workspace.secret("secrets-file-2", "x")
    except BaseException:
        raise
    finally:
        if workspace.path.exists():
            for child in sorted(workspace.path.rglob("*"), key=lambda p: -len(p.parts)):
                child.rmdir() if child.is_dir() else child.unlink()
            workspace.path.rmdir()


def test_workspace_cleanup_succeeds_for_an_unchanged_inventory() -> None:
    workspace = resources.Workspace.create()
    workspace.directory("roots")
    workspace.directory("roots/bench-main", mode=0o755)
    workspace.secret("password", "generated")
    path = workspace.path
    workspace.cleanup()
    assert not path.exists()


def test_reports_redact_credentials_and_refuse_unredacted_secret_material() -> None:
    directory = private_directory()
    try:
        secret = "generated-password-value-123456"
        payload = {"note": f"login with {secret}", "cookie": "sessionid=abc",
                   "nested": [{"password": secret}]}
        written = report.write_report(directory, "summary.json", payload, secrets=(secret,))
        text = written.read_text(encoding="utf-8")
        assert secret not in text and "sessionid=abc" not in text
        assert written.stat().st_mode & 0o777 == 0o600
        with pytest.raises(ValueError, match="report name"):
            report.write_report(directory, "../escape.json", {}, secrets=())
        with pytest.raises(FileExistsError):
            report.write_report(directory, "summary.json", {}, secrets=())
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()


def test_query_plans_are_sanitized_of_literals_names_and_paths() -> None:
    plan = [{"Plan": {
        "Node Type": "Index Scan", "Relation Name": "catalog_catalogentry",
        "Index Name": "catalog_browse_name_asc", "Actual Rows": 101,
        "Index Cond": "((root_id = 'b4f1c1aa-0000-4000-8000-000000000001'::uuid) AND "
                      "(name_key >= '\\x494d475f'::bytea))",
        "Filter": "((size >= '1048576'::numeric) AND (display_name = 'Private Holiday.jpg'))",
        "Output": ["display_name", "'/srv/aegis/roots/personal'::text"],
        "Plans": [{"Node Type": "Seq Scan", "Relation Name": "identity_user",
                   "Filter": "(username = 'alice'::text)", "Shared Hit Blocks": 4}],
    }, "Planning Time": 0.2, "Execution Time": 1.5}]
    sanitized = report.sanitize_plan(plan)
    text = json.dumps(sanitized)
    for private in ("b4f1c1aa", "494d475f", "1048576", "Private Holiday", "/srv/aegis",
                    "alice", "Output"):
        assert private not in text
    assert sanitized[0]["Plan"]["Index Name"] == "catalog_browse_name_asc"
    assert sanitized[0]["Plan"]["Plans"][0]["Shared Hit Blocks"] == 4
    assert "Index Cond" in sanitized[0]["Plan"]


def test_request_mix_and_wide_folder_share_follow_the_profile() -> None:
    operations = list(http_workload.plan_operations(PROFILE, seed=20260914, worker=3,
                                                    count=40000))
    kinds = Counter(operation.kind for operation in operations)
    for kind, weight in PROFILE["mix"].items():
        assert abs(kinds[kind] / len(operations) - weight / 100) < 0.01
    lists = [operation for operation in operations if operation.is_list]
    assert sum(operation.wide for operation in lists) / len(lists) >= 0.25 + 0.05
    assert operations == list(http_workload.plan_operations(PROFILE, seed=20260914, worker=3,
                                                            count=40000))
    sorts = {(operation.sort, operation.order) for operation in operations
             if operation.kind == "alternateSorts"}
    assert len(sorts | {("name", "asc")}) == 6


def test_failures_count_as_failures_never_as_fast_successes() -> None:
    samples = [report.Sample("warm", "list", "list_wide", 200, 120.0, 1000) for _ in range(95)]
    samples += [report.Sample("warm", "list", "list_wide", 503, 1.0, 50) for _ in range(5)]
    samples += [report.Sample("warm", "list", "list_wide", 0, 2.0, 0)]
    summary = report.summarize(samples)
    route = summary["warm"]["routes"]["list"]
    assert route["requests"] == 101 and route["failures"] == 6
    assert route["errorRate"] == pytest.approx(6 / 101)
    assert route["p50Ms"] == pytest.approx(120.0) and route["p95Ms"] == pytest.approx(120.0)
    assert summary["warm"]["gate"]["passed"] is False
    assert os.environ.get("AEGIS_ENV") != "production"


class FakeApi:
    """Realistic cursor behaviour: first pages offer only next; travelled pages offer both."""

    def __init__(self, *, body: bytes | None = None) -> None:
        self.body = body
        self.counter = 0

    def request(self, method: str, path: str, *, query: dict[str, str] | None = None,
                **_kwargs: object) -> tuple[int, bytes, float]:
        del method
        self.counter += 1
        if self.body is not None:
            return 200, self.body, 1.0
        if path.endswith("/entries"):
            travelled = bool(query and "cursor" in query)
            payload = {"entries": [{"id": f"e{self.counter}-{index}"} for index in range(3)],
                       "nextCursor": f"n{self.counter}",
                       "previousCursor": f"p{self.counter}" if travelled else None}
        elif path.startswith("/api/v1/entries/"):
            payload = {"id": "x"}
        else:
            payload = {"state": "ready"}
        return 200, json.dumps(payload).encode(), 1.0


def make_worker(api: object, number: int = 1) -> http_workload.Worker:
    from scripts.benchmarks.dataset import DatasetShape, directory_targets

    targets = directory_targets(DatasetShape(entries=6000, wide_folder=1500, seed=1))
    wide = next(target for target in targets if target.label == "wide")
    roots = {"main": "r-main", "second": "r-second", "third": "r-third"}
    import random

    return http_workload.Worker(number, api, http_workload.accessible_targets(number, targets),
                                wide, roots, 100, random.Random(number))


def test_realized_navigation_mix_follows_the_planned_mix() -> None:
    worker = make_worker(FakeApi())
    for operation in http_workload.plan_operations(PROFILE, seed=20260914, worker=1,
                                                   count=20000):
        assert worker.execute(operation, "warm").status == 200
    mix = http_workload.mix_report([worker])
    for kind in ("directoryPages", "alternateSorts", "basicFilters"):
        planned_total = sum(value["planned"] for key, value in mix.items()
                            if key.startswith(f"{kind}:"))
        for travel in ("first", "next", "previous"):
            entry = mix.get(f"{kind}:{travel}", {"planned": 0, "realized": 0})
            assert abs(entry["realized"] - entry["planned"]) <= 0.03 * planned_total, (
                kind, travel, entry)
        assert mix[f"{kind}:next"]["realized"] > 0.3 * planned_total
    assert mix["directoryPages:previous"]["realized"] > 0
    assert mix["alternateSorts:previous"]["realized"] > 0


def test_navigation_uses_a_context_that_offers_the_cursor() -> None:
    api = FakeApi()
    worker = make_worker(api)
    first = http_workload.Operation("alternateSorts", True, "size", "desc", "first")
    other = http_workload.Operation("alternateSorts", False, "modified", "asc", "next")
    assert ":first" in worker.execute(first, "warm").klass
    sample = worker.execute(other, "warm")
    assert sample.klass.endswith(":wide:size-desc:next"), sample.klass
    back = worker.execute(http_workload.Operation("alternateSorts", True, "name", "desc",
                                                  "previous"), "warm")
    assert back.klass.endswith(":wide:size-desc:previous"), back.klass
    assert len(worker.listings["alternateSorts"]) <= http_workload.CONTEXTS_PER_KIND


def test_non_json_success_bodies_are_failures() -> None:
    for operation in (http_workload.Operation("directoryPages"),
                      http_workload.Operation("details"),
                      http_workload.Operation("indexStatus")):
        sample = make_worker(FakeApi(body=b"<html>login</html>")).execute(operation, "warm")
        assert sample.status == http_workload.INVALID_BODY and not sample.ok


def test_api_requests_never_follow_redirects() -> None:
    import http.server
    import threading

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/landing"):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"entries": []}')
            else:
                self.send_response(302)
                self.send_header("Location", "/landing")
                self.end_headers()

        def log_message(self, *_args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        session = http_workload.HttpSession(f"http://127.0.0.1:{server.server_address[1]}")
        status, _raw, _latency = session.request("GET", "/api/v1/roots/x/entries")
        assert status == 302
    finally:
        server.shutdown()
        server.server_close()


def test_wide_share_minimum_and_exit_code_are_enforced() -> None:
    samples = [report.Sample("warm", "list", "list:directoryPages:wide:name-asc:first", 200,
                             100.0, 10)] * 20
    phases = report.summarize(samples)
    run.enforce_wide_share(phases, 0.30, 0.25)
    assert phases["warm"]["gate"]["passed"] is True and run.gate_exit_code(phases) == 0
    run.enforce_wide_share(phases, 0.20, 0.25)
    assert phases["warm"]["gate"]["passed"] is False
    assert run.gate_exit_code(phases) == run.GATE_FAILED_EXIT
    assert run.gate_exit_code({}) == run.GATE_FAILED_EXIT


def test_bottleneck_evidence_labels_measurement_and_inference() -> None:
    samples = [report.Sample("warm", "list", "list:x", 200, 200.0, 10)] * 50
    samples += [report.Sample("sequential", "list", "sequential:list:wide", 200, 60.0, 10)] * 5
    phases = report.summarize(samples)
    utilization = {"phases": {"warm": {"web": {"cpuMeanPercentOfOneCore": 98.0},
                                       "postgres": {"cpuMeanPercentOfOneCore": 30.0}}}}
    evidence = run.bottleneck_evidence(phases, utilization, measurement=10)
    assert evidence["measured"]["webCpuMeanPercentOfOneCore"] == 98.0
    assert evidence["measured"]["webCpuMsPerRequest"] == pytest.approx(196.0)
    assert evidence["webAtLeastOneCore"] is True
    assert evidence["measured"]["concurrentToSequentialListP50"] == pytest.approx(200 / 60, 0.01)
    assert evidence["inferences"] and all(
        text.startswith("inferred:") for text in evidence["inferences"])
    assert any("serializing request CPU" in text for text in evidence["inferences"])
    idle = {"phases": {"warm": {"web": {"cpuMeanPercentOfOneCore": 40.0},
                                "postgres": {"cpuMeanPercentOfOneCore": 90.0}}}}
    idle_inferences = run.bottleneck_evidence(phases, idle, 10)["inferences"]
    assert any("not supported" in text for text in idle_inferences)
    assert any("new database connection" in text for text in idle_inferences)


def test_runtime_services_cannot_bind_repository_subdirectories() -> None:
    config = compose_config()
    config["services"]["web"]["volumes"] = [{"type": "bind",
                                            "source": str(REPOSITORY / "backend"),
                                            "target": "/app/backend"}]
    with pytest.raises(resources.BenchmarkResourceError, match="repository"):
        resources.check_compose_config(config, project=PROJECT, profile=PROFILE)


def storage_bench(df_output: str, rotational: str | None) -> resources.BenchmarkEnvironment:
    bench = resources.BenchmarkEnvironment(
        PROFILE, Path("/tmp"), resources.Workspace(Path("/tmp/x")), PROJECT, TOKEN,
        f"aegis_bench_{TOKEN}", {}, (), 1, 2,
    )
    bench.container_ids = lambda: {"postgres": "p" * 64}  # type: ignore[method-assign]

    def engine(*arguments: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
        del timeout
        script = arguments[-1]
        if "df -Pk" in script:
            return subprocess.CompletedProcess(list(arguments), 0, df_output, "")
        if rotational is None:
            return subprocess.CompletedProcess(list(arguments), 1, "", "")
        return subprocess.CompletedProcess(list(arguments), 0, rotational + "\n", "")

    bench.engine = engine  # type: ignore[method-assign]
    return bench


def test_storage_check_refuses_insufficient_capacity_and_records_the_medium() -> None:
    plenty = ("/dev/nvme0n1p3 976000000 400000000 500000000 45% /var/lib/postgresql\n"
              "SOURCE btrfs /dev/nvme0n1p3\n")
    record = storage_bench(plenty, "0").storage_check()
    assert record["sufficient"] is True and record["rotational"] == "no (SSD/NVMe)"
    assert record["freeBytes"] == 500000000 * 1024
    assert record["requiredFreeBytes"] == 12 * 1024**3
    unknown = storage_bench(plenty, None).storage_check()
    assert unknown["rotational"] == "unknown"
    scarce = "/dev/sda1 20000000 15000000 5000000 75% /var/lib/postgresql\n"
    with pytest.raises(resources.BenchmarkResourceError, match="insufficient"):
        storage_bench(scarce, "1").storage_check()
    with pytest.raises(resources.BenchmarkResourceError, match="unknown"):
        storage_bench("garbage\n", "0").storage_check()


def test_matrix_counts_non_json_success_bodies_as_failures() -> None:
    directory = private_directory()
    try:
        sink = http_workload.SampleSink(directory / "samples.csv")
        worker = make_worker(FakeApi(body=b"<html>not json</html>"))
        http_workload.run_matrix(worker.session, worker.wide, "r-main", sink, page_size=100,
                                 repetitions=1)
        samples = sink.close()
        assert samples and all(sample.status == http_workload.INVALID_BODY
                               for sample in samples)
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()


def full_summary() -> dict[str, Any]:
    warm_classes = {
        "list:basicFilters:wide:size-desc:next:filter": {"requests": 100, "p50Ms": 300.0,
                                                         "p95Ms": 435.0, "p99Ms": 460.0},
        "list:basicFilters:wide:name-asc:first:filter": {"requests": 120, "p50Ms": 280.0,
                                                         "p95Ms": 351.0, "p99Ms": 400.0},
        "list:directoryPages:wide:name-asc:first": {"requests": 600, "p50Ms": 200.0,
                                                    "p95Ms": 280.0, "p99Ms": 300.0},
        "list:alternateSorts:tree:size-asc:previous": {"requests": 10, "p50Ms": 250.0,
                                                       "p95Ms": 320.0, "p99Ms": 330.0},
        "details": {"requests": 50, "p50Ms": 260.0, "p95Ms": 330.0, "p99Ms": 350.0},
        "list:Private Holiday.jpg": {"requests": 99, "p95Ms": 999.0},
    }
    stats = {"requests": 1000, "failures": 0, "errorRate": 0.0, "p50Ms": 229.4,
             "p95Ms": 291.2, "p99Ms": 335.6, "meanPayloadBytes": 1.0, "meanRows": 96.0}
    return {
        "package": "2A.1", "mode": "catalog", "evidenceLabel": "provisional: test",
        "referenceCertification": "open: requires the calibrated N100 reference host",
        "unexpectedFutureField": "/srv/aegis/roots/personal",
        "clientModel": "closed loop, ten users; client on the same host",
        "workerLoad": "see /srv/aegis/roots/personal",
        "shape": {"entries": 1000000, "wideFolder": 50000, "seed": 20260914,
                  "rawName": "IMG_0001.jpg"},
        "storage": {"path": "/var/home/private", "filesystem": "btrfs", "freeBytes": 1,
                    "device": "/dev/mapper/luks-secret", "rotational": "no (SSD/NVMe)"},
        "phases": {"warm": {
            "gate": {"listP95Ms": 291.2, "thresholdP95Ms": 300.0, "failures": 0,
                     "passed": True, "routesOverThreshold": ["details", "/tmp/x"],
                     "wideFolderListShare": 0.39, "wideFolderListShareMinimum": 0.25},
            "routes": {"list": stats, "secretRoute": stats},
            "classes": warm_classes,
        }},
        "environment": {
            "host": {"cpuModel": "Test CPU", "hostname": "private-host"},
            "containers": {"web": {"containerId": "c" * 64, "ips": ["172.18.0.5"],
                                   "image": "sha256:" + "a" * 64, "memoryLimitBytes": 1}},
            "source": {"gitRevision": "0" * 40, "workingTreeChanges": 8,
                       "remoteUrl": "https://user:token@example.invalid/repo"},
        },
        "queryEvidence": {"requests": [{"label": "wide-name-asc-first", "status": 200,
                                        "sqlCount": 13, "payloadBytes": 25216, "rows": 100,
                                        "catalogPlans": [], "rawSql": "SELECT 'secret'"}],
                          "sizes": {"budgetBytes": 8, "indexes": [
                              {"index": "catalog_browse_name_asc", "bytes": 1, "scans": 2},
                              {"index": "/tmp/evil", "bytes": 1}]},
                          "postgresSettings": {"work_mem": "4096kB", "data_directory": "/x"}},
        "bottleneck": {"measured": {"webCpuMsPerRequest": 40.6},
                       "inferences": ["inferred: web averaged 163.9% of one core",
                                      "raw text with /srv/aegis path"]},
        "verification": {"rootGrantsMatch": True, "staleFixturesUnavailable": [
            ["inaccessible", "IMG_0001.jpg"]]},
    }


def test_tracked_summary_is_an_explicit_allowlist() -> None:
    tracked = report.verification_summary(full_summary())
    text = json.dumps(tracked)
    for private in ("/srv/aegis", "IMG_0001", "/var/home", "luks-secret", "c" * 64,
                    "172.18.0.5", "private-host", "token@", "SELECT", "Private Holiday",
                    "/tmp", "data_directory", "unexpectedFutureField", "secretRoute",
                    "raw text"):
        assert private not in text, private
    assert tracked["storage"] == {"filesystem": "btrfs", "freeBytes": 1,
                                  "rotational": "no (SSD/NVMe)"}
    assert tracked["gates"]["warm"]["routesOverThreshold"] == ["details"]
    assert tracked["clientModel"] == "closed loop, ten users; client on the same host"
    assert tracked["workerLoad"] is None
    assert tracked["sizes"]["indexes"] == [
        {"index": "catalog_browse_name_asc", "bytes": 1, "scans": 2}]
    assert tracked["bottleneck"]["inferences"] == ["inferred: web averaged 163.9% of one core"]


def test_tracked_markdown_reports_classes_over_threshold_and_the_gate_reading() -> None:
    tracked = report.verification_summary(full_summary())
    over = report.classes_over_threshold(tracked)
    assert [row["class"] for row in over["classes"]] == [
        "list:basicFilters:wide:size-desc:next:filter",
        "list:basicFilters:wide:name-asc:first:filter",
    ]
    assert over["requests"] == 220 and over["shareOfGated"] == pytest.approx(0.22)
    assert over["filteredWideP95RangeMs"] == [351.0, 435.0]
    markdown = report.verification_markdown(tracked)
    assert "## Classes over 300 ms (not individually gated)" in markdown
    assert "8.8 ms headroom" in markdown and "p99 335.6 ms" in markdown
    assert "351.0 to 435.0 ms" in markdown and "22.0% of the gated population" in markdown
    assert "lines 116 and 645" in markdown and "(line 175)" in markdown
    assert "line 177" in markdown
    assert "{'" not in markdown, "values render as text, not Python dict reprs"


def test_signing_key_source_survives_report_redaction_and_regeneration() -> None:
    directory = private_directory()
    try:
        report.write_report(directory, "summary.json", {"queryEvidence": {
            "signingKeySource": "generated protected key file"}}, secrets=())
        output = private_directory()
        try:
            json_path, markdown_path = report.regenerate_verification(directory, output)
            assert json.loads(json_path.read_text())["signingKeySource"] == (
                "generated protected key file")
            assert "Django signing key:** generated protected key file" in (
                markdown_path.read_text())
        finally:
            for child in output.iterdir():
                child.unlink()
            output.rmdir()
    finally:
        for child in directory.iterdir():
            child.unlink()
        directory.rmdir()
