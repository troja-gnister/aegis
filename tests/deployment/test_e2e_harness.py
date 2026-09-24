from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from aegisctl.container_resources import ProjectInventory, ProjectResource

SUPPORT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/e2e_support.py"))


def test_failure_logs_never_retain_unstructured_messages_or_credentials() -> None:
    for raw in (
        'password=private-sentinel /srv/aegis/roots/personal',
        json.dumps({"level": "ERROR", "status": 503, "message": "private-sentinel",
                    "metadata": {"password": "private-sentinel"}}),
        json.dumps({"level": {"secret": "private-sentinel"}, "status": "private-sentinel"}),
        "private-sentinel" * 8000,
    ):
        result = json.loads(SUPPORT["sanitize_line"](raw))
        assert "private-sentinel" not in json.dumps(result)
        assert "/srv/aegis" not in json.dumps(result)
        assert set(result) <= {"level", "status", "message"}


def test_generated_secrets_are_private_unique_and_cleanup_preserves_unknown_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    path = private_e2e_directory()
    SUPPORT["prepare"](path)
    files = list((path / "secrets").iterdir())
    assert len(files) == 11
    assert len({file.read_text() for file in files}) == len(files)
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in files)
    assert (path / "roots/alice").is_dir()
    assert (path / "roots/bob").is_dir()
    mounts = (path / "mounts.toml").read_text(encoding="ascii")
    assert str(path / "roots/alice") in mounts
    assert str(path / "roots/bob") in mounts
    assert str(Path(__file__).resolve().parents[2] / "tests/fixtures/roots") not in mounts
    sentinel = path / "unrecognized-data"
    sentinel.write_text("retain")
    with pytest.raises(ValueError, match="unknown"):
        SUPPORT["cleanup"](path)
    assert sentinel.read_text() == "retain"
    assert (path / "secrets/e2e-alice-password").exists()
    sentinel.unlink()
    SUPPORT["cleanup"](path)


@pytest.mark.parametrize("directory", ("/", "/tmp", "/Users", "/srv/aegis"))
def test_cleanup_refuses_broad_or_unowned_directory_targets(directory: str) -> None:
    with pytest.raises((ValueError, OSError)):
        SUPPORT["checked_directory"](directory)


def _safe_compose() -> dict:
    services = {}
    for role in ("web", "migrate", "operations", "indexer", "media", "gateway"):
        services[role] = {
            "read_only": True, "cap_drop": ["ALL"], "networks": {"backend": {}},
            "volumes": [] if role in ("web", "migrate") else [
                {"type": "bind", "source": f"/fixture/{name}",
                 "target": f"/srv/aegis/roots/e2e-{name}", "read_only": True}
                for name in ("alice", "bob")
            ],
        }
    services["gateway"]["ports"] = [{"host_ip": "127.0.0.1"}]
    services["postgres"] = {"ports": [{"host_ip": "127.0.0.1"}]}
    return {
        "name": "aegis-phase1-e2e", "services": services,
        "networks": {"backend": {"internal": True}},
        "volumes": {"postgres-data": {"name": "aegis-phase1-e2e_postgres-data"}},
    }


