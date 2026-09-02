from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Protocol, cast

import pytest
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from aegis_apps.roots.selectors import (
    authorized_roots,
    clear_authorization_cache,
    effective_permissions,
)
from django.contrib.auth.models import Group
from django.db import connection, transaction
from django.db.models import F
from django.test.utils import CaptureQueriesContext

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


class _AuthorizedRoot(Protocol):
    effective_permissions: int


@pytest.fixture(autouse=True)
def _isolated_authorization_cache() -> Iterator[None]:
    clear_authorization_cache()
    yield
    clear_authorization_cache()


def _root(
    *,
    slot_id: str,
    display_name: str | None = None,
    active: bool = True,
    root_id: uuid.UUID | None = None,
) -> Root:
    values: dict[str, object] = {
        "slot_id": slot_id,
        "display_name": display_name or slot_id.title(),
        "mode": Root.Mode.READ_ONLY,
        "active": active,
    }
    if root_id is not None:
        values["id"] = root_id
    return Root.objects.create(**values)


def test_direct_and_multiple_current_group_grants_union_with_zero_mask() -> None:
    root = _root(slot_id="photos")
    groups = [Group.objects.create(name=f"group-{index}") for index in range(3)]
    user = User.objects.create_superuser(username="union-superuser")
    user.groups.add(*groups)
    RootGrant.objects.create(root=root, user=user, permissions=Permission.EXPORT)
    RootGrant.objects.create(root=root, group=groups[0], permissions=Permission.BROWSE)
    RootGrant.objects.create(root=root, group=groups[1], permissions=Permission.PREVIEW)
    RootGrant.objects.create(root=root, group=groups[2], permissions=0)

    assert effective_permissions(user_id=user.id, root_id=root.id) == (
        Permission.BROWSE | Permission.PREVIEW | Permission.EXPORT
    )

    unrelated = _root(slot_id="unrelated")
    assert effective_permissions(user_id=user.id, root_id=unrelated.id) == Permission(0)


def test_effective_permissions_returns_zero_for_inactive_or_missing_subjects() -> None:
    root = _root(slot_id="photos")
    inactive_root = _root(slot_id="inactive-root", active=False)
    active_user = User.objects.create_user(username="active-user")
    inactive_user = User.objects.create_user(username="inactive-user", is_active=False)
    RootGrant.objects.create(root=root, user=inactive_user, permissions=255)
    RootGrant.objects.create(root=inactive_root, user=active_user, permissions=255)

    assert effective_permissions(user_id=inactive_user.id, root_id=root.id) == Permission(0)
    assert effective_permissions(user_id=active_user.id, root_id=inactive_root.id) == Permission(0)
    assert effective_permissions(user_id=uuid.uuid4(), root_id=root.id) == Permission(0)
    assert effective_permissions(user_id=active_user.id, root_id=uuid.uuid4()) == Permission(0)


def test_effective_permissions_cache_is_epoch_keyed_and_uses_postgresql_bit_or() -> None:
    root = _root(slot_id="photos")
    user = User.objects.create_user(username="cached-user")
    grant = RootGrant.objects.create(root=root, user=user, permissions=Permission.BROWSE)

    with CaptureQueriesContext(connection) as captured:
        assert effective_permissions(user_id=user.id, root_id=root.id) == Permission.BROWSE
        assert effective_permissions(user_id=user.id, root_id=root.id) == Permission.BROWSE

    assert sum("BIT_OR" in query["sql"].upper() for query in captured.captured_queries) == 1

    RootGrant.objects.filter(pk=grant.pk).update(permissions=Permission.PREVIEW)
    Root.objects.filter(pk=root.pk).update(authorization_epoch=F("authorization_epoch") + 1)
    assert effective_permissions(user_id=user.id, root_id=root.id) == Permission.PREVIEW


