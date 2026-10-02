"""Phase 2A.1 benchmark runner: ``python -m scripts.benchmarks.run catalog ...``.

The runner creates its own database and stack (see resources.py). It accepts no
database URL, host, project or volume: only a dataset shape and a fresh private
report directory, which is validated before anything else happens.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import random
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .dataset import (
    ADMIN_USERNAME,
    ROOTS,
    DatasetShape,
    dataset_counts,
    directory_targets,
    expected_children,
    username,
)
from .http_workload import (
    HttpSession,
    SampleSink,
    Worker,
    accessible_targets,
    mix_report,
    plan_operations,
    run_matrix,
    run_phase,
    run_sequential_baseline,
)
from .report import (
    markdown_summary,
    summarize,
    verification_markdown,
    verification_summary,
    write_report,
    write_text,
)
from .resources import (
    PROFILE_PATH,
    REPOSITORY,
    BenchmarkEnvironment,
    BenchmarkResourceError,
    benchmark_environment,
    load_profile,
    validate_report_dir,
)

COLD_OPERATIONS_PER_USER = 30
GATE_FAILED_EXIT = 3
HOLD_MARGIN_SECONDS = 2 * 86_400
REQUEST_SERVICES = ("postgres", "web", "gateway")


def parse_arguments(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m scripts.benchmarks.run",
                                     description=__doc__, allow_abbrev=False)
    modes = parser.add_subparsers(dest="mode", required=True)
    catalog = modes.add_parser("catalog", allow_abbrev=False,
                               help="seeded 1M-entry catalog, authenticated HTTP workload")
    catalog.add_argument("--entries", type=int)
    catalog.add_argument("--wide-folder", type=int)
    catalog.add_argument("--report-dir", required=True)
    catalog.add_argument("--smoke", action="store_true",
                         help="development run; labeled smoke and never completion evidence")
    catalog.add_argument("--warmup-seconds", type=int)
    catalog.add_argument("--measurement-seconds", type=int)
    catalog.add_argument("--cold-operations", type=int, default=COLD_OPERATIONS_PER_USER)
    return parser.parse_args(list(argv))


def log(message: str) -> None:
    stamp = datetime.now(UTC).strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _command(arguments: list[str]) -> str | None:
    try:
        result = subprocess.run(arguments, cwd=REPOSITORY, capture_output=True, text=True,
                                timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def host_identity() -> dict[str, Any]:
    model = None
    memory = None
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("model name"):
                model = line.split(":", 1)[1].strip()
                break
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                memory = int(line.split()[1]) * 1024
    except OSError:
        model = memory = None
    governor = None
    with contextlib.suppress(OSError):
        governor = Path("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read_text(
            encoding="ascii").strip()
    return {
        "cpuModel": model, "logicalCpus": os.cpu_count(), "memoryBytes": memory,
        "kernel": platform.release(), "machine": platform.machine(), "governor": governor,
        "python": platform.python_version(),
        "note": "load generator runs on the same host as the stack",
    }


def source_identity() -> dict[str, Any]:
    status = _command(["git", "status", "--porcelain"])
    return {
        "gitRevision": _command(["git", "rev-parse", "HEAD"]),
        "workingTreeChanges": None if status is None else len(status.splitlines()),
        "uvLockSha256": _sha256(REPOSITORY / "uv.lock"),
        "npmLockSha256": _sha256(REPOSITORY / "frontend/package-lock.json"),
        "imagesLockSha256": _sha256(REPOSITORY / "deploy/images.lock"),
        "profileSha256": _sha256(PROFILE_PATH),
    }


def _engine_json(bench: BenchmarkEnvironment, *arguments: str) -> Any:
    result = bench.engine(*arguments)
    try:
        return json.loads(result.stdout) if result.returncode == 0 else None
    except ValueError:
        return None


def engine_identity(bench: BenchmarkEnvironment) -> dict[str, Any]:
    version = _engine_json(bench, "version", "--format", "json") or {}
    info = _engine_json(bench, "info", "--format", "json") or {}
    server = version.get("Server") or {}
    return {
        "engine": "docker", "serverVersion": server.get("Version"),
        "apiVersion": server.get("ApiVersion"), "kernel": server.get("KernelVersion"),
        "os": info.get("OperatingSystem"), "ncpu": info.get("NCPU"),
        "memTotal": info.get("MemTotal"), "storageDriver": info.get("Driver"),
        "driverStatus": info.get("DriverStatus"), "cgroupVersion": info.get("CgroupVersion"),
        "storage": "engine-managed named volume on the host SSD (not tmpfs)",
        "storageCalibration": "not measured: uncalibrated development host",
    }


def container_identity(bench: BenchmarkEnvironment) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for service, identity in sorted(bench.container_ids().items()):
        info = (_engine_json(bench, "container", "inspect", identity) or [{}])[0]
        host = info.get("HostConfig") or {}
        result[service] = {
            "containerId": identity, "image": info.get("Image"),
            "imageReference": (info.get("Config") or {}).get("Image"),
            "memoryLimitBytes": host.get("Memory"), "memorySwapBytes": host.get("MemorySwap"),
            "cpusetCpus": host.get("CpusetCpus"), "nanoCpus": host.get("NanoCpus"),
            "cpuQuota": host.get("CpuQuota"), "readOnly": host.get("ReadonlyRootfs"),
            "ips": sorted(str(network.get("IPAddress")) for network in (
                (info.get("NetworkSettings") or {}).get("Networks") or {}).values()),
        }
    return result


def resource_snapshot(bench: BenchmarkEnvironment) -> dict[str, Any]:
    ids = bench.container_ids()
    result = bench.engine("stats", "--no-stream", "--format", "{{json .}}", *ids.values())
    by_id = {value: key for key, value in ids.items()}
    snapshot: dict[str, Any] = {}
    for line in result.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        for identity, service in by_id.items():
            if identity.startswith(str(row.get("ID", "-"))):
                snapshot[service] = {"cpu": row.get("CPUPerc"), "memory": row.get("MemUsage"),
                                     "memoryPercent": row.get("MemPerc")}
    return snapshot


_UNITS = {"B": 1, "kB": 1000, "KB": 1000, "KiB": 1024, "MB": 1000**2, "MiB": 1024**2,
          "GB": 1000**3, "GiB": 1024**3, "TB": 1000**4, "TiB": 1024**4}


def _percent(value: object) -> float | None:
    try:
        return float(str(value).rstrip("%"))
    except ValueError:
        return None


def _memory_bytes(value: object) -> int | None:
    text = str(value).split("/", 1)[0].strip()
    for unit in sorted(_UNITS, key=len, reverse=True):
        if text.endswith(unit):
            try:
                return int(float(text[: -len(unit)]) * _UNITS[unit])
            except ValueError:
                return None
    return None


class ResourceSampler:
    """Bounded background `docker stats` sampling of exact container IDs during phases.

    CPU is the engine's percentage of one CPU (100 = one full core) over each sample's
    window; memory is the container's current usage.
    """

    def __init__(self, bench: BenchmarkEnvironment, services: Sequence[str],
                 interval: float = 5.0, limit: int = 20_000) -> None:
        ids = bench.container_ids()
        self.bench = bench
        self.ids = {service: ids[service] for service in services}
        self.interval = interval
        self.limit = limit
        self.phase: str | None = None
        self.samples: list[tuple[str, str, float, int]] = []
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._loop, name="benchmark-stats", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            phase = self.phase
            if phase is not None:
                result = self.bench.engine("stats", "--no-stream", "--format", "{{json .}}",
                                           *self.ids.values(), timeout=60)
                for line in result.stdout.splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    for service, identity in self.ids.items():
                        if identity.startswith(str(row.get("ID", "-"))):
                            cpu, memory = _percent(row.get("CPUPerc")), _memory_bytes(
                                row.get("MemUsage"))
                            if cpu is not None and memory is not None and (
                                len(self.samples) < self.limit
                            ):
                                self.samples.append((phase, service, cpu, memory))
            self.stop_event.wait(max(0.0, self.interval - (time.monotonic() - started)))

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=120)

    def summary(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        groups: dict[tuple[str, str], list[tuple[float, int]]] = {}
        for phase, service, cpu, memory in self.samples:
            groups.setdefault((phase, service), []).append((cpu, memory))
        for (phase, service), values in sorted(groups.items()):
            cpus = sorted(value[0] for value in values)
            memories = [value[1] for value in values]
            result.setdefault(phase, {})[service] = {
                "samples": len(values),
                "cpuMeanPercentOfOneCore": round(sum(cpus) / len(cpus), 1),
                "cpuP95PercentOfOneCore": round(cpus[max(0, -(-95 * len(cpus) // 100) - 1)], 1),
                "cpuMaxPercentOfOneCore": round(cpus[-1], 1),
                "memoryMeanBytes": int(sum(memories) / len(memories)),
                "memoryMaxBytes": max(memories),
            }
        return {"intervalSeconds": self.interval, "method": "docker stats --no-stream by "
                "exact container ID on a bounded background thread", "phases": result}


def enforce_wide_share(phases: dict[str, Any], share: float, minimum: float) -> None:
    """The profile's wide-folder share of list traffic is part of the warm gate."""
    if "warm" not in phases:
        return
    gate = phases["warm"]["gate"]
    gate["wideFolderListShare"] = round(share, 4)
    gate["wideFolderListShareMinimum"] = minimum
    gate["passed"] = bool(gate["passed"] and share >= minimum)


