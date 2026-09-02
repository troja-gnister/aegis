from __future__ import annotations

import uuid
from collections import OrderedDict
from collections.abc import Collection
from threading import RLock

from aegisctl.mounts import MAX_SLOTS, SLOT_ID_RE
from django.contrib.postgres.aggregates import BitOr
from django.db import connection, models
from django.db.models import Exists, F, Q
from django.db.models.functions import Lower

from aegis_apps.identity.models import User

from .models import Root, RootGrant
from .permissions import Permission

_MAX_CACHE_ENTRIES = 4096
_CacheKey = tuple[uuid.UUID, uuid.UUID, int, int]
_decision_cache: OrderedDict[_CacheKey, Permission] = OrderedDict()
_cache_lock = RLock()


def clear_authorization_cache() -> None:
    with _cache_lock:
        _decision_cache.clear()


def invalidate_authorization_cache(
    *,
    user_ids: Collection[uuid.UUID] = (),
    root_ids: Collection[uuid.UUID] = (),
) -> None:
    users = frozenset(user_ids)
    roots = frozenset(root_ids)
    if not users and not roots:
        return
    with _cache_lock:
        for key in tuple(_decision_cache):
            if key[0] in users or key[1] in roots:
                _decision_cache.pop(key, None)


def _cache_get(key: _CacheKey) -> Permission | None:
    with _cache_lock:
        value = _decision_cache.get(key)
        if value is not None:
            _decision_cache.move_to_end(key)
        return value


def _cache_set(key: _CacheKey, value: Permission) -> None:
    with _cache_lock:
        _decision_cache[key] = value
        _decision_cache.move_to_end(key)
        while len(_decision_cache) > _MAX_CACHE_ENTRIES:
            _decision_cache.popitem(last=False)


def _authorization_epochs(
    *, user_id: uuid.UUID, root_id: uuid.UUID
) -> tuple[int, int] | None:
    user_epoch = (
        User.objects.filter(pk=user_id, is_active=True)
        .values_list("authorization_epoch", flat=True)
        .first()
    )
    if user_epoch is None:
        return None
    root_epoch = (
        Root.objects.filter(pk=root_id, active=True)
        .values_list("authorization_epoch", flat=True)
        .first()
    )
    if root_epoch is None:
        return None
    return user_epoch, root_epoch


def _database_permissions(*, user_id: uuid.UUID, root_id: uuid.UUID) -> Permission:
    active_user = User.objects.filter(pk=user_id, is_active=True)
    result = (
        RootGrant.objects.filter(root_id=root_id, root__active=True)
        .filter(Exists(active_user))
        .filter(Q(user_id=user_id) | Q(group__user=user_id))
        .aggregate(mask=BitOr("permissions"))["mask"]
    )
    return Permission(0 if result is None else result)


def effective_permissions(*, user_id: uuid.UUID, root_id: uuid.UUID) -> Permission:
    epochs = _authorization_epochs(user_id=user_id, root_id=root_id)
    if epochs is None:
        return Permission(0)
    user_epoch, root_epoch = epochs
    key = (user_id, root_id, user_epoch, root_epoch)
    cacheable = not connection.in_atomic_block
    if cacheable:
        cached = _cache_get(key)
        if cached is not None:
            return cached
    result = _database_permissions(user_id=user_id, root_id=root_id)
    if cacheable:
        _cache_set(key, result)
    return result


def _validated_manifest_slots(values: Collection[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or len(values) > MAX_SLOTS:
        raise ValueError("invalid manifest slot set")
    slots = tuple(dict.fromkeys(values))
    if any(not isinstance(value, str) or SLOT_ID_RE.fullmatch(value) is None for value in slots):
        raise ValueError("invalid manifest slot set")
    return slots


def authorized_roots(
    *,
    user_id: uuid.UUID,
    active_manifest_slot_ids: Collection[str],
) -> models.QuerySet[Root]:
    slots = _validated_manifest_slots(active_manifest_slot_ids)
    if not slots:
        return Root.objects.none()
    active_user = User.objects.filter(pk=user_id, is_active=True)
    principal = Q(grants__user_id=user_id) | Q(grants__group__user=user_id)
    return (
        Root.objects.filter(active=True, slot_id__in=slots)
        .filter(Exists(active_user))
        .filter(principal)
        .annotate(effective_permissions=BitOr("grants__permissions"))
        .annotate(
            _browse_mask=F("effective_permissions").bitand(int(Permission.BROWSE))
        )
        .filter(_browse_mask=int(Permission.BROWSE))
        .order_by(Lower("display_name"), "id")
    )
