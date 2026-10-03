"""Another web worker process never serves a revoked grant from its own cache.

A grant change invalidates only the local process's decision cache. A second process
(here a real subprocess using the web server's pooled ``aegis_web`` configuration)
must still observe the change, because every cached decision is keyed by the user
and root authorization epochs that it reads from the database on each call.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from aegis.config import WEB_DATABASE_POOL_ENV
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from aegis_apps.roots.selectors import clear_authorization_cache
from aegis_apps.roots.services import remove_grant, set_user_grant

from tests.support.database_roles import RoleDatabase

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

BACKEND = Path(__file__).resolve().parents[3]
REQUEST_ID = "cross_process_request"
WORKER = """
import json, sys, uuid
import django
django.setup()
from django.conf import settings
from aegis_apps.roots import selectors
assert settings.DATABASES["default"]["OPTIONS"]["pool"]
for line in sys.stdin:
    user_id, root_id = line.split()
    permissions = selectors.effective_permissions(
        user_id=uuid.UUID(user_id), root_id=uuid.UUID(root_id))
    print(json.dumps({"permissions": int(permissions),
                      "cached": len(selectors._decision_cache)}), flush=True)
"""


class OtherWorker:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process

    def ask(self, user: User, root: Root) -> dict[str, int]:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(f"{user.pk} {root.pk}\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, self.process.stderr.read() if self.process.stderr else ""
        return dict(json.loads(line))


@pytest.fixture
def other_worker(role_database: RoleDatabase) -> Iterator[OtherWorker]:
    from django.db import connection

    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AEGIS_", "DJANGO_"))}
    environment |= {
        "DJANGO_SETTINGS_MODULE": "aegis.settings.test", "AEGIS_ENV": "test",
        "PYTHONPATH": str(BACKEND), WEB_DATABASE_POOL_ENV: "enabled",
        "AEGIS_DB_NAME": str(connection.settings_dict["NAME"]),
        "AEGIS_DB_HOST": str(role_database.host), "AEGIS_DB_PORT": str(role_database.port),
        "AEGIS_DB_USER": "aegis_web",
        "AEGIS_DB_PASSWORD": role_database.passwords["aegis_web"],
    }
    process = subprocess.Popen(
        [sys.executable, "-c", WORKER], cwd=BACKEND, env=environment, text=True,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        yield OtherWorker(process)
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()


def _manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "version": 1, "generatedAt": "2026-10-01T00:00:00Z",
        "slots": [{
            "slotId": "photos", "containerPath": "/srv/aegis/roots/photos",
            "mode": "read_only", "filesystemId": 101, "rootInode": 201,
            "expectedIdentity": "remote:synthetic.invalid:/photos",
            "mountFingerprint": "b" * 64,
        }],
    }
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(path))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", hashlib.sha256(raw).hexdigest())


def test_a_grant_change_in_one_worker_takes_effect_in_another_workers_cache(
    other_worker: OtherWorker, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _manifest(tmp_path, monkeypatch)
    clear_authorization_cache()
    actor = User.objects.create_superuser(username="cross-process-actor")
    subject = User.objects.create_user(username="cross-process-subject")
    root = Root.objects.create(slot_id="photos", display_name="Photos",
                               mode=Root.Mode.READ_ONLY, active=True)

    assert other_worker.ask(subject, root)["permissions"] == 0
    set_user_grant(actor=actor, root_id=root.id, user_id=subject.id,
                   permissions=Permission.BROWSE, request_id=REQUEST_ID)
    granted = other_worker.ask(subject, root)
    assert granted["permissions"] == int(Permission.BROWSE)
    assert other_worker.ask(subject, root) == granted
    assert granted["cached"] >= 1

    grant = RootGrant.objects.get(root=root, user=subject)
    remove_grant(actor=actor, grant_id=grant.id, request_id=REQUEST_ID)

    # This process's invalidation never reached the other process's cache, which
    # still holds the old decision; the epoch read on the next call bypasses it.
    revoked = other_worker.ask(subject, root)
    assert revoked["permissions"] == 0
    assert revoked["cached"] > granted["cached"]
