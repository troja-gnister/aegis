from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable
from typing import Final

from django.contrib.auth.models import Group
from django.db import connection

from aegis_apps.identity.models import User

from .models import Root

_MEMBERSHIP_DISCOVERY_PHASE: Final = 1
_GROUP_PHASE: Final = 2
_ROOT_PHASE: Final = 3
_USER_PHASE: Final = 4
_MEMBERSHIP_DISCOVERY_LOCK_DOMAIN: Final = b"aegis.membership-discovery.v1\x00"


class MembershipDiscoveryChanged(RuntimeError):
    """The membership set changed after its advisory-lock discovery pass."""


def _membership_discovery_lock_key(user_id: uuid.UUID) -> int:
    digest = hashlib.sha256(
        _MEMBERSHIP_DISCOVERY_LOCK_DOMAIN + user_id.bytes
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class AuthorizationLocks:
    """Acquire authorization rows in the only supported deadlock-free order.

    Membership-changing scopes first take transaction-scoped per-user advisory
    discovery locks, then all affected groups, roots, and users. Each phase
    sorts and de-duplicates its keys. Other authorization paths begin at their
    earliest relevant row-lock phase; no path may return to an earlier phase.
    Unrelated user membership scopes therefore continue in parallel.
    """

    def __init__(self) -> None:
        self._phase = 0

    def _enter(self, phase: int) -> None:
        if phase <= self._phase:
            raise RuntimeError("authorization lock phases must be acquired exactly once in order")
        self._phase = phase

    def membership_users(
        self, user_ids: Iterable[uuid.UUID]
    ) -> tuple[uuid.UUID, ...]:
        self._enter(_MEMBERSHIP_DISCOVERY_PHASE)
        ids = tuple(sorted(set(user_ids)))
        if any(not isinstance(user_id, uuid.UUID) for user_id in ids):
            raise ValueError("membership users are invalid")
        if not ids:
            return ()
        if not connection.in_atomic_block:
            raise RuntimeError("membership discovery locks require a transaction")
        with connection.cursor() as cursor:
            for user_id in ids:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(%s)",
                    [_membership_discovery_lock_key(user_id)],
                )
        return ids

    def groups(self, group_ids: Iterable[int]) -> dict[int, Group]:
        self._enter(_GROUP_PHASE)
        ids = tuple(sorted(set(group_ids)))
        if not ids:
            return {}
        return {
            int(group.pk): group
            for group in Group.objects.select_for_update()
            .filter(pk__in=ids)
            .order_by("pk")
        }

    def roots(self, root_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Root]:
        self._enter(_ROOT_PHASE)
        ids = tuple(sorted(set(root_ids)))
        if not ids:
            return {}
        return {
            root.pk: root
            for root in Root.objects.select_for_update()
            .filter(pk__in=ids)
            .order_by("pk")
        }

    def users(self, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, User]:
        self._enter(_USER_PHASE)
        ids = tuple(sorted(set(user_ids)))
        if not ids:
            return {}
        return {
            user.pk: user
            for user in User.objects.select_for_update()
            .filter(pk__in=ids)
            .order_by("pk")
        }
