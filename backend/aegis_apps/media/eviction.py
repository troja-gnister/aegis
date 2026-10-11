"""Derivative eviction under the cache budget (spec M4).

*Evictable bytes* are the bytes of every ready preview and transcode (``evictable``) plus
every ``stale`` derivative of any kind (an older fingerprint, or a parked asset). Thumbnails
and posters that are current are never counted and never evicted.

When the evictable bytes exceed ``AEGIS_DERIVATIVES_CACHE_BUDGET_GB``, eviction deletes, in
this order, until the total is at most :data:`LOW_WATER_RATIO` of the budget:

1. stale derivatives, oldest first (they can never be served again);
2. ready previews and transcodes, least recently used first (``last_access_at``, which the
   delivery endpoint touches at most once an hour).

Each deletion takes the key's advisory lock (session-level, non-blocking), deletes the row
and commits, and only then unlinks the file: the row goes first, so nothing ever names a
missing file, and the lock keeps a concurrent publication of the same key from having its
new file unlinked. A crash between the commit and the unlink leaves an orphan file, which
``recovery.py`` removes. A row touched or republished since it was selected is skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from django.db import transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce

from .enums import DerivativeState
from .models import MediaDerivative
from .publish import DerivativeStore, try_key_lock

LOW_WATER_RATIO: Final = 0.95
MAX_EVICTIONS_PER_RUN: Final = 200
_FIELDS: Final = ("id", "key", "extension", "state", "bytes", "last_access_at")


@dataclass(frozen=True, slots=True)
class EvictionReport:
    used_bytes: int
    deleted: int
    freed_bytes: int


def _sum(query: Any) -> int:
    total = query.aggregate(total=Coalesce(Sum("bytes"), 0))["total"]
    return int(total)


def evictable_bytes() -> int:
    ready = MediaDerivative.objects.filter(evictable=True, state=DerivativeState.READY)
    stale = MediaDerivative.objects.filter(state=DerivativeState.STALE)
    return _sum(ready) + _sum(stale)


def _candidates(limit: int) -> list[dict[str, Any]]:
    stale = list(
        MediaDerivative.objects.filter(state=DerivativeState.STALE)
        .order_by("created_at", "id")
        .values(*_FIELDS)[:limit]
    )
    remaining = limit - len(stale)
    if remaining <= 0:
        return stale
    ready = list(
        MediaDerivative.objects.filter(evictable=True, state=DerivativeState.READY)
        .order_by("last_access_at", "id")
        .values(*_FIELDS)[:remaining]
    )
    return stale + ready


def _still_evictable(candidate: dict[str, Any]) -> Q:
    if candidate["state"] == DerivativeState.STALE:
        return Q(id=candidate["id"], key=candidate["key"], state=DerivativeState.STALE)
    return Q(
        id=candidate["id"],
        key=candidate["key"],
        state=DerivativeState.READY,
        evictable=True,
        last_access_at=candidate["last_access_at"],
    )


def evict(
    store: DerivativeStore,
    *,
    budget_bytes: int,
    limit: int = MAX_EVICTIONS_PER_RUN,
) -> EvictionReport:
    """Delete stale, then least recently used evictable, derivatives above the budget."""
    if type(budget_bytes) is not int or budget_bytes < 1:
        raise ValueError("invalid cache budget")
    if type(limit) is not int or not 1 <= limit <= 10_000:
        raise ValueError("invalid eviction limit")
    used = evictable_bytes()
    if used <= budget_bytes:
        return EvictionReport(used, 0, 0)
    target = int(budget_bytes * LOW_WATER_RATIO)
    deleted = freed = 0
    for candidate in _candidates(limit):
        if used - freed <= target:
            break
        with try_key_lock(candidate["key"]) as held:
            if not held:
                continue
            with transaction.atomic():
                removed, _ = MediaDerivative.objects.filter(_still_evictable(candidate)).delete()
            if not removed:
                continue
            store.remove(candidate["key"], candidate["extension"])
        deleted += 1
        freed += int(candidate["bytes"] or 0)
    return EvictionReport(used, deleted, freed)
