from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.identity.admin_services import set_user_active
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from aegis_apps.roots.selectors import clear_authorization_cache, effective_permissions
from aegis_apps.roots.signals import (
    suppress_membership_epoch_updates_for_identity_admin,
)
from django.contrib.auth.models import Group
from django.contrib.auth.models import Permission as DjangoPermission
from django.db import transaction
from django.test import Client
from django.urls import reverse

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
REQUEST_ID = "membership_request_1234"


@pytest.fixture(autouse=True)
def _clear_decision_cache() -> Iterator[None]:
    clear_authorization_cache()
    yield
    clear_authorization_cache()


def _root(slot_id: str) -> Root:
    return Root.objects.create(
        slot_id=slot_id,
        display_name=slot_id.title(),
        mode=Root.Mode.READ_ONLY,
        active=True,
    )


def _epochs(*objects: User | Root) -> list[int]:
    values: list[int] = []
    for item in objects:
        item.refresh_from_db()
        values.append(item.authorization_epoch)
    return values


def _admin_client() -> Client:
    actor = User.objects.create_superuser(
        username="composite-admin-actor",
        password="Composite-admin-test-731!",
    )
    client = Client()
    client.force_login(actor)
    return client


def _user_change_data(user: User, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "is_active": "on" if user.is_active else "",
        "is_staff": "on" if user.is_staff else "",
        "is_superuser": "on" if user.is_superuser else "",
        "groups": [str(group_id) for group_id in user.groups.values_list("pk", flat=True)],
        "user_permissions": [
            str(permission_id)
            for permission_id in user.user_permissions.values_list("pk", flat=True)
        ],
        "date_joined_0": user.date_joined.strftime("%Y-%m-%d"),
        "date_joined_1": user.date_joined.strftime("%H:%M:%S"),
        "_save": "Save",
    }
    values.update(overrides)
    return values


def test_user_side_membership_add_remove_clear_deduplicates_users_and_roots() -> None:
    user = User.objects.create_user(username="user-side-member")
    first_group = Group.objects.create(name="user-side-first")
    second_group = Group.objects.create(name="user-side-second")
    first_root = _root("first-root")
    second_root = _root("second-root")
    RootGrant.objects.create(root=first_root, group=first_group, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=first_root, group=second_group, permissions=Permission.PREVIEW)
    RootGrant.objects.create(root=second_root, group=second_group, permissions=Permission.BROWSE)

    user.groups.add(first_group, second_group)
    assert _epochs(user, first_root, second_root) == [1, 1, 1]

    user.groups.add(first_group)
    assert _epochs(user, first_root, second_root) == [1, 1, 1]

    user.groups.remove(first_group)
    assert _epochs(user, first_root, second_root) == [2, 2, 1]

    user.groups.clear()
    assert _epochs(user, first_root, second_root) == [3, 3, 2]


