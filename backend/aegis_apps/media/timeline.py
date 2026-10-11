"""``GET /api/v1/roots/{root}/timeline``: a root's media, newest first (spec M5, M6).

Keyset pagination on ``(sort_at DESC, entry DESC)`` over ``media_asset_timeline_idx`` (or
its kind variant). Cursors are signed with their own salt and bound like catalog cursors:
user, cache namespace, user and root epochs, root, filters and limit, with a 900 s expiry
and a size bound. A cursor that does not verify against the current context asks the
client to restart. Only present entries are listed; an entry whose source chain is not
current is listed as ``inaccessible`` without a thumbnail URL.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, cast
from uuid import UUID

from django.core import signing
from django.db import connection
from django.db.models import BooleanField
from django.db.models.expressions import RawSQL

from aegis_apps.catalog.authorization import BrowseContext
from aegis_apps.catalog.cursors import CursorRestartRequired, context_digest
from aegis_apps.catalog.domain import EntryKind, SourceState
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.catalog.queries import media_links, navigation_current_sql, source_current_sql
from aegis_apps.catalog.serializers import entry_summary

from .enums import MediaKind
from .links import MEDIA_ANNOTATIONS, MEDIA_FIELDS

TIMELINE_CURSOR_SALT: Final = "aegis.media.timeline.v1"
TIMELINE_CURSOR_VERSION: Final = 1
TIMELINE_CURSOR_MAX_AGE: Final = 900
MAX_TIMELINE_CURSOR_BYTES: Final = 8192
DEFAULT_TIMELINE_LIMIT: Final = 60
MAX_TIMELINE_LIMIT: Final = 120
MAX_EPOCH: Final = 2**63 - 1
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_MIN_MICROSECONDS: Final = -62_135_596_800_000_000  # 0001-01-01T00:00:00Z
_MAX_MICROSECONDS: Final = 253_402_300_799_999_999  # 9999-12-31T23:59:59.999999Z
_PAYLOAD_FIELDS: Final = frozenset({"v", "context", "key"})
_SUMMARY: Final = (
    "id", "root_id", "display_name", "kind", "type_hint", "size",
    "mtime_ns", "source_state", "catalog_version", "logical_parent_id", "source_parent_id",
)

type TimelineKind = Literal["all", "photo", "video"]
type TimelineKey = tuple[int, UUID]


@dataclass(frozen=True, slots=True)
class TimelineCursorContext:
    user_id: UUID
    cache_namespace: str
    user_epoch: int
    root_id: UUID
    root_epoch: int
    kind: TimelineKind
    year: int | None
    limit: int


def _invalid() -> ValueError:
    return ValueError("invalid timeline cursor")


def _bounded(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _context_fields(context: TimelineCursorContext) -> dict[str, object]:
    if type(context) is not TimelineCursorContext:
        raise _invalid()
    if type(context.user_id) is not UUID or type(context.root_id) is not UUID:
        raise _invalid()
    namespace = context.cache_namespace
    if not isinstance(namespace, str) or not 1 <= len(namespace.encode("utf-8")) <= 256:
        raise _invalid()
    if not _bounded(context.user_epoch, 0, MAX_EPOCH) or not _bounded(
        context.root_epoch, 0, MAX_EPOCH,
    ):
        raise _invalid()
    if type(context.kind) is not str or context.kind not in ("all", "photo", "video"):
        raise _invalid()
    if context.year is not None and not _bounded(context.year, 1, 9999):
        raise _invalid()
    if not _bounded(context.limit, 1, MAX_TIMELINE_LIMIT):
        raise _invalid()
    return {
        "salt": TIMELINE_CURSOR_SALT,
        "user_id": str(context.user_id),
        "cache_namespace": namespace,
        "user_epoch": context.user_epoch,
        "root_id": str(context.root_id),
        "root_epoch": context.root_epoch,
        "kind": context.kind,
        "year": context.year,
        "limit": context.limit,
    }


def _expected_digest(context: TimelineCursorContext) -> str:
    try:
        return context_digest(_context_fields(context))
    except (TypeError, ValueError, UnicodeError):
        raise _invalid() from None


def _validated_key(key: object) -> TimelineKey:
    if type(key) is not tuple or len(key) != 2:
        raise _invalid()
    microseconds, entry_id = key
    if not _bounded(microseconds, _MIN_MICROSECONDS, _MAX_MICROSECONDS):
        raise _invalid()
    if type(entry_id) is not UUID:
        raise _invalid()
    return cast(int, microseconds), entry_id


def sort_key_microseconds(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise _invalid()
    delta = value.astimezone(UTC) - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def sort_key_datetime(microseconds: int) -> datetime:
    return _EPOCH + timedelta(microseconds=microseconds)


def encode_timeline_cursor(context: TimelineCursorContext, key: TimelineKey) -> str:
    """Sign a keyset boundary; authorization remains the caller's duty."""
    try:
        microseconds, entry_id = _validated_key(key)
        payload: dict[str, object] = {
            "v": TIMELINE_CURSOR_VERSION,
            "context": _expected_digest(context),
            "key": [str(microseconds), str(entry_id)],
        }
        token = signing.dumps(payload, salt=TIMELINE_CURSOR_SALT, compress=False)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if len(token) > MAX_TIMELINE_CURSOR_BYTES or token.startswith("."):
        raise _invalid()
    return token


