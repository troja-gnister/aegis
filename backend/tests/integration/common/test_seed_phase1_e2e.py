from __future__ import annotations

import hashlib
import json
import os
from contextlib import AbstractContextManager
from io import StringIO
from pathlib import Path

import pytest
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command
from django.test import override_settings

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _secret(path: Path, value: str) -> Path:
    path.write_text(f"{value}\n")
    path.chmod(0o600)
    return path


def _manifest(path: Path) -> str:
    payload = {
        "version": 1,
        "generatedAt": "2026-09-11T12:00:00Z",
        "slots": [
            {
                "slotId": slot_id,
                "containerPath": f"/srv/aegis/roots/{slot_id}",
                "mode": "read_only",
                "filesystemId": index + 100,
                "rootInode": index + 200,
                "expectedIdentity": f"remote:e2e.invalid:/fixture/{index}",
                "mountFingerprint": f"{index + 1:x}" * 64,
            }
            for index, slot_id in enumerate(("e2e-alice", "e2e-bob"))
        ],
    }
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    os.chown(path, os.geteuid(), os.getegid())
    return hashlib.sha256(raw).hexdigest()


def _settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AbstractContextManager[object]:
    alice = _secret(tmp_path / "alice-password", "Alice-Pine-River-731!")
    bob = _secret(tmp_path / "bob-password", "Bob-Copper-Harbor-984!")
    admin = _secret(tmp_path / "admin-password", "Admin-Orbit-Lantern-448!")
    manifest = tmp_path / "mounts.manifest.json"
    digest = _manifest(manifest)
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(manifest))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", digest)
    return override_settings(
        AEGIS_ENVIRONMENT="test",
        E2E_ALICE_PASSWORD_FILE=str(alice),
        E2E_BOB_PASSWORD_FILE=str(bob),
        E2E_ADMIN_PASSWORD_FILE=str(admin),
    )


def test_seed_creates_exact_identities_roots_and_grants_idempotently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _settings(tmp_path, monkeypatch):
        first_stdout = StringIO()
        call_command("seed_phase1_e2e", stdout=first_stdout)

        alice = User.objects.get(username="alice")
        bob = User.objects.get(username="bob")
        admin = User.objects.get(username="phase1-admin")
        hashes = {user.username: user.password for user in (alice, bob, admin)}
        epochs = {user.username: user.authorization_epoch for user in (alice, bob, admin)}
        root_epochs = dict(Root.objects.values_list("slot_id", "authorization_epoch"))

        second_stdout = StringIO()
        call_command("seed_phase1_e2e", stdout=second_stdout)

    alice.refresh_from_db()
    bob.refresh_from_db()
    admin.refresh_from_db()
    group = Group.objects.get(name="e2e-bob")
    alice_root = Root.objects.get(slot_id="e2e-alice")
    bob_root = Root.objects.get(slot_id="e2e-bob")

    assert first_stdout.getvalue() == second_stdout.getvalue() == "phase1 fixtures ready\n"
    assert {user.username: user.password for user in (alice, bob, admin)} == hashes
    assert {user.username: user.authorization_epoch for user in (alice, bob, admin)} == epochs
    assert dict(Root.objects.values_list("slot_id", "authorization_epoch")) == root_epochs
    assert alice.check_password("Alice-Pine-River-731!")
    assert bob.check_password("Bob-Copper-Harbor-984!")
    assert admin.check_password("Admin-Orbit-Lantern-448!")
    assert not alice.is_staff and not alice.is_superuser and alice.is_active
    assert not bob.is_staff and not bob.is_superuser and bob.is_active
    assert admin.is_staff and admin.is_superuser and admin.is_active
    assert set(alice.groups.all()) == set()
    assert set(bob.groups.all()) == {group}
    assert set(admin.groups.all()) == set()
    assert Root.objects.count() == 2
    assert all(root.active and root.mode == Root.Mode.READ_ONLY for root in (alice_root, bob_root))
    assert RootGrant.objects.count() == 2
    assert RootGrant.objects.filter(
        root=alice_root, user=alice, group=None, permissions=int(Permission.BROWSE)
    ).count() == 1
    assert RootGrant.objects.filter(
        root=bob_root, user=None, group=group, permissions=int(Permission.BROWSE)
    ).count() == 1
    assert not RootGrant.objects.filter(user=bob).exists()
    assert not RootGrant.objects.filter(user=admin).exists()


def test_seed_refuses_production_before_reading_any_fixture_secret(tmp_path: Path) -> None:
    sensitive_path = tmp_path / "must-not-be-read"
    with override_settings(
        AEGIS_ENVIRONMENT="production",
        E2E_ALICE_PASSWORD_FILE=str(sensitive_path),
        E2E_BOB_PASSWORD_FILE=str(sensitive_path),
        E2E_ADMIN_PASSWORD_FILE=str(sensitive_path),
    ), pytest.raises(CommandError, match="seed_phase1_e2e is restricted") as caught:
        call_command("seed_phase1_e2e")

    assert str(sensitive_path) not in str(caught.value)


def test_seed_fails_closed_on_existing_identity_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    User.objects.create_user(username="alice", password="unrelated-password", is_staff=True)

    with _settings(tmp_path, monkeypatch), pytest.raises(
        CommandError, match=r"^phase1 fixture configuration is invalid$"
    ):
        call_command("seed_phase1_e2e")

    alice = User.objects.get(username="alice")
    assert alice.is_staff
    assert alice.check_password("unrelated-password")
    assert User.objects.count() == 1
    assert Root.objects.count() == 0


@pytest.mark.parametrize("drift", ("manifest", "group_permissions", "grant", "password"))
def test_seed_rerun_rejects_drift_without_repairing_existing_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    with _settings(tmp_path, monkeypatch):
        call_command("seed_phase1_e2e", stdout=StringIO())
        alice = User.objects.get(username="alice")
        password_hash = alice.password
        if drift == "manifest":
            monkeypatch.delenv("AEGIS_MOUNT_MANIFEST")
            monkeypatch.delenv("AEGIS_MOUNT_MANIFEST_SHA256")
        elif drift == "group_permissions":
            from django.contrib.auth.models import Permission as DjangoPermission

            permission = DjangoPermission.objects.first()
            assert permission is not None
            Group.objects.get(name="e2e-bob").permissions.add(permission)
        elif drift == "grant":
            RootGrant.objects.filter(user=alice).update(permissions=int(Permission.EXPORT))
        else:
            _secret(tmp_path / "alice-password", "A-different-new-password-987!")

        before_grants = list(RootGrant.objects.order_by("id").values())
        before_users = list(User.objects.order_by("id").values())
        with pytest.raises(CommandError, match=r"^phase1 fixture configuration is invalid$"):
            call_command("seed_phase1_e2e", stdout=StringIO())

    assert list(RootGrant.objects.order_by("id").values()) == before_grants
    assert list(User.objects.order_by("id").values()) == before_users
    alice.refresh_from_db()
    assert alice.password == password_hash


def test_seed_rejects_non_private_secret_before_creating_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _settings(tmp_path, monkeypatch):
        (tmp_path / "alice-password").chmod(0o644)
        with pytest.raises(CommandError, match=r"^phase1 fixture configuration is invalid$"):
            call_command("seed_phase1_e2e", stdout=StringIO())
    assert User.objects.count() == 0
    assert Root.objects.count() == 0