def test_effective_permissions_never_reads_or_populates_cache_inside_atomic_block() -> None:
    root = _root(slot_id="photos")
    user = User.objects.create_user(username="transaction-user")
    grant = RootGrant.objects.create(root=root, user=user, permissions=Permission.BROWSE)

    class RollBack(RuntimeError):
        pass

    with pytest.raises(RollBack), transaction.atomic():
        RootGrant.objects.filter(pk=grant.pk).update(permissions=Permission.PREVIEW)
        assert effective_permissions(user_id=user.id, root_id=root.id) == Permission.PREVIEW
        raise RollBack

    assert effective_permissions(user_id=user.id, root_id=root.id) == Permission.BROWSE


def test_authorized_roots_filters_and_orders_in_postgresql() -> None:
    user = User.objects.create_user(username="listed-user")
    group = Group.objects.create(name="listed-group")
    user.groups.add(group)
    beta = _root(slot_id="beta", display_name="Beta")
    alpha_later = _root(
        slot_id="alpha-later",
        display_name="alpha",
        root_id=uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff"),
    )
    alpha_first = _root(
        slot_id="alpha-first",
        display_name="Alpha",
        root_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
    )
    outside_manifest = _root(slot_id="outside", display_name="Outside")
    preview_only = _root(slot_id="preview", display_name="Preview")
    inactive = _root(slot_id="inactive", display_name="Inactive", active=False)
    RootGrant.objects.create(root=beta, user=user, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=beta, group=group, permissions=Permission.PREVIEW)
    RootGrant.objects.create(root=alpha_later, group=group, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=alpha_first, user=user, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=outside_manifest, user=user, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=preview_only, user=user, permissions=Permission.PREVIEW)
    RootGrant.objects.create(root=inactive, user=user, permissions=Permission.BROWSE)

    roots = list(
        authorized_roots(
            user_id=user.id,
            active_manifest_slot_ids=("alpha-first", "alpha-later", "beta", "preview", "inactive"),
        )
    )

    assert [root.id for root in roots] == [alpha_first.id, alpha_later.id, beta.id]
    assert [Permission(cast(_AuthorizedRoot, root).effective_permissions) for root in roots] == [
        Permission.BROWSE,
        Permission.BROWSE,
        Permission.BROWSE | Permission.PREVIEW,
    ]

    User.objects.filter(pk=user.pk).update(is_active=False)
    assert list(
        authorized_roots(
            user_id=user.id,
            active_manifest_slot_ids=("alpha-first", "alpha-later", "beta"),
        )
    ) == []


def test_authorized_roots_compiles_manifest_membership_aggregate_gate_and_order_to_sql() -> None:
    user_id = uuid.uuid4()
    slot_ids = ("bound-one", "bound-two")
    queryset = authorized_roots(
        user_id=user_id,
        active_manifest_slot_ids=slot_ids,
    )

    sql, parameters = queryset.query.sql_with_params()
    normalized = " ".join(sql.upper().split())
    membership_table = User.groups.through._meta.db_table.upper()

    assert membership_table in normalized
    assert "BIT_OR(" in normalized
    assert "HAVING" in normalized
    assert "LOWER(" in normalized and "ORDER BY" in normalized
    assert "SLOT_ID" in normalized and " IN (" in normalized
    assert str(user_id) not in sql
    assert all(slot_id not in sql for slot_id in slot_ids)
    assert user_id in parameters
    assert all(slot_id in parameters for slot_id in slot_ids)


def test_authorized_roots_rejects_unbounded_or_unvalidated_slot_input() -> None:
    user_id = uuid.uuid4()
    with pytest.raises(ValueError, match="manifest slot"):
        authorized_roots(
            user_id=user_id,
            active_manifest_slot_ids=tuple(f"slot-{i}" for i in range(129)),
        )
    with pytest.raises(ValueError, match="manifest slot"):
        authorized_roots(user_id=user_id, active_manifest_slot_ids=("unsafe/path",))
