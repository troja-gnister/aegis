"""Root-bound keyset reads; application allocations depend on page size, not fanout."""

from __future__ import annotations

import hashlib
from functools import reduce
from operator import or_
from typing import Any, cast
from uuid import UUID

from django.db import connection, models
from django.db.models import Q
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce

from aegis_apps.identity.models import User
from aegis_apps.roots.manifest import ManifestError, configured_manifest
from aegis_apps.roots.selectors import authorized_roots

from .authorization import (
    BrowseContext,
    CatalogNotFound,
    CatalogNotReady,
    bounded_read,
    browse_context,
)
from .cursors import (
    CursorContext,
    CursorKey,
    SortName,
    SortOrder,
    decode_cursor,
    encode_cursor,
)
from .filters import UNKNOWN_TYPE, FileFilter, canonical_filter_bytes
from .models import CatalogEntry
from .serializers import entry_summary, index_status

_SUMMARY = (
    "id", "root_id", "display_name", "kind", "type_hint", "size",
    "mtime_ns", "source_state", "catalog_version",
)


def keyset_predicate(
    fields: tuple[str, ...], ascending: tuple[bool, ...], boundary: tuple[object, ...],
    *, previous: bool,
) -> Q:
    if not fields or len(fields) != len(ascending) or len(fields) != len(boundary):
        raise ValueError("invalid keyset")
    equal = Q()
    branches: list[Q] = []
    for field, is_ascending, value in zip(fields, ascending, boundary, strict=True):
        comparison = "gt" if is_ascending != previous else "lt"
        branches.append(equal & Q(**{f"{field}__{comparison}": value}))
        equal &= Q(**{field: value})
    return reduce(or_, branches)


def _source_current(alias: str) -> str:
    # Only fixed internal aliases reach this function. UNION (distinct) terminates
    # cycles without an application-sized visited set or an ever-growing SQL path.
    return f"""(
        WITH RECURSIVE source_chain AS (
            SELECT id, root_id, source_parent_id, source_parent_revision,
                   source_revision, kind, source_state
              FROM catalog_catalogentry seed
             WHERE seed.id={alias}.id AND seed.root_id={alias}.root_id
            UNION
            SELECT p.id, p.root_id, p.source_parent_id, p.source_parent_revision,
                   p.source_revision, p.kind, p.source_state
              FROM catalog_catalogentry p JOIN source_chain c
                ON p.id=c.source_parent_id AND p.root_id=c.root_id
        )
        SELECT COALESCE(bool_and(c.source_state='present'
                   AND (c.id={alias}.id OR c.kind='directory')
                   AND (c.source_parent_id IS NULL OR
                        (c.source_parent_revision IS NOT NULL
                         AND c.source_parent_revision=p.source_revision))), false)
               AND COALESCE(bool_or(c.source_parent_id IS NULL), false)
          FROM source_chain c LEFT JOIN catalog_catalogentry p
            ON p.id=c.source_parent_id AND p.root_id=c.root_id
    )"""


def _entry(root_id: UUID, entry_id: UUID | None) -> dict[str, Any]:
    query = CatalogEntry.objects.filter(root_id=root_id)
    query = (
        query.filter(source_parent__isnull=True) if entry_id is None else query.filter(pk=entry_id)
    )
    row = query.annotate(
        _current=RawSQL(
            f"{_source_current('catalog_catalogentry')} "
            f"AND {_navigation_current('catalog_catalogentry')}", (),
            output_field=models.BooleanField(),
        ),
    ).values(*_SUMMARY, "logical_parent_id", "children_version", "_current").first()
    if row is None:
        if entry_id is None:
            raise CatalogNotReady()
        raise CatalogNotFound()
    return row


def _navigation_current(alias: str) -> str:
    # Logical moves do not change source identity. Every navigation ancestor must
    # itself still have a current physical source chain, even when the paths differ.
    return f"""(
        WITH RECURSIVE navigation AS (
            SELECT seed.id, seed.root_id, seed.logical_parent_id, seed.source_parent_id, seed.kind
              FROM catalog_catalogentry seed
             WHERE seed.id={alias}.logical_parent_id AND seed.root_id={alias}.root_id
            UNION
            SELECT p.id, p.root_id, p.logical_parent_id, p.source_parent_id, p.kind
              FROM catalog_catalogentry p JOIN navigation n
                ON p.id=n.logical_parent_id AND p.root_id=n.root_id
        )
        SELECT COALESCE(bool_and(n.kind='directory' AND {_source_current('n')}), true)
               AND COALESCE(bool_or(n.logical_parent_id IS NULL AND n.source_parent_id IS NULL),
                            {alias}.logical_parent_id IS NULL AND {alias}.source_parent_id IS NULL)
          FROM navigation n
    )"""


