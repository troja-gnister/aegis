"""Root-bound media reads. Every function requires an active, locked ``BrowseContext``."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from django.db import connection, models
from django.db.models import F
from django.db.models.expressions import RawSQL

from aegis_apps.catalog.authorization import BrowseContext
from aegis_apps.catalog.domain import EntryKind, SourceState
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.catalog.queries import navigation_current_sql, source_current_sql

from .enums import DerivativeState
from .formats import DERIVATIVE_KINDS_BY_MEDIA_KIND
from .keys import fingerprint
from .models import MediaAsset, MediaDerivative
from .paths import MAX_PATH_DEPTH


def availability_sql(alias: str) -> str:
    # The same definition as catalog details and the timeline: a current physical source
    # chain and current navigation ancestors.
    return f"({source_current_sql(alias)}) AND ({navigation_current_sql(alias)})"

_ASSET_FIELDS = (
    "kind", "fingerprint", "probe_state", "thumbnail_state", "poster_state",
    "width", "height", "orientation", "captured_at", "captured_at_source",
    "camera_make", "camera_model", "duration_ms", "video_codec", "audio_codec",
    "container", "has_location", "sort_at", "sort_source",
)


@dataclass(frozen=True, slots=True)
class EntryFile:
    """A catalog entry as delivery sees it; never serialized."""

    entry_id: UUID
    type_hint: str | None
    raw_name: bytes
    available: bool
    # The fingerprint of the catalog's current observation (device, inode, size, mtime),
    # or None when the catalog lacks one of them.
    source_fingerprint: str | None


def entry_file(context: BrowseContext, entry_id: UUID) -> EntryFile | None:
    """The entry if it is a regular file of the locked root, else ``None``.

    ``available`` means present with current source and navigation chains, the condition
    under which catalog details report the entry as ``present``.
    """
    context.require_active()
    row = (
        CatalogEntry.objects.filter(pk=entry_id, root_id=context.root.pk)
        .annotate(_current=RawSQL(
            availability_sql("catalog_catalogentry"), (), output_field=models.BooleanField(),
        ))
        .values(
            "kind", "source_state", "type_hint", "raw_name", "device", "inode", "size",
            "mtime_ns", "_current",
        )
        .first()
    )
    if row is None or row["kind"] != EntryKind.FILE:
        return None
    identity = (row["device"], row["inode"], row["size"], row["mtime_ns"])
    current_fingerprint = None
    if all(value is not None for value in identity):
        try:
            current_fingerprint = fingerprint(context.root.pk, entry_id, *identity)
        except ValueError:
            current_fingerprint = None
    return EntryFile(
        entry_id=entry_id,
        type_hint=row["type_hint"],
        raw_name=bytes(row["raw_name"]),
        available=row["source_state"] == SourceState.PRESENT and row["_current"] is True,
        source_fingerprint=current_fingerprint,
    )


def source_names(context: BrowseContext, entry_id: UUID) -> list[bytes] | None:
    """The raw names from the root anchor down to *entry_id*, or ``None``.

    The walk follows ``source_parent`` only, is bounded by depth and stops on cycles; a
    chain that does not end at the root's anchor (an empty name without a parent) is
    rejected. The entry's availability is decided in the same statement, so the names
    and the currency check come from one snapshot.
    """
    context.require_active()
    root_id = context.root.pk
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            WITH RECURSIVE chain AS (
                SELECT id, source_parent_id, raw_name, 0 AS depth
                  FROM catalog_catalogentry WHERE id=%s AND root_id=%s
                UNION ALL
                SELECT p.id, p.source_parent_id, p.raw_name, c.depth + 1
                  FROM catalog_catalogentry p JOIN chain c ON p.id=c.source_parent_id
                 WHERE p.root_id=%s AND c.depth < %s
            ) CYCLE id SET cyclic USING visited
            SELECT raw_name, source_parent_id IS NULL, cyclic, (
                SELECT leaf.source_state = 'present' AND {availability_sql("leaf")}
                  FROM catalog_catalogentry leaf WHERE leaf.id=%s AND leaf.root_id=%s
            ) FROM chain ORDER BY depth DESC
            """,
            [entry_id, root_id, root_id, MAX_PATH_DEPTH, entry_id, root_id],
        )
        rows = cursor.fetchall()
    if len(rows) < 2 or any(row[2] for row in rows) or rows[0][3] is not True:
        return None
    anchor_name, anchor_is_top, _, _ = rows[0]
    if not anchor_is_top or bytes(anchor_name) != b"":
        return None
    names = [bytes(row[0]) for row in rows[1:]]
    if any(not name for name in names) or any(row[1] for row in rows[1:]):
        return None
    return names


def media_asset(context: BrowseContext, entry_id: UUID) -> dict[str, Any] | None:
    """The asset row of an entry of the locked root, or ``None``."""
    context.require_active()
    row: dict[str, Any] | None = (
        MediaAsset.objects.filter(entry_id=entry_id, root_id=context.root.pk)
        .values(*_ASSET_FIELDS)
        .first()
    )
    return row


def current_derivatives(
    context: BrowseContext, entry_id: UUID, fingerprint: str,
) -> dict[str, dict[str, Any]]:
    """The non-stale derivative rows built from *fingerprint*, by kind."""
    context.require_active()
    rows = (
        MediaDerivative.objects.filter(
            asset_id=entry_id, asset__root_id=context.root.pk, fingerprint=fingerprint,
        )
        .exclude(state=DerivativeState.STALE)
        .values("kind", "state", "key", "extension")
    )
    return {row["kind"]: dict(row) for row in rows}


@dataclass(frozen=True, slots=True)
class ReadyDerivative:
    key: str
    extension: str
    size: int
    created_at: datetime
    fingerprint: str


def ready_derivative(
    context: BrowseContext, entry_id: UUID, kind: str,
) -> ReadyDerivative | None:
    """The ready derivative of *kind* built from the asset's current fingerprint.

    Callers compare ``fingerprint`` with the URL token and with the catalog's current
    observation, so a stale or mismatched URL never serves other bytes.
    """
    context.require_active()
    media_kinds = [
        media_kind for media_kind, kinds in DERIVATIVE_KINDS_BY_MEDIA_KIND.items()
        if kind in kinds
    ]
    row = (
        MediaDerivative.objects.filter(
            asset_id=entry_id,
            asset__root_id=context.root.pk,
            asset__kind__in=media_kinds,
            kind=kind,
            fingerprint=F("asset__fingerprint"),
            state=DerivativeState.READY,
        )
        .values("key", "extension", "bytes", "created_at", "fingerprint")
        .first()
    )
    if row is None or row["bytes"] is None:
        return None
    return ReadyDerivative(
        key=row["key"],
        extension=row["extension"],
        size=row["bytes"],
        created_at=row["created_at"],
        fingerprint=row["fingerprint"],
    )
