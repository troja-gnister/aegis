"""Media discovery: catalog files become assets, and gone files park theirs (spec M1, M3).

The worker keyset-scans each active root's catalog for present files whose extension is on
the media allowlist (M4). It reads only the catalog columns granted to ``aegis_media``
(``MEDIA_CATALOG_READ_COLUMNS``), always through ``.values()`` with an explicit order, and
takes no explicit lock on a catalog or root row (an asset insert's foreign keys take
PostgreSQL's usual brief ``KEY SHARE`` locks on the entry and the root, as any insert does).

- **New entries** get an asset (inserted with ``ON CONFLICT DO NOTHING`` on the entry key, so
  a second worker or a second pass inserts nothing).
- **A changed fingerprint** (or kind) resets the asset in ONE transaction, under the asset's
  row lock: the fingerprint inputs and the fingerprint are replaced, every stage goes back to
  ``pending``, ``attempts`` to 0, the probe metadata is cleared, the lease is dropped and
  ``attempt_token`` is incremented (so in-flight work for the old file can no longer write),
  and every non-stale derivative of the asset is marked ``stale``. Only rows change; stale
  files are removed later by eviction, never in place. Delivery also requires the
  derivative's fingerprint to equal the asset's, so no old bytes are served under the new
  fingerprint even before eviction runs.
- **Gone entries** (no longer a present file with a media extension) **park** their asset.
  Catalog rows are never deleted, and the media role cannot delete assets either. A parked
  asset is reset exactly as for a fingerprint change (stages ``pending``, metadata cleared,
  lease dropped, token incremented, derivatives ``stale``) but keeps its last fingerprint and
  waits with ``next_attempt_at = PARKED_UNTIL`` (year 9999), so no claim ever reaches it and
  no personal metadata is kept for a file that is gone. The timeline and the entry lists show
  only present entries, so a parked asset is never displayed. If the entry becomes present
  again, discovery resets it with the current fingerprint and ``next_attempt_at = now``; the
  regenerated derivatives have the same keys as the stale rows and revive them. The price of
  clearing the metadata is that an entry which is only briefly ``missing`` or
  ``inaccessible`` is probed and thumbnailed again when it returns; that is accepted.

A pass walks every active root (catalog phase, then the asset phase that parks), one bounded
batch per call, and the next pass starts :data:`DISCOVERY_INTERVAL_SECONDS` (an hour) after it
ends; the worker passes ``AEGIS_MEDIA_DISCOVERY_INTERVAL_SECONDS`` (that hour by default).
``DiscoveryCounts.created`` counts the rows offered to ``ON CONFLICT DO NOTHING``; a
concurrent insert by another worker is counted by both.
:func:`rediscover_entry` re-checks one entry at once, for the processor's ``changed`` result.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal

from django.db import transaction
from django.db.models import F

from aegis_apps.catalog.domain import EntryKind, SourceState
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.roots.manifest import MountManifest
from aegis_apps.roots.models import Root

from .config import DISCOVERY_INTERVAL
from .enums import DerivativeState, Lane, MediaKind, SortSource, StageState
from .formats import MEDIA_EXTENSIONS, media_kind
from .keys import fingerprint
from .leasing import PARKED_UNTIL
from .models import MediaAsset, MediaDerivative
from .sorting import modified_sort_at

# A full pass reads every catalog row of every active root, so passes are an hour apart
# (the default of AEGIS_MEDIA_DISCOVERY_INTERVAL_SECONDS, defined once in config.py).
DISCOVERY_INTERVAL_SECONDS: Final = float(DISCOVERY_INTERVAL[1])
# Only these catalog columns are ever selected or filtered on (the media role's grant).
_CATALOG_FIELDS: Final = (
    "id", "root_id", "kind", "type_hint", "source_state", "size", "mtime_ns", "device", "inode",
)

type Rediscovery = Literal["created", "reset", "parked", "unchanged", "absent"]


@dataclass(frozen=True, slots=True)
class CatalogFile:
    """A present media file of the catalog, as discovery sees it."""

    entry_id: uuid.UUID
    root_id: uuid.UUID
    kind: MediaKind
    device: Decimal
    inode: Decimal
    size: Decimal
    mtime_ns: Decimal
    fingerprint: str


@dataclass(slots=True)
class DiscoveryCounts:
    created: int = 0
    reset: int = 0
    parked: int = 0
    skipped: int = 0

    def add(self, other: DiscoveryCounts) -> None:
        self.created += other.created
        self.reset += other.reset
        self.parked += other.parked
        self.skipped += other.skipped


def is_parked(next_attempt_at: datetime) -> bool:
    return next_attempt_at >= PARKED_UNTIL


def active_roots(manifest: MountManifest | None) -> dict[uuid.UUID, str]:
    """Active roots whose slot is in the mount manifest, by id (only granted root columns)."""
    if manifest is None or not manifest.slots:
        return {}
    rows = (
        Root.objects.filter(active=True, slot_id__in=sorted(manifest.slots))
        .order_by("id")
        .values_list("id", "slot_id")
    )
    return {root_id: slot_id for root_id, slot_id in rows}


def catalog_file(row: Mapping[str, Any]) -> CatalogFile | None:
    """A catalog row as a present media file, or ``None`` if it is not one."""
    kind = media_kind(row.get("type_hint"))
    identity = [row.get("device"), row.get("inode"), row.get("size"), row.get("mtime_ns")]
    if (
        kind is None
        or row.get("kind") != EntryKind.FILE
        or row.get("source_state") != SourceState.PRESENT
        or any(value is None for value in identity)
    ):
        return None
    device, inode, size, mtime_ns = (Decimal(str(value)) for value in identity)
    try:
        digest = fingerprint(row["root_id"], row["id"], device, inode, size, mtime_ns)
    except ValueError:
        return None
    return CatalogFile(
        entry_id=row["id"], root_id=row["root_id"], kind=MediaKind(kind), device=device,
        inode=inode, size=size, mtime_ns=mtime_ns, fingerprint=digest,
    )


def _reset_columns(kind: str) -> dict[str, Any]:
    return {
        "probe_state": StageState.PENDING,
        "thumbnail_state": StageState.PENDING,
        "poster_state": StageState.PENDING if kind == MediaKind.VIDEO else None,
        "lane": Lane.BACKGROUND,
        "attempts": 0,
        "environment_failures": 0,
        "lease_owner": None,
        "lease_expires_at": None,
        "processor_version": None,
        "safe_error_code": None,
        "width": None,
        "height": None,
        "orientation": None,
        "captured_at": None,
        "captured_at_source": None,
        "camera_make": None,
        "camera_model": None,
        "duration_ms": None,
        "video_codec": None,
        "audio_codec": None,
        "container": None,
        "has_location": False,
        "sort_source": SortSource.MODIFIED,
    }


def _new_asset(source: CatalogFile, *, now: datetime) -> MediaAsset:
    return MediaAsset(
        entry_id=source.entry_id,
        root_id=source.root_id,
        kind=source.kind,
        device=source.device,
        inode=source.inode,
        size=source.size,
        mtime_ns=source.mtime_ns,
        fingerprint=source.fingerprint,
        probe_state=StageState.PENDING,
        thumbnail_state=StageState.PENDING,
        poster_state=StageState.PENDING if source.kind == MediaKind.VIDEO else None,
        lane=Lane.BACKGROUND,
        next_attempt_at=now,
        sort_at=modified_sort_at(source.mtime_ns),
        sort_source=SortSource.MODIFIED,
    )


def _needs_reset(asset: Mapping[str, Any], source: CatalogFile) -> bool:
    return (
        asset["fingerprint"] != source.fingerprint
        or asset["kind"] != source.kind
        or is_parked(asset["next_attempt_at"])
    )


def reset_asset(
    entry_id: uuid.UUID, *, now: datetime, source: CatalogFile | None,
) -> bool:
    """Reset an asset to a new source (``source``) or park it (``None``), in one transaction.

    Returns whether anything changed; repeating the call with the same input is a no-op.
    """
    with transaction.atomic():
        current = (
            MediaAsset.objects.select_for_update(of=("self",))
            .filter(entry_id=entry_id)
            .values("kind", "fingerprint", "next_attempt_at", "mtime_ns")
            .first()
        )
        if current is None:
            return False
        if source is not None:
            if not _needs_reset(current, source):
                return False
            kind: str = source.kind
            values: dict[str, Any] = {
                "kind": source.kind,
                "device": source.device,
                "inode": source.inode,
                "size": source.size,
                "mtime_ns": source.mtime_ns,
                "fingerprint": source.fingerprint,
                "next_attempt_at": now,
                "sort_at": modified_sort_at(source.mtime_ns),
            }
        else:
            if is_parked(current["next_attempt_at"]):
                return False
            kind = str(current["kind"])
            values = {
                "next_attempt_at": PARKED_UNTIL,
                "sort_at": modified_sort_at(current["mtime_ns"]),
            }
        MediaAsset.objects.filter(entry_id=entry_id).update(
            **values,
            **_reset_columns(kind),
            attempt_token=F("attempt_token") + 1,
            updated_at=now,
        )
        MediaDerivative.objects.filter(asset_id=entry_id).exclude(
            state=DerivativeState.STALE,
        ).update(state=DerivativeState.STALE)
    return True


def _catalog_rows(
    root_id: uuid.UUID, after: uuid.UUID | None, limit: int,
) -> list[dict[str, Any]]:
    query = CatalogEntry.objects.filter(
        root_id=root_id,
        kind=EntryKind.FILE,
        source_state=SourceState.PRESENT,
        type_hint__in=MEDIA_EXTENSIONS,
    )
    if after is not None:
        query = query.filter(id__gt=after)
    return list(query.order_by("id").values(*_CATALOG_FIELDS)[:limit])


def discover_batch(
    root_id: uuid.UUID, after: uuid.UUID | None, *, limit: int, now: datetime,
) -> tuple[uuid.UUID | None, DiscoveryCounts]:
    """Upsert the assets of one keyset batch; returns the next cursor (``None`` at the end)."""
    counts = DiscoveryCounts()
    rows = _catalog_rows(root_id, after, limit)
    if not rows:
        return None, counts
    sources: list[CatalogFile] = []
    for row in rows:
        source = catalog_file(row)
        if source is None:
            counts.skipped += 1
        else:
            sources.append(source)
    existing = {
        row["entry_id"]: row
        for row in MediaAsset.objects.filter(entry_id__in=[s.entry_id for s in sources])
        .order_by("entry_id")
        .values("entry_id", "root_id", "kind", "fingerprint", "next_attempt_at")
    }
    missing = [source for source in sources if source.entry_id not in existing]
    if missing:
        created = MediaAsset.objects.bulk_create(
            [_new_asset(source, now=now) for source in missing], ignore_conflicts=True,
        )
        counts.created += len(created)
    for source in sources:
        asset = existing.get(source.entry_id)
        if asset is None or asset["root_id"] != source.root_id:
            continue
        if _needs_reset(asset, source) and reset_asset(source.entry_id, now=now, source=source):
            counts.reset += 1
    cursor: uuid.UUID = rows[-1]["id"]
    return (cursor if len(rows) >= limit else None), counts


def park_batch(
    root_id: uuid.UUID, after: uuid.UUID | None, *, limit: int, now: datetime,
) -> tuple[uuid.UUID | None, DiscoveryCounts]:
    """Park the assets of one keyset batch whose entry is no longer a present media file."""
    counts = DiscoveryCounts()
    query = MediaAsset.objects.filter(root_id=root_id)
    if after is not None:
        query = query.filter(entry_id__gt=after)
    assets = list(query.order_by("entry_id").values("entry_id", "next_attempt_at")[:limit])
    if not assets:
        return None, counts
    ids = [asset["entry_id"] for asset in assets]
    present = {
        row["id"]
        for row in CatalogEntry.objects.filter(
            id__in=ids,
            root_id=root_id,
            kind=EntryKind.FILE,
            source_state=SourceState.PRESENT,
            type_hint__in=MEDIA_EXTENSIONS,
        ).order_by("id").values("id")
    }
    for asset in assets:
        if asset["entry_id"] in present or is_parked(asset["next_attempt_at"]):
            continue
        if reset_asset(asset["entry_id"], now=now, source=None):
            counts.parked += 1
    cursor: uuid.UUID = assets[-1]["entry_id"]
    return (cursor if len(assets) >= limit else None), counts


def rediscover_entry(entry_id: uuid.UUID, *, now: datetime) -> Rediscovery:
    """Re-check one entry against the catalog at once (the processor reported ``changed``)."""
    row = CatalogEntry.objects.filter(id=entry_id).order_by("id").values(*_CATALOG_FIELDS).first()
    source = None if row is None else catalog_file(row)
    asset = (
        MediaAsset.objects.filter(entry_id=entry_id)
        .order_by("entry_id")
        .values("root_id", "kind", "fingerprint", "next_attempt_at")
        .first()
    )
    if asset is None:
        if source is None:
            return "absent"
        MediaAsset.objects.bulk_create([_new_asset(source, now=now)], ignore_conflicts=True)
        return "created"
    if source is None or source.root_id != asset["root_id"]:
        return "parked" if reset_asset(entry_id, now=now, source=None) else "unchanged"
    if _needs_reset(asset, source):
        return "reset" if reset_asset(entry_id, now=now, source=source) else "unchanged"
    return "unchanged"


@dataclass(slots=True)
class Discovery:
    """One discovery pass at a time over the active roots, one bounded batch per step."""

    batch: int
    interval_seconds: float = DISCOVERY_INTERVAL_SECONDS
    clock: Callable[[], float] = time.monotonic
    roots: list[uuid.UUID] = field(default_factory=list)
    phase: Literal["catalog", "assets"] = "catalog"
    cursor: uuid.UUID | None = None
    next_pass_at: float = 0.0
    passes: int = 0
    totals: DiscoveryCounts = field(default_factory=DiscoveryCounts)

    def __post_init__(self) -> None:
        if type(self.batch) is not int or not 1 <= self.batch <= 5_000:
            raise ValueError("invalid discovery batch")

    @property
    def active(self) -> bool:
        return bool(self.roots)

    def due(self) -> bool:
        return not self.roots and self.clock() >= self.next_pass_at

    def start(self, roots: Sequence[uuid.UUID]) -> None:
        self.roots = sorted(roots)
        self.phase = "catalog"
        self.cursor = None
        if not self.roots:
            self._finish()

    def request_pass(self) -> None:
        """Start the next pass as soon as the current one (if any) ends."""
        self.next_pass_at = 0.0

    def _finish(self) -> None:
        self.roots = []
        self.passes += 1
        self.next_pass_at = self.clock() + self.interval_seconds

    def step(self, *, now: datetime) -> DiscoveryCounts:
        """Run one batch of the current pass (catalog, then parking, per root)."""
        if not self.roots:
            return DiscoveryCounts()
        root_id = self.roots[0]
        if self.phase == "catalog":
            cursor, counts = discover_batch(root_id, self.cursor, limit=self.batch, now=now)
            if cursor is None:
                self.phase, self.cursor = "assets", None
            else:
                self.cursor = cursor
        else:
            cursor, counts = park_batch(root_id, self.cursor, limit=self.batch, now=now)
            if cursor is None:
                self.roots.pop(0)
                self.phase, self.cursor = "catalog", None
                if not self.roots:
                    self._finish()
            else:
                self.cursor = cursor
        self.totals.add(counts)
        return counts