def gate_exit_code(phases: dict[str, Any]) -> int:
    """Nonzero whenever the measured warm gate is not met (labeling is separate)."""
    return 0 if phases.get("warm", {}).get("gate", {}).get("passed") is True else (
        GATE_FAILED_EXIT)


def bottleneck_evidence(phases: dict[str, Any], utilization: dict[str, Any],
                        measurement: int) -> dict[str, Any]:
    """Measured utilization and service times; the causal attribution stays labeled."""
    warm = phases.get("warm", {})
    sequential = phases.get("sequential", {}).get("routes", {})
    requests = warm.get("overall", {}).get("requests", 0)
    rate = requests / measurement if measurement else 0.0
    usage = utilization.get("phases", {}).get("warm", {})
    web = usage.get("web", {}).get("cpuMeanPercentOfOneCore")
    postgres = usage.get("postgres", {}).get("cpuMeanPercentOfOneCore")

    def per_request(percent: float | None) -> float | None:
        return None if percent is None or not rate else round(percent * 10 / rate, 2)

    concurrent_p50 = {name: stats["p50Ms"] for name, stats in warm.get("routes", {}).items()}
    sequential_p50 = {name: stats["p50Ms"] for name, stats in sequential.items()}
    ratio = (round(concurrent_p50["list"] / sequential_p50["list"], 2)
             if concurrent_p50.get("list") and sequential_p50.get("list") else None)
    inferences: list[str] = []
    if ratio is not None:
        inferences.append(
            f"inferred: concurrent list p50 is {ratio}x the sequential service time, so most "
            "loaded latency is waiting for a shared resource rather than per-query work")
    if web is not None:
        if web > 110.0:
            inferences.append(
                f"inferred: web averaged {web}% of one core, above one core, so request work "
                "is not purely serialized by one Python GIL (C extensions release it); the "
                "single-process hypothesis is only partly supported")
        elif web >= 85.0:
            inferences.append(
                f"inferred: web averaged {web}% of one core with a four-core quota, consistent "
                "with the single uvicorn process serializing request CPU")
        else:
            inferences.append(
                f"inferred: web averaged {web}% of one core; the single-process bottleneck "
                "hypothesis is not supported by these samples")
    if web is not None and postgres is not None and postgres >= web:
        inferences.append(
            f"inferred: PostgreSQL used at least as much CPU as web ({postgres}% vs {web}% of "
            "one core). With no persistent connections, every request opens a new database "
            "connection (backend start and SCRAM login) plus a locking transaction; that is a "
            "plausible major cost, not profiled here")
    return {
        "measured": {
            "warmThroughputRequestsPerSecond": round(rate, 2),
            "webCpuMeanPercentOfOneCore": web, "postgresCpuMeanPercentOfOneCore": postgres,
            "webCpuMsPerRequest": per_request(web),
            "postgresCpuMsPerRequest": per_request(postgres),
            "webCpuQuotaCores": 4, "postgresCpuQuotaCores": 4,
            "concurrentP50Ms": concurrent_p50, "sequentialP50Ms": sequential_p50,
            "concurrentToSequentialListP50": ratio,
        },
        "inferences": inferences,
        "webAtLeastOneCore": web is not None and web >= 85.0,
    }


