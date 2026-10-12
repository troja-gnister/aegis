"""Fenced leases on media assets for the background pipeline (spec M3).

The pattern is the operations ``Job`` lease (Task 2's models):

- a claim locks candidate rows with ``FOR UPDATE SKIP LOCKED`` (never a join: the worker may
  not lock ``roots_root``), increments ``attempt_token``, and sets ``lease_owner`` and
  ``lease_expires_at``. It also moves ``next_attempt_at`` to the lease expiry, so a leased row
  leaves the ``next_attempt_at <= now`` range of ``media_asset_work_idx`` and an abandoned
  lease becomes claimable exactly when it expires;
- every later write compares the entry, the token and the owner and requires an unexpired
  lease (:func:`fenced`), so a writer whose lease was lost, renewed by nobody or superseded by
  a fingerprint change (which increments the token too) changes nothing;
- a claim never touches ``attempts``: that counts consecutive failures for the current
  fingerprint and is changed only when a result is recorded.

Interactive assets (``lane = interactive``) are claimed first, by ``next_attempt_at`` then
entry. Background assets are claimed **newest file first**, so the top of the Photos timeline
fills early: per active root, the claim looks at the :data:`NEWEST_WINDOW` newest
(``mtime_ns``, then entry, descending) assets that need work and are not parked, through the
partial index ``media_asset_backfill_idx``, and takes the newest eligible ones across roots.
The window bounds each claim's cost whatever the root holds; only when the window is all
leased or backing off does the claim fall back to the oldest ``next_attempt_at`` (through
``media_asset_work_idx``), so no eligible asset is starved. A pristine asset's one job is a
backfill that probes and renders (``pipeline.py``), and a probed asset is claimable again at
once for its thumbnail or poster, so a library fills newest first, thumbnails included. Lease
lengths are bounded in code because the database has no cap on them.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from django.db import transaction
from django.db.models import Q

from .enums import Lane
from .models import ASSET_NEEDS_WORK, BACKFILL_CANDIDATE, MediaAsset

# Defined with the index whose predicate names it; discovery and the worker import it here.
from .models import PARKED_UNTIL as PARKED_UNTIL

DEFAULT_ASSET_LEASE_SECONDS: Final = 300
MAX_ASSET_LEASE_SECONDS: Final = 3_600
MAX_CLAIM_BATCH: Final = 32
# How many of a root's newest needs-work assets a background claim reads at most. While all
# of them are leased or backing off (for example a new folder of unreadable files), claims
# take the oldest queue time instead: the cost stays bounded and nothing starves, but the
# order is not newest first until those assets clear.
NEWEST_WINDOW: Final = 256

CLAIM_FIELDS: Final = (
    "entry_id", "root_id", "kind", "device", "inode", "size", "mtime_ns", "fingerprint",
    "probe_state", "thumbnail_state", "poster_state", "attempts", "attempt_token",
    "environment_failures", "duration_ms", "lane",
)


@dataclass(frozen=True, slots=True)
class AssetLease:
    entry_id: uuid.UUID
    attempt_token: int
    owner: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ClaimedAsset:
    """A leased asset and the columns the pipeline needs to build its next job."""

    lease: AssetLease
    row: dict[str, Any]


def lease_delta(seconds: int = DEFAULT_ASSET_LEASE_SECONDS) -> timedelta:
    if type(seconds) is not int or not 1 <= seconds <= MAX_ASSET_LEASE_SECONDS:
        raise ValueError("invalid asset lease duration")
    return timedelta(seconds=seconds)


def fenced(lease: AssetLease, *, now: datetime) -> Q:
    """The rows a lease holder may still write: same entry, token and owner, unexpired."""
    return Q(
        entry_id=lease.entry_id,
        attempt_token=lease.attempt_token,
        lease_owner=lease.owner,
        lease_expires_at__gt=now,
    )


def _claimable(now: datetime) -> Q:
    return Q(lease_expires_at__isnull=True) | Q(lease_expires_at__lte=now)


def _by_queue_time(
    lane: str, roots: Sequence[uuid.UUID], now: datetime, limit: int,
    exclude: Collection[uuid.UUID] = (),
) -> list[dict[str, Any]]:
    """Lock up to ``limit`` eligible assets of a lane, oldest ``next_attempt_at`` first."""
    query = (
        MediaAsset.objects.select_for_update(skip_locked=True, of=("self",))
        .filter(ASSET_NEEDS_WORK)
        .filter(lane=lane, next_attempt_at__lte=now, root_id__in=roots)
        .filter(_claimable(now))
    )
    if exclude:
        # Rows this transaction already locked: SKIP LOCKED does not skip our own locks.
        query = query.exclude(entry_id__in=list(exclude))
    return list(query.order_by("next_attempt_at", "entry_id").values(*CLAIM_FIELDS)[:limit])


def _newest(root_id: uuid.UUID, now: datetime, limit: int) -> list[dict[str, Any]]:
    """Lock up to ``limit`` of a root's newest eligible background assets.

    Only the root's :data:`NEWEST_WINDOW` newest candidates are read (an index range of
    ``media_asset_backfill_idx``); leased and backing-off assets among them are skipped.
    """
    window = (
        MediaAsset.objects.filter(BACKFILL_CANDIDATE)
        .filter(root_id=root_id)
        .order_by("-mtime_ns", "-entry_id")
        .values("entry_id")[:NEWEST_WINDOW]
    )
    return list(
        MediaAsset.objects.select_for_update(skip_locked=True, of=("self",))
        .filter(BACKFILL_CANDIDATE)
        .filter(entry_id__in=window, root_id=root_id, next_attempt_at__lte=now)
        .filter(_claimable(now))
        .order_by("-mtime_ns", "-entry_id")
        .values(*CLAIM_FIELDS)[:limit]
    )


def _newest_key(row: dict[str, Any]) -> tuple[Any, uuid.UUID]:
    return row["mtime_ns"], row["entry_id"]


def claim_assets(
    owner: uuid.UUID,
    *,
    limit: int,
    root_ids: Collection[uuid.UUID],
    now: datetime,
    lease_seconds: int = DEFAULT_ASSET_LEASE_SECONDS,
) -> list[ClaimedAsset]:
    """Lease up to ``limit`` assets that need work: interactive first, then newest files."""
    if type(limit) is not int or not 0 <= limit <= MAX_CLAIM_BATCH:
        raise ValueError("invalid asset claim limit")
    if limit == 0 or not root_ids:
        return []
    expires_at = now + lease_delta(lease_seconds)
    claimed: list[ClaimedAsset] = []
    roots: Sequence[uuid.UUID] = sorted(set(root_ids))
    with transaction.atomic():
        rows = _by_queue_time(Lane.INTERACTIVE, roots, now, limit)
        remaining = limit - len(rows)
        if remaining > 0:
            # Every root's newest, then the newest of those; the rest stay locked only until
            # this transaction commits.
            newest = sorted(
                (row for root_id in roots for row in _newest(root_id, now, remaining)),
                key=_newest_key, reverse=True,
            )[:remaining]
            rows += newest
            if len(newest) < remaining:
                rows += _by_queue_time(
                    Lane.BACKGROUND, roots, now, remaining - len(newest),
                    exclude={row["entry_id"] for row in newest},
                )
        for row in rows:
            token = int(row["attempt_token"]) + 1
            updated = MediaAsset.objects.filter(
                entry_id=row["entry_id"], attempt_token=row["attempt_token"],
            ).update(
                attempt_token=token,
                lease_owner=owner,
                lease_expires_at=expires_at,
                next_attempt_at=expires_at,
                updated_at=now,
            )
            if updated != 1:
                continue
            row["attempt_token"] = token
            claimed.append(ClaimedAsset(
                AssetLease(row["entry_id"], token, owner, expires_at), row,
            ))
    return claimed


def renew_asset_lease(
    lease: AssetLease, *, now: datetime, lease_seconds: int = DEFAULT_ASSET_LEASE_SECONDS,
) -> AssetLease | None:
    """Extend a live lease; ``None`` when it was lost (the caller must abandon the work)."""
    expires_at = now + lease_delta(lease_seconds)
    updated = MediaAsset.objects.filter(fenced(lease, now=now)).update(
        lease_expires_at=expires_at, next_attempt_at=expires_at, updated_at=now,
    )
    if updated != 1:
        return None
    return AssetLease(lease.entry_id, lease.attempt_token, lease.owner, expires_at)


def release_asset_lease(lease: AssetLease, *, now: datetime) -> bool:
    """Give a lease back unchanged and claimable at once (shutdown; no attempt is counted)."""
    return MediaAsset.objects.filter(fenced(lease, now=now)).update(
        lease_owner=None, lease_expires_at=None, next_attempt_at=now, updated_at=now,
    ) == 1


def bump_to_interactive(entry_id: uuid.UUID, *, now: datetime) -> int:
    """Serve an asset's unfinished background stages ahead of the backfill."""
    return (
        MediaAsset.objects.filter(ASSET_NEEDS_WORK)
        .filter(entry_id=entry_id, lane=Lane.BACKGROUND, next_attempt_at__lt=PARKED_UNTIL)
        .update(lane=Lane.INTERACTIVE, updated_at=now)
    )
