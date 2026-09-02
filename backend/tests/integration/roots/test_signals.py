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
from django.contrib.auth.models import Group
from django.test import Client

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
