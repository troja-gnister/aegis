"""The test-only benchmark seed creates a real, authorized catalog; pages match the generator."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.indexing.models import IndexDeployment, RootIndexState
from aegis_apps.roots.models import Root, RootGrant
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.db import connection
from django.test import Client
from django.utils import timezone

from scripts.benchmarks.dataset import (
    ADMIN_USERNAME,
    ROOTS,
    DatasetShape,
    dataset_counts,
    directory_targets,
    expected_children,
    username,
)
from tests.support.database_roles import RoleDatabase

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

SHAPE = DatasetShape(entries=2400, wide_folder=700, seed=31337)
HOLD_SECONDS = 7200


def _private_file(path: Path, content: str) -> Path:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


@pytest.fixture
def bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    manifest = tmp_path / "manifest.json"
    raw = json.dumps({
        "version": 1, "generatedAt": "2026-10-01T12:00:00Z", "slots": [{
            "slotId": root.slot_id, "containerPath": f"/srv/aegis/roots/{root.slot_id}",
            "mode": "read_only", "filesystemId": 100, "rootInode": 200 + number,
            "expectedIdentity": f"remote:synthetic.invalid:/{root.slot_id}",
            "mountFingerprint": f"{number:x}" * 64,
        } for number, root in enumerate(ROOTS, start=1)],
    }).encode()
    manifest.write_bytes(raw)
    manifest.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(manifest))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", digest)
    IndexDeployment.objects.create(
        manifest_identity=digest, slot_ids=sorted(root.slot_id for root in ROOTS),
        interval_seconds=3600, idle_timeout_seconds=120, batch_records=500, readers=1,
    )
    password = secrets.token_hex(24)
    token = secrets.token_hex(6)
    record = {
        "version": 1, "token": token, "database": connection.settings_dict["NAME"],
        "role": "aegis_migrator", "entries": SHAPE.entries, "wideFolder": SHAPE.wide_folder,
        "seed": SHAPE.seed, "users": 10, "scheduleHoldSeconds": HOLD_SECONDS,
    }
    return {
        "record": _private_file(tmp_path / "ownership.json", json.dumps(record)),
        "record_data": record,
        "password_file": _private_file(tmp_path / "password", password + "\n"),
        "password": password,
        "tmp_path": tmp_path,
    }


def _seed(bench: dict[str, Any], record: Path | None = None) -> None:
    call_command(
        "seed_catalog_benchmark", ownership_record=str(record or bench["record"]),
        password_file=str(bench["password_file"]),
    )


def _login(role_database: RoleDatabase, name: str, password: str) -> Client:
    client = Client(enforce_csrf_checks=True)
    with role_database.as_django_role("aegis_web"):
        token = client.get("/api/v1/auth/csrf").json()["csrfToken"]
        response = client.post(
            "/api/v1/auth/login", {"username": name, "password": password},
            content_type="application/json", headers={"X-CSRFToken": token},
        )
    assert response.status_code == 200
    return client


def _get(role_database: RoleDatabase, client: Client, path: str, **query: str) -> Any:
    with role_database.as_django_role("aegis_web"):
        return client.get(path, query)


def _walk(role_database: RoleDatabase, client: Client, root_id: object, parent: object,
          **query: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    cursor: str | None = None
    for _page in range(100):
        params = {"parent": str(parent), "limit": "250", **query}
        if cursor:
            params["cursor"] = cursor
        response = _get(role_database, client, f"/api/v1/roots/{root_id}/entries", **params)
        assert response.status_code == 200, response.content[:200]
        payload = response.json()
        entries.extend(payload["entries"])
        cursor = payload["nextCursor"]
        if cursor is None:
            return entries
    raise AssertionError("unbounded walk")


def test_seed_builds_the_generated_catalog_with_real_grants_and_pages(
    bench: dict[str, Any], role_database: RoleDatabase,
) -> None:
    before = timezone.now()
    with role_database.as_django_role("aegis_migrator"):
        _seed(bench)
    counts = dataset_counts(SHAPE)
    assert CatalogEntry.objects.count() == counts["entries"] == SHAPE.entries
    for kind, count in counts["kinds"].items():
        assert CatalogEntry.objects.filter(kind=kind).count() == count
    for state, count in counts["states"].items():
        assert CatalogEntry.objects.filter(source_state=state).count() == count
    roots = {root.slot_id: root for root in Root.objects.all()}
    assert set(roots) == {root.slot_id for root in ROOTS}
    assert User.objects.filter(username__startswith="bench-user-").count() == 10
    admin = User.objects.get(username=ADMIN_USERNAME)
    assert admin.is_superuser and admin.is_staff
    assert not RootGrant.objects.filter(user=admin).exists()
    assert not admin.groups.exists()
    assert RootGrant.objects.filter(group__isnull=False).exists()
    assert RootGrant.objects.filter(user__isnull=False).exists()
    for state in RootIndexState.objects.all():
        assert state.status == "ready"
        assert state.due_at >= before + timedelta(seconds=HOLD_SECONDS)
        assert state.active_run_id is None and not state.rescan_requested
    assert Group.objects.filter(name__startswith="bench-").count() >= 1

    from aegis_apps.operations.heartbeats import publish_heartbeat_for_test
    from aegis_apps.operations.selectors import current_schema_identity
    publish_heartbeat_for_test(
        role="indexer", worker_id="00000000-0000-4000-8000-000000000001",
        observed_at=timezone.now(), release_id=settings.AEGIS_RELEASE_ID,
        schema_identity=current_schema_identity(),
        manifest_identity=IndexDeployment.objects.get(pk=1).manifest_identity,
        current_job_id=None, status="idle", metrics={},
    )

    main = next(root for root in ROOTS if root.key == "main")
    reader = username(main.direct_users[0])
    group_reader = username(main.group_users[0])
    client = _login(role_database, reader, bench["password"])
    group_client = _login(role_database, group_reader, bench["password"])
    main_id = roots[main.slot_id].pk
    targets = directory_targets(SHAPE)
    wide = next(target for target in targets if target.label == "wide")
    for sort, order in (("name", "asc"), ("size", "desc"), ("modified", "asc")):
        walked = _walk(role_database, client, main_id, wide.id, sort=sort, order=order)
        expected = expected_children(SHAPE, wide.id, sort=sort, order=order)
        assert [entry["id"] for entry in walked] == [str(row["id"]) for row in expected]
    walked = _walk(role_database, group_client, main_id, wide.id)
    assert len(walked) == len(expected_children(SHAPE, wide.id)) > 0
    states = {entry["sourceState"] for entry in walked}
    assert "present" in states and "inaccessible" in states
    assert sum(entry["sourceState"] == "present" for entry in walked) > len(walked) * 0.9

    for target in (target for target in targets if target.label == "stale"):
        page = _walk(role_database, client, main_id, target.id)
        assert page and all(entry["sourceState"] != "present" for entry in page)
        response = _get(role_database, client, f"/api/v1/roots/{main_id}/entries",
                        parent=str(target.id))
        assert response.json()["indexStatus"]["state"] == "unavailable"
    status = _get(role_database, client, f"/api/v1/roots/{main_id}/index-status").json()
    assert status["state"] == "ready"
    details = _get(role_database, client, f"/api/v1/entries/{walked[1]['id']}").json()
    assert details["parentId"] == str(wide.id)

    unrelated = next(root for root in ROOTS if main.direct_users[0] not in (
        *root.direct_users, *root.group_users))
    assert _get(role_database, client,
                f"/api/v1/roots/{roots[unrelated.slot_id].pk}/entries").status_code == 404
    admin_client = _login(role_database, ADMIN_USERNAME, bench["password"])
    assert _get(role_database, admin_client, "/api/v1/roots").json()["roots"] == []
    assert _get(role_database, admin_client,
                f"/api/v1/roots/{main_id}/entries").status_code == 404


def test_seed_refuses_double_seeding_without_any_reset(
    bench: dict[str, Any], role_database: RoleDatabase,
) -> None:
    with role_database.as_django_role("aegis_migrator"):
        _seed(bench)
        with pytest.raises(CommandError, match="fresh empty benchmark database"):
            _seed(bench)
    assert CatalogEntry.objects.count() == SHAPE.entries


def test_seed_refuses_unexpected_database_and_role_before_writing(
    bench: dict[str, Any], role_database: RoleDatabase,
) -> None:
    other = bench["tmp_path"] / "other.json"
    _private_file(other, json.dumps(bench["record_data"] | {"database": "aegis"}))
    with (role_database.as_django_role("aegis_migrator"),
          pytest.raises(CommandError, match="unexpected benchmark database")):
        _seed(bench, other)
    with pytest.raises(CommandError, match="unexpected benchmark database role"):
        _seed(bench)  # the default test connection is not the migrator login
    assert not CatalogEntry.objects.exists()
    assert not User.objects.exists()


def test_seed_refuses_production_and_unsafe_ownership_records(
    bench: dict[str, Any], role_database: RoleDatabase, monkeypatch: pytest.MonkeyPatch,
) -> None:
    with role_database.as_django_role("aegis_migrator"):
        monkeypatch.setattr(settings, "AEGIS_ENVIRONMENT", "production")
        with pytest.raises(CommandError, match="restricted to the test environment"):
            _seed(bench)
        monkeypatch.setattr(settings, "AEGIS_ENVIRONMENT", "test")
        bench["record"].chmod(0o644)
        with pytest.raises(CommandError, match="ownership record"):
            _seed(bench)
        bench["record"].chmod(0o600)
        link = bench["tmp_path"] / "link.json"
        link.symlink_to(bench["record"])
        with pytest.raises(CommandError, match="ownership record"):
            _seed(bench, link)
        changed = bench["tmp_path"] / "changed.json"
        _private_file(changed, json.dumps(bench["record_data"] | {"entries": 10}))
        with pytest.raises(CommandError, match="ownership record"):
            _seed(bench, changed)
    assert not CatalogEntry.objects.exists()