def test_group_side_membership_add_remove_clear_tracks_only_changed_members() -> None:
    first = User.objects.create_user(username="group-side-first")
    second = User.objects.create_user(username="group-side-second")
    group = Group.objects.create(name="group-side")
    root = _root("group-side-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    group.user_set.add(first, second)
    assert _epochs(first, second, root) == [1, 1, 1]

    group.user_set.remove(first)
    assert _epochs(first, second, root) == [2, 1, 2]

    group.user_set.remove(first)
    assert _epochs(first, second, root) == [2, 1, 2]

    group.user_set.clear()
    assert _epochs(first, second, root) == [2, 2, 3]


def test_membership_changes_make_cached_decisions_unreachable_and_revoke_session() -> None:
    user = User.objects.create_user(username="cached-member", password="test-password")
    group = Group.objects.create(name="cached-group")
    root = _root("cached-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)
    assert effective_permissions(user_id=user.id, root_id=root.id) == Permission(0)
    client = Client()
    client.force_login(user)
    session_key = client.session.session_key

    user.groups.add(group)

    assert effective_permissions(user_id=user.id, root_id=root.id) == Permission.BROWSE
    response = client.get("/health/live")
    assert response.status_code == 200
    assert client.session.session_key != session_key
    assert AuditEvent.objects.get(event_type="auth.session.revoked").actor is None

    user.groups.remove(group)
    assert effective_permissions(user_id=user.id, root_id=root.id) == Permission(0)


def test_user_active_change_advances_each_reachable_root_once_and_revokes_session() -> None:
    actor = User.objects.create_superuser(username="active-change-actor")
    user = User.objects.create_user(username="active-change-user", password="test-password")
    group = Group.objects.create(name="active-change-group")
    user.groups.add(group)
    first_root = _root("active-first-root")
    second_root = _root("active-second-root")
    RootGrant.objects.create(root=first_root, user=user, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=first_root, group=group, permissions=Permission.PREVIEW)
    RootGrant.objects.create(root=second_root, group=group, permissions=Permission.BROWSE)
    User.objects.filter(pk=user.pk).update(authorization_epoch=0)
    Root.objects.filter(pk__in=(first_root.pk, second_root.pk)).update(authorization_epoch=0)
    clear_authorization_cache()
    client = Client()
    user.refresh_from_db()
    client.force_login(user)
    session_key = client.session.session_key

    changed = set_user_active(
        actor=actor,
        user_id=user.id,
        active=False,
        request_id=REQUEST_ID,
    )
    repeated = set_user_active(
        actor=actor,
        user_id=user.id,
        active=False,
        request_id=REQUEST_ID,
    )

    assert changed.id == repeated.id == user.id
    assert _epochs(user, first_root, second_root) == [1, 1, 1]
    response = client.get("/health/live")
    assert response.status_code == 200
    assert client.session.session_key != session_key
    event_types = AuditEvent.objects.order_by("occurred_at").values_list(
        "event_type", flat=True
    )
    assert list(event_types) == [
        "identity.user.changed",
        "auth.session.revoked",
    ]


def test_user_admin_coalesces_group_add_with_active_change_per_user_and_root() -> None:
    client = _admin_client()
    user = User.objects.create_user(username="combined-active-user", is_active=True)
    group = Group.objects.create(name="combined-active-group")
    root = _root("combined-active-root")
    RootGrant.objects.create(root=root, user=user, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    response = client.post(
        reverse("admin:identity_user_change", args=[user.pk]),
        _user_change_data(user, is_active="", groups=[str(group.pk)]),
        headers={"X-Request-ID": REQUEST_ID},
    )

    user.refresh_from_db()
    assert response.status_code == 302
    assert user.is_active is False
    assert set(user.groups.values_list("pk", flat=True)) == {group.pk}
    assert _epochs(user, root) == [1, 1]
    assert list(AuditEvent.objects.values_list("event_type", flat=True)) == [
        "identity.user.changed"
    ]


def test_user_admin_deduplicates_direct_current_removed_and_new_roots() -> None:
    client = _admin_client()
    user = User.objects.create_user(username="complete-root-delta-user", is_active=True)
    retained_group = Group.objects.create(name="complete-root-delta-retained")
    removed_group = Group.objects.create(name="complete-root-delta-removed")
    added_group = Group.objects.create(name="complete-root-delta-added")
    user.groups.add(retained_group, removed_group)
    User.objects.filter(pk=user.pk).update(authorization_epoch=0)

    direct_root = _root("complete-delta-direct")
    retained_root = _root("complete-delta-retained")
    removed_root = _root("complete-delta-removed")
    added_root = _root("complete-delta-added")
    shared_root = _root("complete-delta-shared")
    RootGrant.objects.create(
        root=direct_root, user=user, permissions=Permission.BROWSE
    )
    RootGrant.objects.create(
        root=retained_root, group=retained_group, permissions=Permission.BROWSE
    )
    RootGrant.objects.create(
        root=removed_root, group=removed_group, permissions=Permission.BROWSE
    )
    RootGrant.objects.create(
        root=added_root, group=added_group, permissions=Permission.BROWSE
    )
    for group in (retained_group, removed_group, added_group):
        RootGrant.objects.create(
            root=shared_root,
            group=group,
            permissions=Permission.BROWSE,
        )

    response = client.post(
        reverse("admin:identity_user_change", args=[user.pk]),
        _user_change_data(
            user,
            is_active="",
            groups=[str(retained_group.pk), str(added_group.pk)],
        ),
        headers={"X-Request-ID": REQUEST_ID},
    )

    user.refresh_from_db()
    assert response.status_code == 302
    assert user.is_active is False
    assert set(user.groups.values_list("pk", flat=True)) == {
        retained_group.pk,
        added_group.pk,
    }
    assert _epochs(
        user,
        direct_root,
        retained_root,
        removed_root,
        added_root,
        shared_root,
    ) == [1, 1, 1, 1, 1, 1]
    assert list(AuditEvent.objects.values_list("event_type", flat=True)) == [
        "identity.user.changed"
    ]


def test_user_admin_coalesces_group_add_with_direct_permission_change() -> None:
    client = _admin_client()
    user = User.objects.create_user(username="combined-permission-user")
    group = Group.objects.create(name="combined-permission-group")
    permission = DjangoPermission.objects.order_by("pk").first()
    assert permission is not None
    root = _root("combined-permission-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    response = client.post(
        reverse("admin:identity_user_change", args=[user.pk]),
        _user_change_data(
            user,
            groups=[str(group.pk)],
            user_permissions=[str(permission.pk)],
        ),
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert response.status_code == 302
    assert set(user.groups.values_list("pk", flat=True)) == {group.pk}
    assert set(user.user_permissions.values_list("pk", flat=True)) == {permission.pk}
    assert _epochs(user, root) == [1, 1]
    assert list(AuditEvent.objects.values_list("event_type", flat=True)) == [
        "identity.user.changed"
    ]


def test_user_admin_group_replacement_coalesces_shared_user_and_root() -> None:
    client = _admin_client()
    user = User.objects.create_user(username="replacement-user")
    first_group = Group.objects.create(name="replacement-first-group")
    second_group = Group.objects.create(name="replacement-second-group")
    user.groups.add(first_group)
    User.objects.filter(pk=user.pk).update(authorization_epoch=0)
    root = _root("replacement-root")
    RootGrant.objects.create(root=root, group=first_group, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=root, group=second_group, permissions=Permission.BROWSE)

    response = client.post(
        reverse("admin:identity_user_change", args=[user.pk]),
        _user_change_data(user, groups=[str(second_group.pk)]),
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert response.status_code == 302
    assert set(user.groups.values_list("pk", flat=True)) == {second_group.pk}
    assert _epochs(user, root) == [1, 1]
    assert list(AuditEvent.objects.values_list("event_type", flat=True)) == [
        "identity.user.changed"
    ]


def test_group_admin_combined_permission_and_member_replacement_coalesces_root() -> None:
    client = _admin_client()
    first = User.objects.create_user(username="group-replacement-first")
    retained = User.objects.create_user(username="group-replacement-retained")
    second = User.objects.create_user(username="group-replacement-second")
    group = Group.objects.create(name="group-replacement")
    group.user_set.add(first, retained)
    User.objects.filter(pk__in=(first.pk, retained.pk)).update(authorization_epoch=0)
    permission = DjangoPermission.objects.order_by("pk").first()
    assert permission is not None
    root = _root("group-replacement-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    response = client.post(
        reverse("admin:auth_group_change", args=[group.pk]),
        {
            "name": group.name,
            "permissions": [str(permission.pk)],
            "members": [str(retained.pk), str(second.pk)],
            "_save": "Save",
        },
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert response.status_code == 302
    assert set(group.user_set.values_list("pk", flat=True)) == {retained.pk, second.pk}
    assert set(group.permissions.values_list("pk", flat=True)) == {permission.pk}
    assert _epochs(first, retained, second, root) == [1, 1, 1, 1]
    assert list(AuditEvent.objects.values_list("event_type", flat=True)) == [
        "identity.group.changed"
    ]


def test_membership_epoch_suppression_is_nested_and_restores_raw_signals() -> None:
    user = User.objects.create_user(username="nested-suppression-user")
    group = Group.objects.create(name="nested-suppression-group")
    root = _root("nested-suppression-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    with suppress_membership_epoch_updates_for_identity_admin():
        with suppress_membership_epoch_updates_for_identity_admin():
            user.groups.add(group)
        assert _epochs(user, root) == [0, 0]

    user.groups.remove(group)

    assert not user.groups.filter(pk=group.pk).exists()
    assert _epochs(user, root) == [1, 1]


def test_caught_membership_add_savepoint_rollback_does_not_advance_epochs() -> None:
    user = User.objects.create_user(username="savepoint-add-user")
    group = Group.objects.create(name="savepoint-add-group")
    root = _root("savepoint-add-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    class RollBackSavepoint(RuntimeError):
        pass

    with transaction.atomic():
        try:
            with transaction.atomic():
                user.groups.add(group)
                raise RollBackSavepoint
        except RollBackSavepoint:
            pass

    assert not user.groups.filter(pk=group.pk).exists()
    assert _epochs(user, root) == [0, 0]


def test_caught_membership_add_savepoint_rollback_does_not_join_valid_batch() -> None:
    retained = User.objects.create_user(username="savepoint-retained-user")
    rolled_back = User.objects.create_user(username="savepoint-rolled-back-user")
    group = Group.objects.create(name="savepoint-mixed-group")
    root = _root("savepoint-mixed-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    class RollBackSavepoint(RuntimeError):
        pass

    with transaction.atomic():
        retained.groups.add(group)
        try:
            with transaction.atomic():
                rolled_back.groups.add(group)
                raise RollBackSavepoint
        except RollBackSavepoint:
            pass

    assert retained.groups.filter(pk=group.pk).exists()
    assert not rolled_back.groups.filter(pk=group.pk).exists()
    assert _epochs(retained, rolled_back, root) == [1, 0, 1]


def test_caught_membership_remove_savepoint_rollback_does_not_advance_epochs() -> None:
    user = User.objects.create_user(username="savepoint-remove-user")
    group = Group.objects.create(name="savepoint-remove-group")
    root = _root("savepoint-remove-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)
    user.groups.add(group)
    User.objects.filter(pk=user.pk).update(authorization_epoch=0)
    Root.objects.filter(pk=root.pk).update(authorization_epoch=0)

    class RollBackSavepoint(RuntimeError):
        pass

    with transaction.atomic():
        try:
            with transaction.atomic():
                user.groups.remove(group)
                raise RollBackSavepoint
        except RollBackSavepoint:
            pass

    assert user.groups.filter(pk=group.pk).exists()
    assert _epochs(user, root) == [0, 0]


def test_membership_suppression_rollback_resets_context_and_preserves_raw_signals() -> None:
    user = User.objects.create_user(username="coalesced-rollback-user")
    group = Group.objects.create(name="coalesced-rollback-group")
    root = _root("coalesced-rollback-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    class RollBackOuter(RuntimeError):
        pass

    with (
        pytest.raises(RollBackOuter),
        transaction.atomic(),
        suppress_membership_epoch_updates_for_identity_admin(),
    ):
        user.groups.add(group)
        assert _epochs(user, root) == [0, 0]
        raise RollBackOuter

    assert not user.groups.filter(pk=group.pk).exists()
    assert _epochs(user, root) == [0, 0]

    user.groups.add(group)

    assert user.groups.filter(pk=group.pk).exists()
    assert _epochs(user, root) == [1, 1]


def test_raw_membership_epoch_and_cache_invalidation_follow_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User.objects.create_user(username="coalesced-commit-user")
    group = Group.objects.create(name="coalesced-commit-group")
    root = _root("coalesced-commit-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)
    invalidations: list[tuple[frozenset[object], frozenset[object]]] = []

    def record_invalidation(
        *, user_ids: frozenset[object], root_ids: frozenset[object]
    ) -> None:
        invalidations.append((user_ids, root_ids))

    monkeypatch.setattr(
        "aegis_apps.roots.services.invalidate_authorization_cache",
        record_invalidation,
    )

    class RollBackTransaction(RuntimeError):
        pass

    with pytest.raises(RollBackTransaction), transaction.atomic():
        user.groups.add(group)
        assert _epochs(user, root) == [1, 1]
        assert invalidations == []
        raise RollBackTransaction

    assert not user.groups.filter(pk=group.pk).exists()
    assert _epochs(user, root) == [0, 0]
    assert invalidations == []

    with transaction.atomic():
        user.groups.add(group)
        assert invalidations == []

    assert user.groups.filter(pk=group.pk).exists()
    assert _epochs(user, root) == [1, 1]
    assert invalidations == [
        (frozenset((user.id,)), frozenset((root.id,)))
    ]


def test_user_admin_audit_failure_rolls_back_composite_change_and_epochs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _admin_client()
    user = User.objects.create_user(username="composite-rollback-user", is_active=True)
    group = Group.objects.create(name="composite-rollback-group")
    root = _root("composite-rollback-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    def fail_audit(**_values: object) -> None:
        raise RuntimeError("simulated composite audit failure")

    monkeypatch.setattr("aegis_apps.identity.admin_services.record_event", fail_audit)

    with pytest.raises(RuntimeError, match="simulated composite audit failure"):
        client.post(
            reverse("admin:identity_user_change", args=[user.pk]),
            _user_change_data(user, is_active="", groups=[str(group.pk)]),
            headers={"X-Request-ID": REQUEST_ID},
        )

    user.refresh_from_db()
    assert user.is_active is True
    assert not user.groups.filter(pk=group.pk).exists()
    assert _epochs(user, root) == [0, 0]
    assert AuditEvent.objects.count() == 0


def test_reverse_clear_state_is_isolated_between_model_instances() -> None:
    users = [User.objects.create_user(username=f"clear-user-{index}") for index in range(2)]
    groups = [Group.objects.create(name=f"clear-group-{index}") for index in range(2)]
    roots = [_root(f"clear-root-{index}") for index in range(2)]
    for group, root, user in zip(groups, roots, users, strict=True):
        RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)
        group.user_set.add(user)

    for group in groups:
        group.user_set.clear()

    assert _epochs(*users, *roots) == [2, 2, 2, 2]


def test_membership_accepts_uuid_primary_keys_without_integer_coercion() -> None:
    user = User.objects.create_user(username="uuid-member")
    group = Group.objects.create(name="uuid-group")
    root = _root("uuid-root")
    RootGrant.objects.create(root=root, group=group, permissions=Permission.BROWSE)

    group.user_set.add(uuid.UUID(str(user.pk)))  # type: ignore[arg-type]

    assert _epochs(user, root) == [1, 1]
