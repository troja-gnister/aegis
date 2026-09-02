from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.identity.models import GroupIdentity, User
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from aegis_apps.roots.selectors import clear_authorization_cache, effective_permissions
from aegis_apps.roots.services import (
    activate_root,
    create_root,
    deactivate_root,
    remove_grant,
    set_group_grant,
    set_user_grant,
    update_root,
)
from django.contrib.auth.models import Group

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
REQUEST_ID = "task9_service_request"


@pytest.fixture(autouse=True)
def _clear_decision_cache() -> Iterator[None]:
    clear_authorization_cache()
    yield
    clear_authorization_cache()


def _configure_manifest(
    *, path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "read_write"
) -> None:
    payload = {
        "version": 1,
        "generatedAt": "2026-09-02T12:34:56Z",
        "slots": [
            {
                "slotId": "photos",
                "containerPath": "/srv/aegis/roots/photos",
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


def _root() -> Root:
    return Root.objects.create(
        slot_id="photos",
        display_name="Photos",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )


def test_set_user_grant_is_idempotent_audited_and_invalidates_only_on_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    actor = User.objects.create_superuser(username="user-grant-actor")
    subject = User.objects.create_user(username="private-subject-name")
    root = _root()
    assert effective_permissions(user_id=subject.id, root_id=root.id) == Permission(0)

    created = set_user_grant(
        actor=actor,
        root_id=root.id,
        user_id=subject.id,
        permissions=Permission.BROWSE,
        request_id=REQUEST_ID,
    )
    repeated = set_user_grant(
        actor=actor,
        root_id=root.id,
        user_id=subject.id,
        permissions=Permission.BROWSE,
        request_id=REQUEST_ID,
    )

    assert repeated.id == created.id
    root.refresh_from_db()
    subject.refresh_from_db()
    assert root.authorization_epoch == subject.authorization_epoch == 1
    assert effective_permissions(user_id=subject.id, root_id=root.id) == Permission.BROWSE
    event = AuditEvent.objects.get()
    assert event.event_type == "authorization.user.grant.changed"
    assert event.root_id == root.id
    assert event.object_id == subject.id
    assert event.metadata == {
        "subject_id": str(subject.id),
        "permissions": int(Permission.BROWSE),
    }
    assert subject.username not in str(event.metadata)
    assert root.slot_id not in str(event.metadata)

    changed = set_user_grant(
        actor=actor,
        root_id=root.id,
        user_id=subject.id,
        permissions=Permission.PREVIEW,
        request_id=REQUEST_ID,
    )
    root.refresh_from_db()
    subject.refresh_from_db()
    assert changed.id == created.id
    assert root.authorization_epoch == subject.authorization_epoch == 2
    assert AuditEvent.objects.count() == 2
    assert effective_permissions(user_id=subject.id, root_id=root.id) == Permission.PREVIEW


def test_set_group_grant_uses_opaque_identity_and_only_current_members(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    actor = User.objects.create_superuser(username="group-grant-actor")
    member = User.objects.create_user(username="current-member")
    nonmember = User.objects.create_user(username="not-a-member")
    group = Group.objects.create(name="private-group-name")
    member.groups.add(group)
    User.objects.filter(pk=member.pk).update(authorization_epoch=0)
    member.refresh_from_db()
    root = _root()

    grant = set_group_grant(
        actor=actor,
        root_id=root.id,
        group_id=group.id,
        permissions=Permission.BROWSE | Permission.PREVIEW,
        request_id=REQUEST_ID,
    )
    identity = GroupIdentity.objects.get(group=group)
    root.refresh_from_db()
    member.refresh_from_db()
    nonmember.refresh_from_db()

    assert grant.group == group
    assert root.authorization_epoch == member.authorization_epoch == 1
    assert nonmember.authorization_epoch == 0
    event = AuditEvent.objects.get()
    assert event.event_type == "authorization.group.grant.changed"
    assert event.object_id == identity.id
    assert event.metadata == {
        "subject_id": str(identity.id),
        "permissions": 3,
    }
    serialized = str(event.metadata)
    assert "group_id" not in event.metadata
    assert group.name not in serialized


def test_remove_grant_is_audited_once_and_exact_repeat_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    actor = User.objects.create_superuser(username="remove-actor")
    subject = User.objects.create_user(username="removed-subject")
    root = _root()
    grant = RootGrant.objects.create(
        root=root,
        user=subject,
        permissions=Permission.BROWSE,
    )

    removed = remove_grant(actor=actor, grant_id=grant.id, request_id=REQUEST_ID)
    repeated = remove_grant(actor=actor, grant_id=grant.id, request_id=REQUEST_ID)

    assert removed is not None and removed.id == grant.id
    assert repeated is None
    assert not RootGrant.objects.filter(pk=grant.pk).exists()
    root.refresh_from_db()
    subject.refresh_from_db()
    assert root.authorization_epoch == subject.authorization_epoch == 1
    event = AuditEvent.objects.get()
    assert event.event_type == "authorization.grant.removed"
    assert event.object_id == subject.id
    assert event.metadata == {"subject_id": str(subject.id), "permissions": 1}


def test_audit_failure_rolls_back_grant_epochs_and_cache_invalidation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    actor = User.objects.create_superuser(username="rollback-actor")
    subject = User.objects.create_user(username="rollback-subject")
    root = _root()
    assert effective_permissions(user_id=subject.id, root_id=root.id) == Permission(0)

    def fail_audit(**_values: object) -> None:
        raise RuntimeError("simulated root audit failure")

    monkeypatch.setattr("aegis_apps.roots.services.record_event", fail_audit)
    with pytest.raises(RuntimeError, match="simulated root audit failure"):
        set_user_grant(
            actor=actor,
            root_id=root.id,
            user_id=subject.id,
            permissions=Permission.BROWSE,
            request_id=REQUEST_ID,
        )

    root.refresh_from_db()
    subject.refresh_from_db()
    assert root.authorization_epoch == subject.authorization_epoch == 0
    assert RootGrant.objects.count() == 0
    assert AuditEvent.objects.count() == 0
    assert effective_permissions(user_id=subject.id, root_id=root.id) == Permission(0)


def test_root_services_validate_manifest_audit_and_advance_only_authorization_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    _configure_manifest(path=path, monkeypatch=monkeypatch)
    actor = User.objects.create_superuser(username="root-actor")
    direct = User.objects.create_user(username="direct-user")
    grouped = User.objects.create_user(username="group-user")
    both = User.objects.create_user(username="both-user")
    group = Group.objects.create(name="root-group")
    group.user_set.add(grouped, both)
    User.objects.filter(pk__in=(grouped.pk, both.pk)).update(authorization_epoch=0)

    root = create_root(
        actor=actor,
        slot_id="photos",
        display_name="Photos",
        mode=Root.Mode.READ_ONLY,
        active=False,
        request_id=REQUEST_ID,
    )
    RootGrant.objects.create(root=root, user=direct, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=root, user=both, permissions=Permission.PREVIEW)
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    renamed = update_root(
        actor=actor,
        root_id=root.id,
        display_name="Family Photos",
        request_id=REQUEST_ID,
    )
    assert renamed.display_name == "Family Photos"
    root.refresh_from_db()
    direct.refresh_from_db()
    grouped.refresh_from_db()
    both.refresh_from_db()
    assert root.authorization_epoch == 0
    assert [direct.authorization_epoch, grouped.authorization_epoch, both.authorization_epoch] == [
        0,
        0,
        0,
    ]

    changed = update_root(
        actor=actor,
        root_id=root.id,
        mode=Root.Mode.READ_WRITE,
        request_id=REQUEST_ID,
    )
    repeated = update_root(
        actor=actor,
        root_id=root.id,
        mode=Root.Mode.READ_WRITE,
        request_id=REQUEST_ID,
    )
    assert changed.id == repeated.id == root.id
    root.refresh_from_db()
    direct.refresh_from_db()
    grouped.refresh_from_db()
    both.refresh_from_db()
    assert root.authorization_epoch == 1
    assert [direct.authorization_epoch, grouped.authorization_epoch, both.authorization_epoch] == [
        1,
        1,
        1,
    ]

    activate_root(actor=actor, root_id=root.id, request_id=REQUEST_ID)
    deactivate_root(actor=actor, root_id=root.id, request_id=REQUEST_ID)
    root.refresh_from_db()
    direct.refresh_from_db()
    grouped.refresh_from_db()
    both.refresh_from_db()
    assert root.authorization_epoch == 3
    assert [direct.authorization_epoch, grouped.authorization_epoch, both.authorization_epoch] == [
        3,
        3,
        3,
    ]
    event_types = AuditEvent.objects.order_by("occurred_at").values_list(
        "event_type", flat=True
    )
    assert list(event_types) == [
        "authorization.root.created",
        "authorization.root.changed",
        "authorization.root.changed",
        "authorization.root.changed",
        "authorization.root.changed",
    ]
    assert all(
        event.metadata == {"root_id": str(root.id)} for event in AuditEvent.objects.all()
    )


def test_unconfigured_or_weaker_manifest_rejects_root_mutation_without_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actor = User.objects.create_superuser(username="invalid-manifest-actor")
    monkeypatch.delenv("AEGIS_MOUNT_MANIFEST", raising=False)
    monkeypatch.delenv("AEGIS_MOUNT_MANIFEST_SHA256", raising=False)
    with pytest.raises(ValueError, match="manifest"):
        create_root(
            actor=actor,
            slot_id="photos",
            display_name="Photos",
            mode=Root.Mode.READ_ONLY,
            request_id=REQUEST_ID,
        )

    _configure_manifest(
        path=tmp_path / "manifest.json",
        monkeypatch=monkeypatch,
        mode="read_only",
    )
    root = _root()
    with pytest.raises(ValueError, match="mode"):
        update_root(
            actor=actor,
            root_id=root.id,
            mode=Root.Mode.READ_WRITE,
            request_id=REQUEST_ID,
        )

    root.refresh_from_db()
    assert root.mode == Root.Mode.READ_ONLY
    assert AuditEvent.objects.count() == 0


@pytest.mark.parametrize("value", [True, False, 0, -1, "1"])
def test_set_group_grant_rejects_non_internal_positive_integer_group_id(
    value: object,
) -> None:
    actor = User.objects.create_superuser(username=f"bad-group-{value}")
    with pytest.raises(ValueError, match="group ID"):
        set_group_grant(
            actor=actor,
            root_id=Root().id,
            group_id=value,  # type: ignore[arg-type]
            permissions=Permission.BROWSE,
            request_id=REQUEST_ID,
        )
