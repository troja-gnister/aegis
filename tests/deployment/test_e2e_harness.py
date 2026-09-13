from __future__ import annotations

import json
import os
import runpy
from copy import deepcopy
from pathlib import Path

import pytest

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
    tmp_path: Path,
) -> None:
    SUPPORT["prepare"](tmp_path)
    files = list((tmp_path / "secrets").iterdir())
    assert len(files) == 11
    assert len({file.read_text() for file in files}) == len(files)
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in files)
    sentinel = tmp_path / "unrecognized-data"
    sentinel.write_text("retain")
    with pytest.raises(OSError):
        SUPPORT["cleanup"](tmp_path)
    assert sentinel.read_text() == "retain"


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