def _get(session: HttpSession, path: str, **query: str) -> tuple[int, dict[str, Any]]:
    status, raw, _latency = session.request("GET", path, query=query or None)
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        payload = {}
    return status, payload if isinstance(payload, dict) else {}


def verify_catalog(sessions: dict[int, HttpSession], admin: HttpSession,
                   root_ids: dict[str, str], shape: DatasetShape, page_size: int,
                   expected_wide: list[str], expected_wide_count: int) -> dict[str, Any]:
    """Real-HTTP membership/availability checks so a stale dataset cannot pass as fast."""
    targets = directory_targets(shape)
    wide = next(target for target in targets if target.label == "wide")
    main = root_ids["main"]
    reader = sessions[1]
    checks: dict[str, Any] = {}
    for number, session in sessions.items():
        status, payload = _get(session, "/api/v1/roots")
        granted = {root.key for root in ROOTS
                   if number in (*root.direct_users, *root.group_users)}
        visible = {item.get("id") for item in payload.get("roots", [])}
        if status != 200 or visible != {root_ids[key] for key in granted}:
            raise RuntimeError("benchmark root grants do not match the fixture")
    checks["rootGrantsMatch"] = True
    walked: list[dict[str, Any]] = []
    cursor = None
    while True:
        query = {"parent": str(wide.id), "limit": "250"}
        if cursor:
            query["cursor"] = cursor
        status, payload = _get(reader, f"/api/v1/roots/{main}/entries", **query)
        if status != 200:
            raise RuntimeError(f"benchmark wide-folder walk failed with HTTP {status}")
        walked.extend(payload["entries"])
        cursor = payload.get("nextCursor")
        if not cursor:
            break
    if len(walked) != expected_wide_count:
        raise RuntimeError("benchmark wide-folder membership does not match the generator")
    if [entry["id"] for entry in walked[:len(expected_wide)]] != expected_wide:
        raise RuntimeError("benchmark wide-folder order does not match the generator")
    present = sum(entry["sourceState"] == "present" for entry in walked)
    if present < 0.9 * len(walked):
        raise RuntimeError("benchmark dataset is unexpectedly unavailable")
    checks["wideFolder"] = {"walkedEntries": len(walked), "effectivePresent": present,
                            "orderVerifiedEntries": len(expected_wide)}
    stale = []
    for target in (target for target in targets if target.label == "stale"):
        status, payload = _get(reader, f"/api/v1/roots/{main}/entries", parent=str(target.id),
                               limit=str(page_size))
        states = {entry["sourceState"] for entry in payload.get("entries", [])}
        if status != 200 or not states or "present" in states or (
            payload["indexStatus"]["state"] != "unavailable"
        ):
            raise RuntimeError("benchmark stale-availability fixture is not effective")
        stale.append(sorted(states))
    checks["staleFixturesUnavailable"] = stale
    for key, root_id in root_ids.items():
        holder = next(number for number in sessions if number in (
            *next(root for root in ROOTS if root.key == key).direct_users,
            *next(root for root in ROOTS if root.key == key).group_users))
        status, payload = _get(sessions[holder], f"/api/v1/roots/{root_id}/index-status")
        if status != 200 or payload.get("state") != "ready":
            raise RuntimeError("benchmark index status is not ready")
    checks["indexStatusReady"] = True
    denied, _ = _get(reader, f"/api/v1/roots/{root_ids['second']}/entries")
    admin_roots_status, admin_roots = _get(admin, "/api/v1/roots")
    admin_main, _ = _get(admin, f"/api/v1/roots/{main}/entries")
    if denied != 404 or admin_roots_status != 200 or admin_roots.get("roots") != [] or (
        admin_main != 404
    ):
        raise RuntimeError("benchmark authorization boundary is not effective")
    checks["ungrantedAccessDenied"] = {"crossRoot": denied, "adminRootCount": 0,
                                      "adminBrowse": admin_main}
    return checks


