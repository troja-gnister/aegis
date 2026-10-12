"""Root-bound keyset reads; application allocations depend on page size, not fanout."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from functools import reduce
from operator import or_
from typing import Any, cast
from uuid import UUID

from django.db import connection, models
from django.db.models import Q
from django.db.models.expressions import RawSQL
from django.db.models.functions import Coalesce

from aegis_apps.identity.models import User
from aegis_apps.media.links import MEDIA_ANNOTATIONS, MEDIA_FIELDS, MediaLinks
from aegis_apps.roots.manifest import ManifestError, configured_manifest
from aegis_apps.roots.permissions import Permission
from aegis_apps.roots.selectors import authorized_roots

from .authorization import (
    BrowseContext,
    CatalogNotFound,
    CatalogNotReady,
    bounded_read,
    browse_context,
    reject_not_found,
)
from .cursors import (
    BrowseScope,
    CursorContext,
    CursorKey,
    CursorPosition,
    SortName,
    SortOrder,
    decode_cursor,
    encode_cursor,
)
from .domain import EntryKind
from .filters import UNKNOWN_TYPE, FileFilter, canonical_filter_bytes
from .models import CatalogEntry
from .serializers import entry_summary, index_status

_SUMMARY = (
    "id", "root_id", "display_name", "kind", "type_hint", "size",
    "mtime_ns", "source_state", "catalog_version",
)


def media_links(context: BrowseContext) -> MediaLinks:
    """URL scope of the locked context: its namespace, root epoch and media grants."""
    return MediaLinks(
        namespace=context.namespace,
        root_epoch=context.root_epoch,
        preview=context.allows(Permission.PREVIEW),
        export=context.allows(Permission.EXPORT),
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


def source_current_sql(alias: str) -> str:
    """SQL deciding whether the row *alias* (a fixed internal alias) has a current source chain."""
    return _source_current(alias)


def navigation_current_sql(alias: str) -> str:
    """SQL deciding whether every navigation ancestor of *alias* is itself current."""
    return _navigation_current(alias)


def _entry(root_id: UUID, entry_id: UUID | None) -> dict[str, Any]:
    query = CatalogEntry.objects.filter(root_id=root_id)
    query = (
        query.filter(source_parent__isnull=True) if entry_id is None else query.filter(pk=entry_id)
    )
    # At most one row matches (the anchor is unique per root). Unordered, so the anchor
    # is read from its partial unique index instead of an ID-ordered walk of the root.
    rows: list[dict[str, Any]] = list(
        query.annotate(
            _current=RawSQL(
                f"{_source_current('catalog_catalogentry')} "
                f"AND {_navigation_current('catalog_catalogentry')}",
                (),
                output_field=models.BooleanField(),
            ),
            **MEDIA_ANNOTATIONS,
        )
        .values(
            *_SUMMARY,
            *MEDIA_FIELDS,
            "logical_parent_id",
            "children_version",
            "_current",
        )
        .order_by()[:1]
    )
    row = rows[0] if rows else None
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


def _ranked(query: Any, sort: str) -> tuple[Any, tuple[str, ...]]:
    """Annotate the order ranks shared by both scopes; returns the query and its keyset."""
    query = query.annotate(
        _missing=models.Case(models.When(source_state="missing", then=True), default=False),
        _kind=models.Case(models.When(kind="directory", then=0), default=1),
        **MEDIA_ANNOTATIONS,
    )
    if sort == "name":
        query = query.annotate(_null=models.Value(0), _value=models.F("name_key"))
        return query, ("_kind", "name_key", "id")
    value_field = "mtime_ns" if sort == "modified" else "size"
    query = query.annotate(
        _null=models.Case(models.When(**{f"{value_field}__isnull": True}, then=1), default=0),
        _value=Coalesce(value_field, 0, output_field=models.DecimalField()),
    )
    return query, ("_kind", "_null", "_value", "name_key", "id")


def _missing_branches(filters: FileFilter) -> tuple[bool, ...]:
    if "missing" not in filters.availability:
        return (False,)
    return (True,) if len(filters.availability) == 1 else (False, True)


def _rows(sql: str, params: Any) -> list[dict[str, Any]]:
    with connection.cursor() as result:
        result.execute(sql, params)
        assert result.description is not None
        columns = [column[0] for column in result.description]
        return [dict(zip(columns, row, strict=True)) for row in result.fetchall()]


def _directory_rows(
    parent: dict[str, Any],
    filters: FileFilter,
    sort: str,
    order: str,
    limit: int,
    position: CursorPosition | None,
) -> list[dict[str, Any]]:
    """One directory's children; ``_current`` also requires the directory to be current."""
    previous = position is not None and position.travel == "previous"
    ascending = order == "asc"
    query, fields = _ranked(
        CatalogEntry.objects.filter(root_id=parent["root_id"], logical_parent_id=parent["id"]),
        sort,
    )
    if sort == "name":
        directions: tuple[bool, ...] = (True, ascending, ascending)
        boundary: tuple[object, ...] = (
            (position.key[0], position.key[3], position.key[4]) if position else ()
        )
    else:
        directions = (True, True, ascending, ascending, ascending)
        boundary = position.key if position else ()
    query = _filter(query, filters)
    if position:
        query = query.filter(keyset_predicate(fields, directions, boundary, previous=previous))
    ordering = tuple(
        field if direction != previous else f"-{field}"
        for field, direction in zip(fields, directions, strict=True)
    )
    projection = (*_SUMMARY, *MEDIA_FIELDS, "name_key", "_kind", "_null", "_value")
    candidates = [query.filter(_missing=missing).order_by(*ordering).values(*projection)[:limit + 1]
                  for missing in _missing_branches(filters)]
    selected = candidates[0]
    if len(candidates) == 2:
        selected = selected.union(candidates[1], all=True).order_by(*ordering)[:limit + 1]
    sql, params = selected.query.sql_with_params()
    final_order = ", ".join(f'"{field}" {"ASC" if direction != previous else "DESC"}'
                            for field, direction in zip(fields, directions, strict=True))
    rows = _rows(
        f"WITH candidates AS MATERIALIZED ({sql}) SELECT candidates.*, "
        f"{_source_current('candidates')} AS _current FROM candidates ORDER BY {final_order}",
        params,
    )
    for row in rows:
        row["_current"] = bool(parent["_current"] and row["_current"])
    return rows


