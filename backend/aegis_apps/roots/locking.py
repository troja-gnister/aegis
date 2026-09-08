from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Final

from django.contrib.auth.models import Group

from aegis_apps.identity.models import User

from .models import Root

_GROUP_PHASE: Final = 1
_ROOT_PHASE: Final = 2
_USER_PHASE: Final = 3


class AuthorizationLocks:
    """Acquire authorization rows in the only supported deadlock-free order.

    A scope locks all affected groups first, then roots, then users. Each phase
    sorts and de-duplicates its keys. Root-only paths begin at the root phase;
    no path may return to an earlier phase. PostgreSQL row locks remain scoped
    to the caller's transaction, so unrelated authorization scopes proceed in
    parallel.
    """

    def __init__(self) -> None:
        self._phase = 0

    def _enter(self, phase: int) -> None:
        if phase <= self._phase:
            raise RuntimeError("authorization lock phases must be acquired exactly once in order")
        self._phase = phase

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