def evidence_requests(root_ids: dict[str, str], shape: DatasetShape) -> list[dict[str, Any]]:
    from .http_workload import filter_parameter

    targets = directory_targets(shape)
    wide = next(target for target in targets if target.label == "wide")
    tree = next(target for target in targets if target.label == "tree" and target.root == "main")
    main = root_ids["main"]
    path = f"/api/v1/roots/{main}/entries"
    base = {"parent": str(wide.id), "limit": "100"}
    requests: list[dict[str, Any]] = [
        {"label": "wide-name-asc-first", "path": path, "query": base, "explain": True},
        {"label": "wide-name-asc-next", "path": path, "query": base,
         "follow": "wide-name-asc-first", "explain": True},
    ]
    for sort, order in (("name", "desc"), ("modified", "asc"), ("modified", "desc"),
                        ("size", "asc"), ("size", "desc")):
        label = f"wide-{sort}-{order}-first"
        requests.append({"label": label, "path": path,
                         "query": base | {"sort": sort, "order": order}, "explain": True})
        requests.append({"label": f"wide-{sort}-{order}-next", "path": path,
                         "query": base | {"sort": sort, "order": order}, "follow": label,
                         "explain": True})
    for case in ("type-common", "type-rare", "type-zero", "prefix-common", "prefix-zero",
                 "size-rare", "modified-common", "availability-missing", "all-common",
                 "all-zero"):
        requests.append({"label": f"wide-filter-{case}", "path": path,
                         "query": base | {"filters": filter_parameter(case)}, "explain": True})
    requests += [
        {"label": "tree-name-asc-first", "path": path,
         "query": {"parent": str(tree.id), "limit": "100"}, "explain": True},
        {"label": "details", "path": "details-from:wide-name-asc-first", "query": {},
         "explain": True},
        {"label": "index-status", "path": f"/api/v1/roots/{main}/index-status", "query": {},
         "explain": False},
    ]
    return requests