def _filter(query: Any, filters: FileFilter) -> Any:
    if filters.kind:
        query = query.filter(kind__in=filters.kind)
    if filters.type:
        selected = Q(type_hint__in=[value for value in filters.type if value != UNKNOWN_TYPE])
        if UNKNOWN_TYPE in filters.type:
            selected |= Q(type_hint__isnull=True)
        query = query.filter(selected)
    if filters.availability:
        query = query.filter(source_state__in=filters.availability)
    if filters.size is not None:
        value = filters.size
        query = query.filter(size__isnull=value.unknown)
        if value.minimum is not None:
            query = query.filter(size__gte=value.minimum)
        if value.maximum is not None:
            query = query.filter(size__lte=value.maximum)
    if filters.modified is not None:
        modified = filters.modified
        query = query.filter(mtime_ns__isnull=modified.unknown)
        if modified.from_ns is not None:
            query = query.filter(mtime_ns__gte=modified.from_ns)
        if modified.before_ns is not None:
            query = query.filter(mtime_ns__lt=modified.before_ns)
    if filters.prefix:
        prefix = filters.prefix.encode("utf-8")
        # UTF-8 ends below 0xff. Byte ranges implement literal prefix matching,
        # independent of SQL LIKE wildcards, text collation and escaping.
        upper = prefix[:-1] + bytes([prefix[-1] + 1])
        query = query.filter(name_key__gte=prefix, name_key__lt=upper)
    return query


def _key(row: dict[str, Any], sort: str) -> CursorKey:
    name = bytes(row["name_key"])
    return (row["_kind"], row["_null"], name if sort == "name" else int(row["_value"]),
            name, row["id"])


def directory_page(
    context: BrowseContext, parent_id: UUID | None, filters: FileFilter, sort: str,
    order: str, limit: int, cursor: str | None,
) -> dict[str, object]:
    context.require_active()
    if sort not in ("name", "modified", "size") or order not in ("asc", "desc") or (
        type(limit) is not int or not 1 <= limit <= 250
    ):
        raise ValueError("invalid_catalog_query")
    parent = _entry(context.root.pk, parent_id)
    if parent["kind"] != "directory":
        raise CatalogNotFound()
    cursor_context = CursorContext(
        context.user.pk, context.namespace, context.user_epoch, context.root.pk,
        context.root_epoch, parent["id"], parent["children_version"],
        hashlib.sha256(canonical_filter_bytes(filters)).hexdigest(),
        cast(SortName, sort), cast(SortOrder, order), limit,
    )
    position = decode_cursor(cursor, cursor_context) if cursor is not None else None
    previous = position is not None and position.travel == "previous"
    ascending = order == "asc"
    query = CatalogEntry.objects.filter(root_id=context.root.pk, logical_parent_id=parent["id"])
    query = query.annotate(
        _missing=models.Case(models.When(source_state="missing", then=True), default=False),
        _kind=models.Case(models.When(kind="directory", then=0), default=1),
    )
    if sort == "name":
        query = query.annotate(_null=models.Value(0), _value=models.F("name_key"))
        fields: tuple[str, ...] = ("_kind", "name_key", "id")
        directions: tuple[bool, ...] = (True, ascending, ascending)
        boundary: tuple[object, ...] = (
            (position.key[0], position.key[3], position.key[4]) if position else ()
        )
    else:
        value_field = "mtime_ns" if sort == "modified" else "size"
        query = query.annotate(
            _null=models.Case(models.When(**{f"{value_field}__isnull": True}, then=1), default=0),
            _value=Coalesce(value_field, 0, output_field=models.DecimalField()),
        )
        fields = ("_kind", "_null", "_value", "name_key", "id")
        directions = (True, True, ascending, ascending, ascending)
        boundary = position.key if position else ()
    query = _filter(query, filters)
    if position:
        query = query.filter(keyset_predicate(fields, directions, boundary, previous=previous))
    ordering = tuple(
        field if direction != previous else f"-{field}"
        for field, direction in zip(fields, directions, strict=True)
    )
    projection = (*_SUMMARY, "name_key", "_kind", "_null", "_value")
    branches: tuple[bool, ...] = (False,)
    if "missing" in filters.availability:
        branches = (True,) if len(filters.availability) == 1 else (False, True)
    candidates = [query.filter(_missing=missing).order_by(*ordering).values(*projection)[:limit + 1]
                  for missing in branches]
    selected = candidates[0]
    if len(candidates) == 2:
        selected = selected.union(candidates[1], all=True).order_by(*ordering)[:limit + 1]
    sql, params = selected.query.sql_with_params()
    final_order = ", ".join(f'"{field}" {"ASC" if direction != previous else "DESC"}'
                            for field, direction in zip(fields, directions, strict=True))
    with connection.cursor() as result:
        result.execute(
            f"WITH candidates AS MATERIALIZED ({sql}) SELECT candidates.*, "
            f"{_source_current('candidates')} AS _current FROM candidates ORDER BY {final_order}",
            params,
        )
        assert result.description is not None
        columns = [column[0] for column in result.description]
        rows = [dict(zip(columns, row, strict=True)) for row in result.fetchall()]
    more = len(rows) > limit
    rows = rows[:limit]
    if previous:
        rows.reverse()
    has_next = bool(position) if previous else more
    has_previous = more if previous else bool(position)
    return {
        "entries": [entry_summary(row, available=parent["_current"] and row["_current"])
                    for row in rows],
        "nextCursor": encode_cursor(cursor_context, _key(rows[-1], sort), "next")
        if rows and has_next else None,
        "previousCursor": encode_cursor(cursor_context, _key(rows[0], sort), "previous")
        if rows and has_previous else None,
        "directoryId": str(parent["id"]), "directoryVersion": str(parent["children_version"]),
        "contractVersion": 1, "indexStatus": index_status(context.root.pk,
                                                        available=parent["_current"]),
    }


