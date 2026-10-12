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
from .filters import UNKNOWN_TYPE, FileFilter, SearchTooShort, canonical_filter_bytes
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


def _prefix_range(filters: FileFilter) -> Q:
    if not filters.prefix:
        return Q()
    prefix = filters.prefix.encode("utf-8")
    # UTF-8 ends below 0xff. Byte ranges implement literal prefix matching,
    # independent of SQL LIKE wildcards, text collation and escaping.
    upper = prefix[:-1] + bytes([prefix[-1] + 1])
    return Q(name_key__gte=prefix, name_key__lt=upper)


def _filter_q(filters: FileFilter) -> Q:
    """Every selected filter as one condition. Leaving out missing tombstones by default
    is the caller's (a branch rank or an explicit condition)."""
    condition = Q()
    if filters.kind:
        condition &= Q(kind__in=filters.kind)
    if filters.type:
        selected = Q(type_hint__in=[value for value in filters.type if value != UNKNOWN_TYPE])
        if UNKNOWN_TYPE in filters.type:
            selected |= Q(type_hint__isnull=True)
        condition &= selected
    if filters.availability:
        condition &= Q(source_state__in=filters.availability)
    if filters.size is not None:
        value = filters.size
        condition &= Q(size__isnull=value.unknown)
        if value.minimum is not None:
            condition &= Q(size__gte=value.minimum)
        if value.maximum is not None:
            condition &= Q(size__lte=value.maximum)
    if filters.modified is not None:
        modified = filters.modified
        condition &= Q(mtime_ns__isnull=modified.unknown)
        if modified.from_ns is not None:
            condition &= Q(mtime_ns__gte=modified.from_ns)
        if modified.before_ns is not None:
            condition &= Q(mtime_ns__lt=modified.before_ns)
    condition &= _prefix_range(filters)
    if filters.contains:
        # A literal LIKE '%...%': the lookup escapes %, _ and \ in the search text.
        condition &= Q(search_name__contains=filters.contains)
    return condition


def _filter(query: Any, filters: FileFilter) -> Any:
    return query.filter(_filter_q(filters))


def _key(row: dict[str, Any], sort: str) -> CursorKey:
    name = bytes(row["name_key"])
    return (row["_kind"], row["_null"], name if sort == "name" else int(row["_value"]),
            name, row["id"])