def collect_evidence(bench: BenchmarkEnvironment, root_ids: dict[str, str],
                     shape: DatasetShape) -> dict[str, Any]:
    return bench.query_evidence({"username": username(1),
                                 "requests": evidence_requests(root_ids, shape)})


def restart_cold(bench: BenchmarkEnvironment) -> list[str]:
    """Restart the request path by exact IDs; re-resolve peers only if an address moved."""
    before = container_identity(bench)
    actions = ["postgres", "web"]
    bench.restart(["postgres"])
    bench.restart(["web"])
    after = container_identity(bench)
    if after["web"]["ips"] != before["web"]["ips"]:
        bench.restart(["gateway"])
        actions.append("gateway")
        if container_identity(bench)["gateway"]["ips"] != before["gateway"]["ips"]:
            bench.restart(["web"])
            actions.append("web")
    bench.wait_healthy(["postgres", "web", "gateway", "indexer"])
    return actions


def run_catalog(arguments: argparse.Namespace) -> int:
    report_dir = validate_report_dir(arguments.report_dir)
    profile = load_profile()
    entries = arguments.entries if arguments.entries is not None else profile["entries"]
    wide_folder = (arguments.wide_folder if arguments.wide_folder is not None
                   else profile["wideFolderChildren"])
    warmup = arguments.warmup_seconds or profile["warmupSeconds"]
    measurement = arguments.measurement_seconds or profile["measurementSeconds"]
    full = (entries, wide_folder, warmup, measurement) == (
        profile["entries"], profile["wideFolderChildren"], profile["warmupSeconds"],
        profile["measurementSeconds"],
    )
    if not full and not arguments.smoke:
        raise SystemExit("A non-profile shape or duration requires --smoke and is not evidence.")
    shape = DatasetShape(entries=entries, wide_folder=wide_folder, seed=profile["seed"])
    label = ("provisional: full profile workload on an uncalibrated development host"
             if full and not arguments.smoke else "smoke: development run, not evidence")
    log(f"Report directory validated. Mode: {label}.")
    log("Computing exact generator counts and expected wide-folder order...")
    counts = dataset_counts(shape)
    wide = next(target for target in directory_targets(shape) if target.label == "wide")
    expected_rows = expected_children(shape, wide.id)
    expected_wide = [str(row["id"]) for row in expected_rows[:500]]
    hold = warmup + measurement + HOLD_MARGIN_SECONDS
    page_size = profile["pageSize"]
    started_at = datetime.now(UTC).isoformat()
    timings: dict[str, float] = {}
    summary: dict[str, Any] = {}
    cleanup: dict[str, Any] = {}
    with benchmark_environment(profile, report_dir, entries=entries, wide_folder=wide_folder,
                               hold_seconds=hold, log=log) as bench:
        cleanup["project"] = bench.project
        cleanup["workspace"] = str(bench.workspace.path)
        identity = {"host": host_identity(), "engine": engine_identity(bench),
                    "source": source_identity(), "release": "phase2a-benchmark"}
        log(f"Seeding {entries} entries ({wide_folder}-child wide folder)...")
        moment = time.monotonic()
        seed = bench.seed()
        timings["seedSeconds"] = round(time.monotonic() - moment, 1)
        if seed["counts"]["entries"] != counts["entries"] or any(
            seed["counts"][key] != counts[key] for key in ("kinds", "states", "roots")
        ):
            raise RuntimeError("benchmark seed counts differ from the generator")
        log(f"Seeded in {timings['seedSeconds']}s; logging in ten independent sessions...")
        root_ids: dict[str, str] = seed["roots"]
        password = bench.password()
        sessions: dict[int, HttpSession] = {}
        for number in range(1, profile["users"] + 1):
            session = HttpSession(bench.base_url)
            session.login(username(number), password)
            sessions[number] = session
        admin = HttpSession(bench.base_url)
        admin.login(ADMIN_USERNAME, password)
        log("Verifying membership, order, availability and grants over HTTP...")
        verification = verify_catalog(sessions, admin, root_ids, shape, page_size,
                                      expected_wide, len(expected_rows))
        targets = directory_targets(shape)
        workers = [
            Worker(number, sessions[number], accessible_targets(number, targets), wide,
                   root_ids, page_size, random.Random(f"{profile['seed']}:{number}"))
            for number in sorted(sessions)
        ]
        plans = [plan_operations(profile, seed=profile["seed"], worker=worker.number)
                 for worker in workers]
        sink = SampleSink(report_dir / "samples.csv")
        tree = next(target for target in targets
                    if target.label == "tree" and target.root == "main")
        sampler: ResourceSampler | None = None
        try:
            log("Cold: restarting PostgreSQL and web by exact container ID...")
            restarts = restart_cold(bench)
            sampler = ResourceSampler(bench, ("web", "postgres", "gateway", "indexer"))
            sampler.start()
            sampler.phase = "cold"
            run_phase(workers, plans, sink, "cold", operations=arguments.cold_operations)
            mix_cold = mix_report(workers)
            for worker in workers:
                worker.planned.clear()
                worker.realized.clear()
            sampler.phase = "warmup"
            log(f"Warm-up {warmup}s (recorded, excluded from the gate)...")
            run_phase(workers, plans, sink, "warmup", seconds=warmup, progress=log)
            for worker in workers:
                worker.planned.clear()
                worker.realized.clear()
            sampler.phase = "warm"
            log(f"Measurement {measurement}s, ten concurrent users...")
            run_phase(workers, plans, sink, "warm", seconds=measurement, progress=log)
            mix_warm = mix_report(workers)
            sampler.phase = "sequential"
            log("Sequential single-user baselines (list, details, status)...")
            run_sequential_baseline(sessions[1], wide, tree, root_ids["main"], sink,
                                    page_size=page_size)
            sampler.phase = None
            resources_after = resource_snapshot(bench)
            log("Filter/sort matrix (sequential, unloaded)...")
            matrix = run_matrix(sessions[1], wide, root_ids["main"], sink, page_size=page_size)
        finally:
            if sampler is not None:
                sampler.stop()
            samples = sink.close()
        utilization = sampler.summary()
        log("Collecting SQL counts, sanitized EXPLAIN plans, sizes and settings...")
        evidence = collect_evidence(bench, root_ids, shape)
        identity["containers"] = container_identity(bench)
        phases = summarize(samples)
        warm_lists = [sample for sample in samples
                      if sample.phase == "warm" and sample.route == "list"]
        wide_share = (sum(":wide:" in sample.klass for sample in warm_lists) / len(warm_lists)
                      if warm_lists else 0.0)
        enforce_wide_share(phases, wide_share, profile["wideFolderListShareMinimum"])
        summary = {
            "version": 1, "package": "2A.1", "mode": "catalog", "evidenceLabel": label,
            "referenceCertification": "open: requires the calibrated N100 reference host",
            "startedAt": started_at, "profile": profile,
            "shape": {"entries": entries, "wideFolder": wide_folder, "seed": profile["seed"]},
            "durations": {"warmupSeconds": warmup, "measurementSeconds": measurement,
                          "coldOperationsPerUser": arguments.cold_operations},
            "fixtureControl": seed["fixtureControl"], "scheduleHoldSeconds": hold,
            "postSeedMaintenance": seed.get("postSeedMaintenance"),
            "workerLoad": "none (catalog seed mode; no scan or preview jobs ran)",
            "cpuPlacement": {
                "requested": "shared four-CPU set (reference profile)",
                "applied": {"mode": bench.cpu[0], "value": bench.cpu[1]},
                "note": ("shared cpuset enforced" if bench.cpu[0] == "cpuset" else
                         "engine has no cpuset controller: each core service capped at 4 CPUs; "
                         "the shared four-CPU set is NOT enforced"),
            },
            "clientModel": "closed loop, ten concurrent users (one Python thread and cookie "
                           "jar each), zero think time; the load generator shares the host, "
                           "and client-side GIL waits are included in measured latency",
            "optimisticFixtureSteps": "post-seed VACUUM (ANALYZE) and an OS page cache that "
                                      "survives the cold restart both favor this run; Task 16's "
                                      "worker-load run is the counterweight",
            "realizedMix": {"cold": mix_cold, "warm": mix_warm},
            "utilization": utilization,
            "bottleneck": bottleneck_evidence(phases, utilization, measurement),
            "storage": bench.storage,
            "loadedRun": "not run in catalog mode (Task 16 owns worker-load runs)",
            "datasetCounts": counts, "seedCounts": seed["counts"], "timings": timings,
            "verification": verification, "coldRestart": restarts,
            "wideFolderListShare": round(wide_share, 4), "phases": phases,
            "matrix": matrix, "queryEvidence": evidence, "resourcesAfterMeasurement":
            resources_after, "environment": identity,
        }
        write_report(report_dir, "summary.json", summary, secrets=bench.secrets)
        write_text(report_dir, "summary.md", markdown_summary(summary), secrets=bench.secrets)
        tracked = verification_summary(summary)
        write_report(report_dir, "phase-2a-catalog-benchmark.json", tracked,
                     secrets=bench.secrets)
        write_text(report_dir, "phase-2a-catalog-benchmark.md", verification_markdown(tracked),
                   secrets=bench.secrets)
        secret_values = bench.secrets
    workspace = Path(cleanup["workspace"])
    cleanup["workspaceRemoved"] = not workspace.exists()
    cleanup["reportsPreserved"] = sorted(path.name for path in report_dir.iterdir())
    write_report(report_dir, "cleanup.json", cleanup, secrets=secret_values)
    warm_gate = phases.get("warm", {}).get("gate", {})
    log(f"Done. Warm list p95={warm_gate.get('listP95Ms')} ms, "
        f"failures={warm_gate.get('failures')}, gate passed={warm_gate.get('passed')} "
        f"({label}).")
    # Exit status reflects only the measured gate; certification labeling is separate.
    return gate_exit_code(phases)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        if arguments.mode == "catalog":
            return run_catalog(arguments)
    except BenchmarkResourceError as error:
        log(f"Refused: {error}")
        return 2
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