def decode_timeline_cursor(value: object, context: TimelineCursorContext) -> TimelineKey:
    """Authenticate a cursor against the current authorized context, or restart."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_TIMELINE_CURSOR_BYTES
        or not value.isascii()
        or value.startswith(".")
    ):
        raise CursorRestartRequired()
    try:
        payload = signing.loads(
            value, salt=TIMELINE_CURSOR_SALT, max_age=TIMELINE_CURSOR_MAX_AGE,
        )
    except (signing.BadSignature, TypeError, ValueError, UnicodeError, RecursionError):
        raise CursorRestartRequired() from None
    try:
        expected = _expected_digest(context)
        if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
            raise _invalid()
        if type(payload["v"]) is not int or payload["v"] != TIMELINE_CURSOR_VERSION:
            raise _invalid()
        supplied = payload["context"]
        if type(supplied) is not str or not secrets.compare_digest(supplied, expected):
            raise _invalid()
        key = payload["key"]
        if type(key) is not list or len(key) != 2:
            raise _invalid()
        encoded_time, encoded_id = key
        if type(encoded_time) is not str or type(encoded_id) is not str:
            raise _invalid()
        if not encoded_time.lstrip("-").isdecimal() or not encoded_time.isascii():
            raise _invalid()
        if encoded_time != str(int(encoded_time)):
            raise _invalid()
        entry_id = UUID(encoded_id)
        if str(entry_id) != encoded_id:
            raise _invalid()
        return _validated_key((int(encoded_time), entry_id))
    except (KeyError, TypeError, ValueError, AttributeError):
        raise CursorRestartRequired() from None


def _year_bounds(year: int) -> tuple[datetime, datetime | None]:
    start = datetime(year, 1, 1, tzinfo=UTC)
    return start, (datetime(year + 1, 1, 1, tzinfo=UTC) if year < 9999 else None)


def timeline_page(
    context: BrowseContext,
    *,
    kind: TimelineKind,
    year: int | None,
    limit: int,
    cursor: str | None,
) -> dict[str, object]:
    context.require_active()
    if (
        kind not in ("all", "photo", "video")
        or (year is not None and not _bounded(year, 1, 9999))
        or not _bounded(limit, 1, MAX_TIMELINE_LIMIT)
    ):
        raise ValueError("invalid_catalog_query")
    cursor_context = TimelineCursorContext(
        context.user.pk, context.namespace, context.user_epoch, context.root.pk,
        context.root_epoch, kind, year, limit,
    )
    boundary = decode_timeline_cursor(cursor, cursor_context) if cursor is not None else None
    root_id = context.root.pk
    query = CatalogEntry.objects.filter(
        root_id=root_id,
        kind=EntryKind.FILE,
        source_state=SourceState.PRESENT,
        media_asset__root_id=root_id,
    )
    if kind != "all":
        query = query.filter(media_asset__kind=MediaKind(kind))
    if year is not None:
        start, end = _year_bounds(year)
        query = query.filter(media_asset__sort_at__gte=start)
        if end is not None:
            query = query.filter(media_asset__sort_at__lt=end)
    if boundary is not None:
        # A row comparison in the index's own column order lets PostgreSQL seek straight
        # to the boundary in media_asset_timeline_idx (or its kind variant) instead of
        # scanning every newer row; an OR of comparisons gives the index no lower bound.
        query = query.filter(RawSQL(
            '("media_mediaasset"."sort_at", "media_mediaasset"."entry_id") < (%s, %s)',
            (sort_key_datetime(boundary[0]), boundary[1]),
            output_field=BooleanField(),
        ))
    candidates = (
        query.annotate(**MEDIA_ANNOTATIONS)
        .order_by("-media_asset__sort_at", "-media_asset__entry_id")
        .values(*_SUMMARY, *MEDIA_FIELDS)[:limit + 1]
    )
    sql, params = candidates.query.sql_with_params()
    with connection.cursor() as result:
        # The availability checks run only for the bounded page of candidates.
        result.execute(
            f"WITH candidates AS MATERIALIZED ({sql}) SELECT candidates.*, "
            f"({source_current_sql('candidates')}) "
            f"AND ({navigation_current_sql('candidates')}) AS _current "
            'FROM candidates ORDER BY "_media_sort_at" DESC, "id" DESC',
            params,
        )
        assert result.description is not None
        columns = [column[0] for column in result.description]
        rows: list[dict[str, Any]] = [
            dict(zip(columns, row, strict=True)) for row in result.fetchall()
        ]
    more = len(rows) > limit
    rows = rows[:limit]
    links = media_links(context)
    items = [
        {
            **entry_summary(row, available=row["_current"] is True, links=links),
            "parentId": str(row["logical_parent_id"]) if row["logical_parent_id"] else None,
        }
        for row in rows
    ]
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_timeline_cursor(
            cursor_context, (sort_key_microseconds(last["_media_sort_at"]), last["id"]),
        )
    return {"items": items, "nextCursor": next_cursor, "contractVersion": 1}