# The keyset columns of the root-scope indexes after their equality ranks. The text
# must stay the indexed expressions so a row comparison seeks within the index.
_ROOT_KEYSET_SQL = {
    "name": '("catalog_catalogentry"."name_key", "catalog_catalogentry"."id")',
    "modified": '(COALESCE("catalog_catalogentry"."mtime_ns", 0), '
    '"catalog_catalogentry"."name_key", "catalog_catalogentry"."id")',
    "size": '(COALESCE("catalog_catalogentry"."size", 0), '
    '"catalog_catalogentry"."name_key", "catalog_catalogentry"."id")',
}


def _root_keyset(sort: str, key: CursorKey, forward: bool) -> RawSQL:
    operator = ">" if forward else "<"
    if sort == "name":
        return RawSQL(
            f"{_ROOT_KEYSET_SQL[sort]} {operator} (%s, %s)",
            (key[3], key[4]),
            output_field=models.BooleanField(),
        )
    return RawSQL(
        f"{_ROOT_KEYSET_SQL[sort]} {operator} (CAST(%s AS numeric), %s, %s)",
        (key[2], key[3], key[4]),
        output_field=models.BooleanField(),
    )


def _value_bounds(filters: FileFilter, sort: str) -> Q:
    """The filtered range of the sort field, restated on the indexed sort value.

    Only for rows whose value is known, where both are equal; the index then seeks to
    the range instead of filtering every row before it.
    """
    bounds = Q()
    if sort == "size" and filters.size is not None and not filters.size.unknown:
        if filters.size.minimum is not None:
            bounds &= Q(_value__gte=filters.size.minimum)
        if filters.size.maximum is not None:
            bounds &= Q(_value__lte=filters.size.maximum)
    if sort == "modified" and filters.modified is not None and not filters.modified.unknown:
        if filters.modified.from_ns is not None:
            bounds &= Q(_value__gte=filters.modified.from_ns)
        if filters.modified.before_ns is not None:
            bounds &= Q(_value__lt=filters.modified.before_ns)
    return bounds


def _type_branch() -> RawSQL:
    """The type value of the current row of a LATERAL type branch."""
    return RawSQL('"type_branch"."type_hint"', (), output_field=models.CharField())


