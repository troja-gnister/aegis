from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models.deletion import PROTECT, ProtectedError

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _configure_manifest(
    *,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slot_id: str = "photos",
    mode: str = "read_only",
) -> None:
    payload = {
        "version": 1,
        "generatedAt": "2026-09-02T12:34:56Z",
        "slots": [
            {
                "slotId": slot_id,
                "containerPath": f"/srv/aegis/roots/{slot_id}",
                "mode": mode,
                "filesystemId": 123,
                "rootInode": 456,
                "expectedIdentity": "remote:nas.invalid:/opaque",
                "mountFingerprint": "a" * 64,
            }
        ],
    }
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    os.chown(path, os.geteuid(), os.getegid())
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(path))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", hashlib.sha256(raw).hexdigest())


def _root(
    *,
    slot_id: str = "photos",
    display_name: str = "Photos",
    mode: str = Root.Mode.READ_ONLY,
    capabilities: dict[str, object] | None = None,
) -> Root:
    values = {} if capabilities is None else capabilities
    return Root.objects.create(
        slot_id=slot_id,
        display_name=display_name,
        mode=mode,
        capabilities=values,
    )


def test_root_and_grant_use_uuid4_and_capabilities_are_not_generic_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    root = _root(capabilities={"secret": "internal-only"})
    user = User.objects.create_user(username="model-user")
    grant = RootGrant.objects.create(root=root, user=user, permissions=1)

    assert root.id.version == grant.id.version == 4
    assert Root._meta.get_field("capabilities").editable is False
    assert RootGrant._meta.get_field("root").remote_field.on_delete is PROTECT
    assert RootGrant._meta.get_field("user").remote_field.on_delete is PROTECT
    assert RootGrant._meta.get_field("group").remote_field.on_delete is PROTECT


@pytest.mark.parametrize(
    ("values", "field"),
    [
        ({"slot_id": "BAD/SLOT"}, "slot_id"),
        ({"display_name": "   "}, "display_name"),
        ({"authorization_epoch": -1}, "authorization_epoch"),
    ],
)
def test_root_model_rejects_invalid_bounded_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, object],
    field: str,
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    root = Root(slot_id="photos", display_name="Photos", mode=Root.Mode.READ_ONLY)
    for name, value in values.items():
        setattr(root, name, value)

    with pytest.raises(ValidationError) as caught:
        root.full_clean()

    assert field in caught.value.message_dict


def test_root_clean_requires_configured_live_slot_even_while_inactive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Root(
        slot_id="photos",
        display_name="Photos",
        mode=Root.Mode.READ_ONLY,
        active=False,
    )
    monkeypatch.delenv("AEGIS_MOUNT_MANIFEST", raising=False)
    monkeypatch.delenv("AEGIS_MOUNT_MANIFEST_SHA256", raising=False)
    with pytest.raises(ValidationError, match="manifest"):
        root.full_clean()

    _configure_manifest(
        path=tmp_path / "manifest.json",
        monkeypatch=monkeypatch,
        slot_id="other",
    )
    with pytest.raises(ValidationError, match="slot"):
        root.full_clean()


def test_root_clean_allows_weaker_mode_and_rejects_stronger_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    _configure_manifest(path=path, monkeypatch=monkeypatch, mode="read_write")
    weaker = Root(slot_id="photos", display_name="Photos", mode=Root.Mode.READ_ONLY)
    weaker.full_clean()

    _configure_manifest(path=path, monkeypatch=monkeypatch, mode="read_only")
    stronger = Root(slot_id="photos", display_name="Photos", mode=Root.Mode.READ_WRITE)
    with pytest.raises(ValidationError, match="mode"):
        stronger.full_clean()


@pytest.mark.parametrize("mode", ["invalid", "read-write", ""])
def test_database_rejects_invalid_root_modes(mode: str) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        _root(slot_id=f"slot-{mode or 'empty'}", mode=mode)


def test_database_rejects_invalid_principal_shapes_duplicate_grants_and_masks() -> None:
    root = _root()
    user = User.objects.create_user(username="grant-user")
    group = Group.objects.create(name="grant-group")

    invalid_grants = (
        RootGrant(root=root, permissions=1),
        RootGrant(root=root, user=user, group=group, permissions=1),
        RootGrant(root=root, user=user, permissions=-1),
        RootGrant(root=root, group=group, permissions=256),
    )
    for grant in invalid_grants:
        with pytest.raises(IntegrityError), transaction.atomic():
            grant.save(force_insert=True)

    RootGrant.objects.create(root=root, user=user, permissions=0)
    RootGrant.objects.create(root=root, group=group, permissions=255)
    with pytest.raises(IntegrityError), transaction.atomic():
        RootGrant.objects.create(root=root, user=user, permissions=2)
    with pytest.raises(IntegrityError), transaction.atomic():
        RootGrant.objects.create(root=root, group=group, permissions=2)


def test_root_and_grant_generic_deletion_are_disabled_and_principals_are_protected() -> None:
    root = _root()
    user = User.objects.create_user(username="protected-user")
    group = Group.objects.create(name="protected-group")
    user_grant = RootGrant.objects.create(root=root, user=user, permissions=1)
    group_grant = RootGrant.objects.create(root=root, group=group, permissions=1)

    with pytest.raises(PermissionError, match="service"):
        root.delete()
    with pytest.raises(PermissionError, match="service"):
        Root.objects.filter(pk=root.pk).delete()
    with pytest.raises(PermissionError, match="service"):
        user_grant.delete()
    with pytest.raises(PermissionError, match="service"):
        RootGrant.objects.filter(pk=group_grant.pk).delete()
    with pytest.raises(ProtectedError):
        user.delete()
    with pytest.raises(ProtectedError):
        group.delete()


def test_root_and_grant_base_managers_cannot_bypass_guarded_deletion() -> None:
    orphan = _root(slot_id="orphan")
    root = _root(slot_id="protected")
    user = User.objects.create_user(username="base-manager-user")
    grant = RootGrant.objects.create(root=root, user=user, permissions=1)

    with pytest.raises(PermissionError, match="service"):
        Root._base_manager.filter(pk=orphan.pk).delete()
    with pytest.raises(PermissionError, match="service"):
        RootGrant._base_manager.filter(pk=grant.pk).delete()

    assert Root.objects.filter(pk=orphan.pk).exists()
    assert RootGrant.objects.filter(pk=grant.pk).exists()