def entry_details(user: User, entry_id: UUID, namespace: str) -> dict[str, object]:
    with bounded_read():
        try:
            manifest = configured_manifest()
        except ManifestError:
            raise CatalogNotFound() from None
        roots = authorized_roots(
            user_id=user.pk, active_manifest_slot_ids=tuple(manifest.slots) if manifest else (),
        ).values("id")
        root_id = CatalogEntry.objects.filter(pk=entry_id, root_id__in=roots).values_list(
            "root_id", flat=True,
        ).first()
        if root_id is None:
            raise CatalogNotFound()
        with browse_context(user, root_id, namespace) as context:
            context.require_active()
            row = _entry(root_id, entry_id)
            ancestors: list[dict[str, object]] = []
            truncated = False
            available = row["_current"]
            # _entry checks the entire source/navigation chains in SQL. Stream
            # only the nearest labels in explicit depth order; the 65th proves
            # truncation. CYCLE and depth both bound this label-only recursion.
            with connection.chunked_cursor() as result:
                result.execute("""
                    WITH RECURSIVE ancestors AS (
                        SELECT id, logical_parent_id, display_name, 1 AS depth
                          FROM catalog_catalogentry WHERE id=%s AND root_id=%s
                        UNION ALL
                        SELECT p.id, p.logical_parent_id, p.display_name, a.depth + 1
                          FROM catalog_catalogentry p JOIN ancestors a ON p.id=a.logical_parent_id
                         WHERE p.root_id=%s AND a.depth < 65
                    ) CYCLE id SET cyclic USING visited
                    SELECT id, display_name, cyclic FROM ancestors ORDER BY depth
                """, [row["logical_parent_id"], root_id, root_id])
                while batch := result.fetchmany(64):
                    for ancestor_id, display, cyclic in batch:
                        if cyclic:
                            truncated = True
                        elif len(ancestors) < 64:
                            ancestors.append({
                                "id": str(ancestor_id), "displayName": display,
                            })
                        else:
                            truncated = True
            return {
                **entry_summary(row, available=available),
                "parentId": str(row["logical_parent_id"]) if row["logical_parent_id"] else None,
                "ancestors": list(reversed(ancestors)), "ancestorsTruncated": truncated,
            }