def _root_rows(
    context: BrowseContext,
    filters: FileFilter,
    sort: str,
    order: str,
    limit: int,
    position: CursorPosition | None,
) -> list[dict[str, Any]]:
    """Matching entries anywhere in the root, merged from bounded indexed branches.

    Each branch fixes the missing state, kind rank and null rank, the order prefix of the
    root-scope indexes, so it reads one of them in keyset order with ``LIMIT n+1`` and one
    ascending index serves both orders. A type filter reads each type value's keyset in
    turn, all from one LATERAL branch per rank (and an unknown type from its own). The
    branches are merged in SQL; ``_current`` and ``_parent_name`` come from one join on
    the page's distinct parent directories.
    """
    previous = position is not None and position.travel == "previous"
    ascending = order == "asc"
    named = sort == "name"
    base, fields = _ranked(
        CatalogEntry.objects.filter(root_id=context.root.pk, logical_parent__isnull=False),
        sort,
    )
    base = _filter(base, replace(filters, type=()))
    directions = (
        (True, ascending, ascending) if named else (True, True, ascending, ascending, ascending)
    )
    ordering = tuple(
        field if direction != previous else f"-{field}"
        for field, direction in zip(fields, directions, strict=True)
    )
    kinds = (
        tuple(sorted({0 if kind == EntryKind.DIRECTORY else 1 for kind in filters.kind}))
        if filters.kind
        else (0, 1)
    )
    selected_range = filters.size if sort == "size" else filters.modified
    nulls = (
        (0,)
        if named
        else ((0, 1) if selected_range is None else (1,) if selected_range.unknown else (0,))
    )
    projection = (
        *_SUMMARY,
        *MEDIA_FIELDS,
        "name_key",
        "logical_parent_id",
        "_kind",
        "_null",
        "_value",
    )
    parts: list[str] = []
    params: list[object] = []
    merge_order = ", ".join(
        f'"{field}" {"ASC" if direction != previous else "DESC"}'
        for field, direction in zip(fields, directions, strict=True)
    )

    def add(branch: Any) -> None:
        sql, branch_params = branch[: limit + 1].query.sql_with_params()
        parts.append(sql)
        params.extend(branch_params)

    def add_per_type(branch: Any, type_values: list[str]) -> None:
        # One planned branch reads each type value's keyset in turn (LATERAL), each with
        # its own LIMIT n+1, so planning does not grow with the number of selected types.
        sql, branch_params = branch[: limit + 1].query.sql_with_params()
        rows = ", ".join(["(%s)"] * len(type_values))
        parts.append(
            f'SELECT picked.* FROM (VALUES {rows}) AS "type_branch"("type_hint") '
            f"CROSS JOIN LATERAL ({sql}) AS picked ORDER BY {merge_order} LIMIT {limit + 1}"
        )
        params.extend((*type_values, *branch_params))

    known_types = [value for value in filters.type if value != UNKNOWN_TYPE]
    for missing in _missing_branches(filters):
        for kind in kinds:
            for null in nulls:
                query = base.filter(_missing=missing, _kind=kind)
                if not named:
                    query = query.filter(_null=null)
                    if null == 0:
                        query = query.filter(_value_bounds(filters, sort))
                if position is not None:
                    rank, boundary = (kind, null), (position.key[0], position.key[1])
                    if rank == boundary:
                        forward = ascending != previous
                        query = query.filter(_root_keyset(sort, position.key, forward))
                    elif (rank < boundary) != previous:
                        continue  # The whole branch lies behind the cursor.
                shaped = query.order_by(*ordering).values(*projection)
                if not filters.type:
                    add(shaped)
                if UNKNOWN_TYPE in filters.type:
                    add(shaped.filter(type_hint__isnull=True))
                if len(known_types) == 1:
                    add(shaped.filter(type_hint=known_types[0]))
                elif known_types:
                    add_per_type(shaped.filter(type_hint=_type_branch()), known_types)
    if not parts:
        return []
    sql = (
        parts[0]
        if len(parts) == 1
        else (
            " UNION ALL ".join(f"({part})" for part in parts)
            + f" ORDER BY {merge_order} LIMIT {limit + 1}"
        )
    )
    final_order = ", ".join(
        f'candidates."{field}" {"ASC" if direction != previous else "DESC"}'
        for field, direction in zip(fields, directions, strict=True)
    )
    rows = _rows(
        f"WITH candidates AS MATERIALIZED ({sql}), parents AS MATERIALIZED ("
        "SELECT parent.id, parent.display_name, parent.kind = 'directory' "
        f"AND {_source_current('parent')} AND {_navigation_current('parent')} AS current "
        "FROM catalog_catalogentry parent WHERE parent.root_id=%s "
        "AND parent.id IN (SELECT logical_parent_id FROM candidates)"
        ") SELECT candidates.*, parents.display_name AS _parent_name, "
        f"COALESCE(parents.current, false) AND {_source_current('candidates')} AS _current "
        "FROM candidates LEFT JOIN parents ON parents.id=candidates.logical_parent_id "
        f"ORDER BY {final_order}",
        (*params, context.root.pk),
    )
    for row in rows:
        row["_current"] = row["_current"] is True
    return rows