@pytest.mark.parametrize("drift", ("protected_volume", "external", "writable", "egress", "port"))
def test_compose_gate_rejects_unsafe_resource_or_original_mount_changes(drift: str) -> None:
    original = _safe_compose()
    SUPPORT["check_compose"](original)
    changed = deepcopy(original)
    if drift == "protected_volume":
        changed["volumes"]["postgres-data"]["name"] = "aegis_postgres-data"
    elif drift == "external":
        changed["volumes"]["postgres-data"]["external"] = True
    elif drift == "writable":
        changed["services"]["operations"]["volumes"][0]["read_only"] = False
    elif drift == "egress":
        changed["services"]["indexer"]["networks"]["edge"] = {}
    else:
        changed["services"]["postgres"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(ValueError):
        SUPPORT["check_compose"](changed)


def test_test_profile_keeps_fixture_credentials_out_of_the_production_compose() -> None:
    repository = Path(__file__).resolve().parents[2]
    assert "e2e-alice-password" not in (repository / "compose.yaml").read_text()
    assert "e2e-alice-password" in (repository / "compose.test.yaml").read_text()
    assert os.access(repository / "scripts/test-e2e.sh", os.X_OK)


def private_e2e_directory() -> Path:
    path = Path(tempfile.mkdtemp(prefix="aegis-phase1-e2e.", dir="/tmp"))
    path.chmod(0o700)
    return path


def complete_runtime_inputs(path: Path) -> None:
    for name in SUPPORT["GENERATED_NAMES"]:
        target = path / name
        if not target.exists():
            target.write_text("generated\n", encoding="ascii")
    SUPPORT["record_generated"](path)


def test_podman_preparation_targets_only_inventoried_synthetic_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    provider = tmp_path / "provider"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)
    commands: list[list[str]] = []

    def completed(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(arguments)
        output = ""
        if arguments[2:4] == ["unshare", "stat"]:
            output = "1000:1000\n" * (len(SUPPORT["SECRET_NAMES"]) + 1)
        return subprocess.CompletedProcess(arguments, 0, output, "")

    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")
        monkeypatch.setattr(subprocess, "run", completed)

        SUPPORT["prepare_sources"](path)
        complete_runtime_inputs(path)
        SUPPORT["prepare_runtime"](path)

        secrets = [str(path / "secrets" / name) for name in SUPPORT["SECRET_NAMES"]]
        label_targets = [
            *secrets,
            str(path / "mounts.manifest.json"),
            str(path / "mounts.gateway.attestation"),
        ]
        assert commands == [
            ["chcon", "--no-dereference", "--type", "container_file_t", "--",
             str(path / "roots/alice"), str(path / "roots/bob")],
            ["chcon", "--no-dereference", "--type", "container_file_t", "--", *label_targets],
            ["podman", "--remote=false", "unshare", "chown", "--no-dereference", "1000:1000", "--",
             *secrets, str(path / "mounts.manifest.json")],
            ["podman", "--remote=false", "unshare", "stat", "--format=%u:%g", "--",
             *secrets, str(path / "mounts.manifest.json")],
        ]
    finally:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_e2e_mapping_refuses_added_entry_without_updating_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    unknown = path / "added-during-map"
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        ledger_path = path / SUPPORT["LEDGER_NAME"]
        original_ledger = ledger_path.read_bytes()
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")

        def completed(
            arguments: list[str], **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            del kwargs
            if arguments[2:4] == ["unshare", "chown"]:
                unknown.write_text("retain\n", encoding="ascii")
            output = ""
            if arguments[2:4] == ["unshare", "stat"]:
                output = "1000:1000\n" * (len(SUPPORT["SECRET_NAMES"]) + 1)
            return subprocess.CompletedProcess(arguments, 0, output, "")

        monkeypatch.setattr(subprocess, "run", completed)
        with pytest.raises(ValueError, match="ownership inventory changed"):
            SUPPORT["prepare_runtime"](path)
        assert ledger_path.read_bytes() == original_ledger
        assert unknown.read_text(encoding="ascii") == "retain\n"
    finally:
        unknown.unlink(missing_ok=True)
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


@pytest.mark.parametrize("field", (stat.ST_UID, stat.ST_GID))
def test_e2e_preparation_refuses_recorded_owner_drift_before_mutation(
    monkeypatch: pytest.MonkeyPatch, field: int,
) -> None:
    path = private_e2e_directory()
    commands: list[list[str]] = []
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        target = path / "mounts.manifest.json"
        original_lstat = Path.lstat

        def changed(candidate: Path) -> os.stat_result:
            metadata = original_lstat(candidate)
            if candidate == target:
                values = list(metadata)
                values[field] += 1
                return os.stat_result(values)
            return metadata

        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setattr(Path, "lstat", changed)
        monkeypatch.setattr(
            subprocess, "run", lambda arguments, **kwargs: commands.append(arguments),
        )

        with pytest.raises(ValueError, match="inventory changed"):
            SUPPORT["prepare_runtime"](path)
        assert commands == []
    finally:
        monkeypatch.setattr(Path, "lstat", original_lstat)
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_podman_preparation_refuses_replaced_or_unknown_inputs_before_ownership_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    provider = tmp_path / "provider"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)
    ownership_called = False
    original_manifest = path / "original-manifest"

    def replace_manifest(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal ownership_called
        del kwargs
        if arguments[:4] == ["podman", "--remote=false", "unshare", "chown"]:
            ownership_called = True
        else:
            manifest = path / "mounts.manifest.json"
            manifest.rename(original_manifest)
            manifest.symlink_to(path / "mounts.toml")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")
        monkeypatch.setattr(subprocess, "run", replace_manifest)

        with pytest.raises(ValueError, match=r"inventory|replaced"):
            SUPPORT["prepare_runtime"](path)
        assert ownership_called is False

        (path / "mounts.manifest.json").unlink()
        original_manifest.rename(path / "mounts.manifest.json")
        (path / "unexpected").write_text("retain\n", encoding="ascii")
        with pytest.raises(ValueError, match="unknown"):
            SUPPORT["prepare_runtime"](path)
        with pytest.raises(ValueError, match="unknown"):
            SUPPORT["cleanup"](path)
        assert (path / "secrets/e2e-alice-password").exists()
    finally:
        (path / "unexpected").unlink(missing_ok=True)
        if original_manifest.exists():
            (path / "mounts.manifest.json").unlink(missing_ok=True)
            original_manifest.rename(path / "mounts.manifest.json")
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_e2e_ledger_refuses_intermediate_symlink_hardlink_and_known_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "alice").mkdir()
    (outside / "bob").mkdir()
    commands: list[list[str]] = []
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        roots = path / "roots"
        saved = path / "saved-roots"
        roots.rename(saved)
        roots.symlink_to(outside, target_is_directory=True)
        support_globals = SUPPORT["prepare_sources"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setattr(
            subprocess, "run", lambda arguments, **kwargs: commands.append(arguments),
        )
        with pytest.raises(ValueError, match=r"inventory|replaced"):
            SUPPORT["prepare_sources"](path)
        assert commands == []
        roots.unlink()
        saved.rename(roots)

        original = tmp_path / "user-original"
        original.write_text("preserve", encoding="ascii")
        linked = path / "unexpected-hardlink"
        os.link(original, linked)
        with pytest.raises(ValueError, match="hardlink"):
            SUPPORT["cleanup"](path)
        assert original.read_text(encoding="ascii") == "preserve"
        assert (path / "secrets/e2e-alice-password").exists()
        linked.unlink()
    finally:
        if path.exists():
            monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
            SUPPORT["cleanup"](path)


def test_e2e_resource_cleanup_refusal_preserves_complete_filesystem_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        cleanup_globals = SUPPORT["resources_cleanup"].__globals__

        def refuse(_inventory: object) -> None:
            raise RuntimeError("unknown project resource")

        monkeypatch.setitem(cleanup_globals, "cleanup_project_inventory", refuse)
        with pytest.raises(RuntimeError, match="unknown project resource"):
            SUPPORT["resources_cleanup"](path)
        assert (path / "secrets/e2e-alice-password").exists()
        assert (path / "roots/alice").is_dir()
        assert (path / SUPPORT["LEDGER_NAME"]).exists()
    finally:
        SUPPORT["cleanup"](path)


def test_e2e_resource_record_refuses_unknown_addition_without_overwriting_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        resource = ProjectResource(
            "container", "unknown-id", "unknown-id",
            json.dumps({
                "Id": "unknown-id", "Created": "now", "Name": "foreign-postgres",
                "Image": "image", "Labels": {
                    "com.docker.compose.project": SUPPORT["PROJECT"],
                    "com.docker.compose.service": "postgres",
                    "com.docker.compose.oneoff": "False",
                    "aegis.test.resource-token": "foreign-token",
                },
            }, sort_keys=True, separators=(",", ":")),
        )
        record_globals = SUPPORT["resources_record"].__globals__
        monkeypatch.setitem(
            record_globals, "capture_project_inventory",
            lambda project: ProjectInventory(project, (resource,)),
        )

        with pytest.raises(RuntimeError, match="unexpected"):
            SUPPORT["resources_record"](path, "up")
        assert SUPPORT["_stored_resources"](SUPPORT["_load_ledger"](path)).resources == ()
    finally:
        SUPPORT["cleanup"](path)


def test_e2e_reads_host_credentials_before_subordinate_ownership_preparation() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/test-e2e.sh").read_text()

    preparation = script.index("prepare-runtime")
    for variable in ("E2E_ALICE_PASSWORD", "E2E_BOB_PASSWORD", "E2E_ADMIN_PASSWORD"):
        assert script.index(f"read -r {variable}") < preparation
    assert script.index("prepare-sources") < script.index("mounts preflight")