def _ranked(query: Any, sort: str, *, media: bool = True) -> tuple[Any, tuple[str, ...]]:
    """Annotate the order ranks shared by both scopes; returns the query and its keyset."""
    query = query.annotate(
        _missing=models.Case(models.When(source_state="missing", then=True), default=False),
        _kind=models.Case(models.When(kind="directory", then=0), default=1),
        **(MEDIA_ANNOTATIONS if media else {}),
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


# Planner settings for one search read: a trigram read uses bitmap scans only (never an
# ordered walk under its LIMIT n+1), and an ordered window index scans only (never a
# whole rank read and sorted).
_SCANS = {
    "bitmap": ("enable_seqscan", "enable_indexscan", "enable_indexonlyscan"),
    "index": ("enable_seqscan", "enable_bitmapscan"),
}


def _scan_rows(sql: str, params: Any, scan: str | None) -> list[dict[str, Any]]:
    """Rows of *sql*, planned with only the *scan* kind if given. A failure aborts the
    transaction, which reverts the settings."""
    if scan is None:
        return _rows(sql, params)
    with connection.cursor() as cursor:
        cursor.execute("; ".join(f"SET LOCAL {setting} = off" for setting in _SCANS[scan]))
    rows = _rows(sql, params)
    with connection.cursor() as cursor:
        cursor.execute("; ".join(f"SET LOCAL {setting} TO DEFAULT" for setting in _SCANS[scan]))
    return rows


# Whether pg_trgm extracts a trigram from LIKE '%text%'. It reads runs of letters and
# digits as the database's character type classifies them, as these regex classes do,
# padding a run with two blanks before it and one after unless that side is a wildcard
# (the ends of the text): a run after another character always yields one, a run at the
# start needs two characters and a character after them, and the whole text needs three.
_INDEXABLE_SQL = (
    "SELECT %(text)s ~ '[^[:alnum:]][[:alnum:]]' "
    "OR %(text)s ~ '^[[:alnum:]]{2,}[^[:alnum:]]' OR %(text)s ~ '^[[:alnum:]]{3,}$'"
)


def _require_indexable(text: str) -> None:
    """Refuse search text no trigram index can look up: it would read every entry."""
    with connection.cursor() as cursor:
        cursor.execute(_INDEXABLE_SQL, {"text": text})
        row = cursor.fetchone()
    if row is None or row[0] is not True:
        raise SearchTooShort()


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


def _root_keyset(sort: str, key: CursorKey, forward: bool, *, inclusive: bool = False) -> RawSQL:
    operator = (">" if forward else "<") + ("=" if inclusive else "")
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


_ROOT_PROJECTION = (
    *_SUMMARY, *MEDIA_FIELDS, "name_key", "logical_parent_id", "_kind", "_null", "_value",
)
# What each search read returns per candidate: its order key, then ``_hit`` (whether it
# matches) and ``_frontier`` (whether the read stopped there without knowing what follows).
_SEARCH_KEY = ("id", "_kind", "_null", "_value", "name_key")
# Per page row: how many file matches name search first takes from the trigram index, and
# how many entries it then reads from each ordered file branch, which share the rows of at
# most SEARCH_WINDOW_SHARE branches between them (see _root_search_rows).
SEARCH_MATCH_FACTOR = 100
SEARCH_WINDOW_FACTOR = 20
SEARCH_WINDOW_SHARE = 4


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
    if filters.contains is not None:
        return _root_search_rows(context, filters, sort, order, limit, position)
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
    projection = _ROOT_PROJECTION
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
    return _located(context, sql, params, fields, directions, previous)


def _located(
    context: BrowseContext,
    sql: str,
    params: Any,
    fields: tuple[str, ...],
    directions: tuple[bool, ...],
    previous: bool,
) -> list[dict[str, Any]]:
    """The page's candidate rows (*sql*) with ``_current`` and their parent's name."""
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


def _known(keys: list[dict[str, Any]], needed: int) -> list[dict[str, Any]] | None:
    """The matches among a read's *keys*, if every match among the first *needed* of the
    read's order is known: no ``_frontier`` comes before them (a matching one may end them).
    """
    known: list[dict[str, Any]] = []
    for key in keys:
        if key["_frontier"] and (len(known) + 1 < needed or not key["_hit"]):
            return None if len(known) < needed else known
        if key["_hit"]:
            known.append(key)
        if key["_frontier"]:
            break
    return known


def _root_search_rows(
    context: BrowseContext,
    filters: FileFilter,
    sort: str,
    order: str,
    limit: int,
    position: CursorPosition | None,
) -> list[dict[str, Any]]:
    """Entries anywhere in the root whose name contains the search text.

    A trigram index finds matches in no particular order, and an ordered index walk may
    pass any number of other names between two matches. Folders and files never mix in
    an order (folders come first), so each kind is read apart, from the first of these
    reads that decides its share of the page; the page's rows are then read by ID:

    1. The kind's trigram index, stopping after ``SEARCH_MATCH_FACTOR * (limit + 1)``
       matches past the cursor. Rare and medium terms end here, in one bitmap pass.
    2. For files, the ordered root-scope branches, as without a search (per type when
       types are selected), each reading a bounded window of entries and stopping once
       the page is full. A branch that reads its whole window marks the window's last
       entry: every match up to it is known, none after it. Common terms end here when
       their matches are dense past the cursor.
    3. The matches up to a bound. The first read's matches are a sample of more than
       ``limit + 1``, so the page needs nothing past the sample's last needed key. Read
       per rank between the cursor and that key, a root-scope index range (one per type
       selection) offers itself next to the trigram index, so the read costs the cheaper
       of the rows in that range and the matches past the cursor.

    A read that cannot decide the page marks its rows ``_frontier``. Each read is planned
    with one kind of scan only (see ``_SCANS``).
    """
    previous = position is not None and position.travel == "previous"
    ascending = order == "asc"
    named = sort == "name"
    narrow, fields = _ranked(
        CatalogEntry.objects.filter(root_id=context.root.pk, logical_parent__isnull=False),
        sort,
        media=False,
    )
    forward = ascending != previous
    directions = (
        (True, ascending, ascending) if named else (True, True, ascending, ascending, ascending)
    )
    ordering = tuple(
        field if direction != previous else f"-{field}"
        for field, direction in zip(fields, directions, strict=True)
    )

    def order_by(columns: tuple[str, ...]) -> str:
        return ", ".join(
            f'"{field}" {"ASC" if direction != previous else "DESC"}'
            for field, direction in zip(fields, directions, strict=True)
            if field in columns
        )

    merge_order = order_by(fields)
    # Within one branch the ranks are constant, so its index order is the rest of the key.
    stream_order = order_by(fields[1:] if named else fields[2:])
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
    matching = _filter_q(filters)
    if not filters.availability:
        matching &= ~Q(source_state="missing")
    keyset = None
    if position is not None:
        boundary = (position.key[0], position.key[3], position.key[4]) if named else position.key
        keyset = keyset_predicate(fields, directions, boundary, previous=previous)
    key_columns = ", ".join(f'{{0}}."{column}"' for column in _SEARCH_KEY)
    known_types = [value for value in filters.type if value != UNKNOWN_TYPE]
    Part = tuple[str, list[object]]

    def matched(query: Any, cap: int | None = None) -> Part:
        """The first ``limit + 1`` matches past the cursor; with a *cap*, among at most
        that many matches (a sample), all marked ``_frontier`` if there are more."""
        if keyset is not None:
            query = query.filter(keyset)
        sql, params = query.filter(matching).order_by().values(*_SEARCH_KEY).query.sql_with_params()
        if cap is None:
            source, flags = sql, 'true AS "_hit", false AS "_frontier"'
        else:
            source = (
                f'SELECT capped.*, count(*) OVER () AS "_matches" FROM ({sql} LIMIT {cap + 1}) '
                "AS capped"
            )
            flags = (
                f'matched."_matches" <= {cap} AS "_hit", '
                f'matched."_matches" > {cap} AS "_frontier"'
            )
        return (
            f"SELECT {key_columns.format('matched')}, {flags} FROM ({source}) AS matched "
            f"ORDER BY {merge_order} LIMIT {limit + 1}",
            list(params),
        )

    def windows() -> list[Part]:
        """File windows: one per rank and missing state, and per type when types are
        selected, sharing ``SEARCH_WINDOW_FACTOR * (limit + 1) * SEARCH_WINDOW_SHARE``
        rows (each at least a page)."""
        selections = len(known_types) + (UNKNOWN_TYPE in filters.type) or 1
        count = len(_missing_branches(filters)) * len(nulls) * selections
        budget = SEARCH_WINDOW_FACTOR * (limit + 1) * min(count, SEARCH_WINDOW_SHARE)
        window = max(limit + 1, budget // count)
        hit = models.ExpressionWrapper(matching, output_field=models.BooleanField())

        def walk(query: Any) -> tuple[str, list[object]]:
            walked = query.annotate(_hit=hit).order_by(*ordering)
            sql, params = walked.values(*_SEARCH_KEY, "_hit")[:window].query.sql_with_params()
            # Numbered in index order, so a dense window stops after its page.
            return (
                f'SELECT {key_columns.format("walked")}, walked."_hit", '
                f'walked."_walked" = {window} AS "_frontier" FROM ('
                f"SELECT walked.*, row_number() OVER (ORDER BY {stream_order}) "
                f'AS "_walked" FROM ({sql}) AS walked) AS walked '
                f'WHERE walked."_hit" OR walked."_walked" = {window} '
                f"ORDER BY {stream_order} LIMIT {limit + 1}",
                list(params),
            )

        parts = []
        for missing in _missing_branches(filters):
            for null in nulls:
                query = narrow.filter(_missing=missing, _kind=1)
                if named:
                    query = query.filter(_prefix_range(filters))
                else:
                    query = query.filter(_null=null)
                    if null == 0:
                        query = query.filter(_value_bounds(filters, sort))
                if position is not None:
                    rank, boundary = (1, null), (position.key[0], position.key[1])
                    if rank == boundary:
                        query = query.filter(_root_keyset(sort, position.key, forward))
                    elif (rank < boundary) != previous:
                        continue  # The whole branch lies behind the cursor.
                if not filters.type:
                    parts.append(walk(query))
                if UNKNOWN_TYPE in filters.type:
                    parts.append(walk(query.filter(type_hint__isnull=True)))
                if len(known_types) == 1:
                    parts.append(walk(query.filter(type_hint=known_types[0])))
                elif known_types:
                    # One planned window reads each type's keyset in turn (LATERAL).
                    sql, params = walk(query.filter(type_hint=_type_branch()))
                    values = ", ".join(["(%s)"] * len(known_types))
                    parts.append((
                        f'SELECT picked.* FROM (VALUES {values}) AS "type_branch"("type_hint") '
                        f"CROSS JOIN LATERAL ({sql}) AS picked "
                        f"ORDER BY {stream_order} LIMIT {limit + 1}",
                        [*known_types, *params],
                    ))
        return parts

    def keys(parts: list[Part], scan: str | None) -> list[dict[str, Any]]:
        if not parts:
            return []
        sql = (
            parts[0][0]
            if len(parts) == 1
            else " UNION ALL ".join(f"({part})" for part, _ in parts)
            + f" ORDER BY {merge_order} LIMIT {limit + 1}"
        )
        return _scan_rows(sql, [param for _, part in parts for param in part], scan)

    def travel(rank: tuple[int, int]) -> tuple[int, int]:
        return (-rank[0], -rank[1]) if previous else rank

    def bounded(kind: int, bound: dict[str, Any]) -> list[Part]:
        """The kind's matches past the cursor and not past *bound*, a sampled match."""
        top = _key(bound, sort)
        top_rank = (top[0], top[1])
        cursor_rank = None if position is None else (position.key[0], position.key[1])
        parts = []
        for missing in _missing_branches(filters):
            for null in nulls:
                rank = (kind, null)
                if travel(rank) > travel(top_rank) or (
                    cursor_rank is not None and travel(rank) < travel(cursor_rank)
                ):
                    continue
                # The plain kind condition keeps the partial trigram index in play.
                query = narrow.filter(_missing=missing, _kind=kind)
                query = (
                    query.filter(kind=EntryKind.DIRECTORY)
                    if kind == 0
                    else query.exclude(kind=EntryKind.DIRECTORY)
                )
                if named:
                    query = query.filter(_prefix_range(filters))
                else:
                    query = query.filter(_null=null)
                    if null == 0:
                        query = query.filter(_value_bounds(filters, sort))
                if position is not None and rank == cursor_rank:
                    query = query.filter(_root_keyset(sort, position.key, forward))
                if rank == top_rank:
                    query = query.filter(_root_keyset(sort, top, not forward, inclusive=True))
                if not filters.type:
                    parts.append(matched(query))
                if UNKNOWN_TYPE in filters.type:
                    parts.append(matched(query.filter(type_hint__isnull=True)))
                if known_types:
                    parts.append(matched(query.filter(type_hint__in=known_types)))
        return parts

    def kind_matches(kind: int, needed: int) -> list[dict[str, Any]]:
        """The kind's matches past the cursor in travel order, with all of the first
        *needed* among them."""
        query = (
            narrow.filter(kind=EntryKind.DIRECTORY)
            if kind == 0
            else narrow.exclude(kind=EntryKind.DIRECTORY)
        )
        # The sample must exceed a page for its keys to bound one (see bounded()).
        cap = max(SEARCH_MATCH_FACTOR, 1) * (limit + 1)
        sample = keys([matched(query, cap)], "bitmap")
        known = _known(sample, needed)
        if known is None and kind == 1:
            known = _known(keys(windows(), "index"), needed)
        if known is None:
            # At least *needed* matches lie at or before the sample's needed-th key.
            known = keys(bounded(kind, sample[needed - 1]), "bitmap")
        return known

    # Folders are behind a cursor among the files when reading forward, and files behind
    # one among the folders when reading backward.
    folders_ahead = 0 in kinds and (position is None or previous or position.key[0] == 0)
    files_ahead = 1 in kinds and (position is None or not previous or position.key[0] == 1)
    # In the travel order: folders before files reading forward, after them backward.
    first, second = ((1, files_ahead), (0, folders_ahead)) if previous else (
        (0, folders_ahead), (1, files_ahead)
    )
    found = kind_matches(first[0], limit + 1) if first[1] else []
    if second[1] and len(found) <= limit:
        found = [*found, *kind_matches(second[0], limit + 1 - len(found))]
    if not found:
        return []
    page = [key["id"] for key in found[: limit + 1]]
    candidates, _ = _ranked(CatalogEntry.objects.filter(root_id=context.root.pk, pk__in=page), sort)
    sql, params = candidates.order_by().values(*_ROOT_PROJECTION).query.sql_with_params()
    return _located(context, sql, params, fields, directions, previous)


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
    if filters.contains is not None:
        # In both scopes, so one filter document is valid or invalid wherever it is used.
        _require_indexable(filters.contains)
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