def directory_page(
    context: BrowseContext,
    parent_id: UUID | None,
    filters: FileFilter,
    sort: str,
    order: str,
    limit: int,
    cursor: str | None,
    scope: str = "directory",
) -> dict[str, object]:
    """One keyset page of a directory's children (``directory`` scope), or of matching
    entries anywhere in the root (``root`` scope, which takes no parent: the root
    anchor's identity binds its cursors). Root-scope entries also name their parent
    directory (``parentId``, ``parentName``); the root anchor's name is empty."""
    context.require_active()
    if (
        sort not in ("name", "modified", "size")
        or order not in ("asc", "desc")
        or (type(limit) is not int or not 1 <= limit <= 250)
        or scope not in ("directory", "root")
        or (scope == "root" and parent_id is not None)
    ):
        raise ValueError("invalid_catalog_query")
    parent = _entry(context.root.pk, parent_id)
    if parent["kind"] != "directory":
        raise CatalogNotFound()
    cursor_context = CursorContext(
        context.user.pk,
        context.namespace,
        context.user_epoch,
        context.root.pk,
        context.root_epoch,
        parent["id"],
        parent["children_version"],
        hashlib.sha256(canonical_filter_bytes(filters)).hexdigest(),
        cast(SortName, sort),
        cast(SortOrder, order),
        limit,
        cast(BrowseScope, scope),
    )
    position = decode_cursor(cursor, cursor_context) if cursor is not None else None
    previous = position is not None and position.travel == "previous"
    rows = (
        _root_rows(context, filters, sort, order, limit, position)
        if scope == "root"
        else _directory_rows(parent, filters, sort, order, limit, position)
    )
    more = len(rows) > limit
    rows = rows[:limit]
    if previous:
        rows.reverse()
    has_next = bool(position) if previous else more
    has_previous = more if previous else bool(position)
    links = media_links(context)
    entries = [entry_summary(row, available=row["_current"], links=links) for row in rows]
    if scope == "root":
        entries = [
            {
                **entry,
                "parentId": str(row["logical_parent_id"]),
                "parentName": row["_parent_name"] or "",
            }
            for entry, row in zip(entries, rows, strict=True)
        ]
    return {
        "entries": entries,
        "nextCursor": encode_cursor(cursor_context, _key(rows[-1], sort), "next")
        if rows and has_next else None,
        "previousCursor": encode_cursor(cursor_context, _key(rows[0], sort), "previous")
        if rows and has_previous else None,
        "directoryId": str(parent["id"]), "directoryVersion": str(parent["children_version"]),
        "contractVersion": 1, "indexStatus": index_status(context.root.pk,
                                                        available=parent["_current"]),
    }


def entry_details(user: User, entry_id: UUID, namespace: str) -> dict[str, object]:
    requested_epoch = user.authorization_epoch
    with bounded_read():
        try:
            manifest = configured_manifest()
        except ManifestError:
            reject_not_found(user, requested_epoch)
        roots = authorized_roots(
            user_id=user.pk, active_manifest_slot_ids=tuple(manifest.slots) if manifest else (),
        ).values("id")
        root_id = CatalogEntry.objects.filter(pk=entry_id, root_id__in=roots).values_list(
            "root_id", flat=True,
        ).first()
        if root_id is None:
            reject_not_found(user, requested_epoch)
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
                **entry_summary(row, available=available, links=media_links(context)),
                "parentId": str(row["logical_parent_id"]) if row["logical_parent_id"] else None,
                "ancestors": list(reversed(ancestors)), "ancestorsTruncated": truncated,
            }
