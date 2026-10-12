"""The ``root`` browse scope: filtered keyset pages over a whole root, merged from branches."""

from __future__ import annotations

import json
import uuid
from itertools import pairwise
from typing import Any

import pytest
from aegis_apps.catalog.cursors import CursorRestartRequired
from aegis_apps.catalog.filters import parse_filters
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.db import connection, transaction

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

ROOT_INDEXES = {
    "name": "catalog_root_name",
    "modified": "catalog_root_modified",
    "size": "catalog_root_size",
}


def _tree(browse_fixture: Any) -> dict[str, CatalogEntry]:
    """Nested folders holding photos; a folder whose own name carries a photo extension."""
    entry = browse_fixture.entry
    dcim = entry(b"DCIM", kind="directory")
    camera = entry(b"100CANON", kind="directory", parent=dcim)
    trip = entry(b"trip.jpg", kind="directory")
    return {
        "DCIM": dcim,
        "100CANON": camera,
        "trip.jpg": trip,
        "top.jpg": entry(b"top.jpg", size=30, mtime_ns=3),
        "IMG_0001.JPG": entry(b"IMG_0001.JPG", parent=camera, size=10, mtime_ns=1),
        "IMG_0002.jpeg": entry(b"IMG_0002.jpeg", parent=camera, size=20, mtime_ns=2),
        "inside.jpg": entry(b"inside.jpg", parent=trip, size=None, mtime_ns=None),
        "notes.txt": entry(b"notes.txt", parent=dcim, size=5, mtime_ns=5),
        "gone.jpg": entry(b"gone.jpg", parent=camera, state="missing", size=1, mtime_ns=1),
    }


def _names(page: dict[str, Any]) -> list[str]:
    return [item["displayName"] for item in page["entries"]]


def test_root_scope_finds_matches_in_nested_folders_with_their_folder(
    browse_fixture: Any,
) -> None:
    tree = _tree(browse_fixture)
    jpg = parse_filters({"v": 1, "type": ["jpg"]})
    # The directory listing keeps its contract: the anchor's own children only.
    folder = browse_fixture.page(filters=jpg)
    assert _names(folder) == ["trip.jpg", "top.jpg"]
    assert all(
        set(item)
        == {
            "id",
            "rootId",
            "displayName",
            "kind",
            "typeHint",
            "size",
            "modifiedNs",
            "sourceState",
            "version",
            "media",
        }
        for item in folder["entries"]
    )

    page = browse_fixture.page(filters=jpg, scope="root")
    # Folders first, then files by name; the missing tombstone is not selected.
    assert _names(page) == ["trip.jpg", "IMG_0001.JPG", "inside.jpg", "top.jpg"]
    located = {
        item["displayName"]: (item["parentId"], item["parentName"]) for item in page["entries"]
    }
    anchor = str(browse_fixture.anchor.pk)
    assert located == {
        "trip.jpg": (anchor, ""),
        "top.jpg": (anchor, ""),
        "IMG_0001.JPG": (str(tree["100CANON"].pk), "100CANON"),
        "inside.jpg": (str(tree["trip.jpg"].pk), "trip.jpg"),
    }
    assert page["directoryId"] == anchor
    assert page["nextCursor"] is None and page["previousCursor"] is None
    raw = json.dumps(page)
    assert all(word not in raw for word in ("raw_name", "rawName", "inode", "device", "/srv/"))

    both = browse_fixture.page(
        filters=parse_filters({"v": 1, "type": ["jpeg", "jpg"], "kind": ["file"]}),
        scope="root",
    )
    assert _names(both) == ["IMG_0001.JPG", "IMG_0002.jpeg", "inside.jpg", "top.jpg"]
    everything = browse_fixture.page(scope="root")
    assert len(everything["entries"]) == 8  # Every entry but the anchor and the tombstone.
    missing = browse_fixture.page(
        filters=parse_filters({"v": 1, "availability": ["missing"]}),
        scope="root",
    )
    assert _names(missing) == ["gone.jpg"]
    assert missing["entries"][0]["sourceState"] == "missing"


def test_root_scope_reports_availability_like_the_directory_listing(
    browse_fixture: Any,
) -> None:
    tree = _tree(browse_fixture)
    # A folder replaced on disk and not read again yet: its children's chain is stale.
    CatalogEntry.objects.filter(pk=tree["DCIM"].pk).update(source_revision=1)
    jpg = parse_filters({"v": 1, "type": ["jpg"]})
    states = {
        item["displayName"]: item["sourceState"]
        for item in browse_fixture.page(filters=jpg, scope="root")["entries"]
    }
    # The last indexed match is listed as currently inaccessible, as in its folder.
    assert states == {
        "trip.jpg": "present",
        "IMG_0001.JPG": "inaccessible",
        "inside.jpg": "present",
        "top.jpg": "present",
    }
    directory = browse_fixture.page(filters=jpg, parent_id=tree["100CANON"].pk)
    assert [item["sourceState"] for item in directory["entries"]] == ["inaccessible"]


def test_root_scope_omits_a_vanished_folder_s_subtree(browse_fixture: Any) -> None:
    tree = _tree(browse_fixture)
    # As the indexer leaves a deleted or moved folder: missing with everything below it.
    below = ("DCIM", "100CANON", "IMG_0001.JPG", "IMG_0002.jpeg", "notes.txt")
    CatalogEntry.objects.filter(pk__in=[tree[name].pk for name in below]).update(
        source_state="missing",
    )
    jpg = parse_filters({"v": 1, "type": ["jpg"]})
    assert _names(browse_fixture.page(filters=jpg, scope="root")) == [
        "trip.jpg",
        "inside.jpg",
        "top.jpg",
    ]
    tombstones = parse_filters({"v": 1, "type": ["jpg"], "availability": ["missing"]})
    missing = browse_fixture.page(filters=tombstones, scope="root")
    assert {item["displayName"]: item["sourceState"] for item in missing["entries"]} == {
        "IMG_0001.JPG": "missing",
        "gone.jpg": "missing",
    }


def _rank(entry: CatalogEntry, sort: str) -> tuple[int, int]:
    value = entry.mtime_ns if sort == "modified" else entry.size
    return (0 if entry.kind == "directory" else 1, 0 if sort == "name" or value is not None else 1)


def _expected(entries: list[CatalogEntry], sort: str, order: str) -> list[str]:
    def rest(entry: CatalogEntry) -> tuple[Any, ...]:
        name = bytes(entry.name_key)
        value = entry.mtime_ns if sort == "modified" else entry.size
        head: tuple[Any, ...] = () if sort == "name" else (int(value or 0),)
        return (*head, name, entry.id)

    groups: dict[tuple[int, int], list[CatalogEntry]] = {}
    for entry in entries:
        groups.setdefault(_rank(entry, sort), []).append(entry)
    return [
        str(entry.id)
        for rank in sorted(groups)
        for entry in sorted(groups[rank], key=rest, reverse=order == "desc")
    ]


def _seed_mixed(browse_fixture: Any) -> list[CatalogEntry]:
    folders = [browse_fixture.anchor]
    for depth in range(3):
        folders.append(
            browse_fixture.entry(
                f"level-{depth}".encode(),
                kind="directory",
                parent=folders[-1],
                size=4096,
                mtime_ns=depth,
            )
        )
    entries = list(folders[1:])
    extensions = ("jpg", "JPEG", "png", "txt", "")
    for index in range(150):
        extension = extensions[index % 5]
        name = f"item-{index % 37:02d}-{index:03d}" + (f".{extension}" if extension else "")
        kind = "directory" if index % 11 == 0 else "symlink" if index % 13 == 0 else "file"
        entries.append(
            browse_fixture.entry(
                name.encode(),
                parent=folders[index % 4],
                kind=kind,
                size=None if index % 9 == 0 else index % 6,
                mtime_ns=None if index % 10 == 0 else (index % 4) - 2,
                state="missing" if index % 17 == 0 else "present",
            )
        )
    return entries


@pytest.mark.parametrize("sort", ["name", "modified", "size"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize(
    "selection",
    [
        {},
        {"type": ["jpg"]},
        {"type": ["jpeg", "jpg", "png", "__unknown__"]},
        {"type": ["jpg", "png"], "availability": ["present", "missing"]},
        {"kind": ["file", "symlink"], "size": {"min": "1", "max": "4"}},
        {"modified": {"unknown": True}},
    ],
)
def test_merged_branches_page_in_full_key_order_both_ways(
    browse_fixture: Any,
    sort: str,
    order: str,
    selection: dict[str, Any],
) -> None:
    entries = _seed_mixed(browse_fixture)
    filters = parse_filters({"v": 1, **selection})
    states = filters.availability or ("present", "unsupported", "inaccessible")
    wanted = [
        entry
        for entry in entries
        if entry.source_state in states
        and (not filters.type or (entry.type_hint or "__unknown__") in filters.type)
        and (not filters.kind or entry.kind in filters.kind)
        and (filters.size is None or (entry.size is not None and 1 <= entry.size <= 4))
        and (filters.modified is None or entry.mtime_ns is None)
    ]
    options = {"sort": sort, "order": order, "limit": 7, "filters": filters, "scope": "root"}
    pages = browse_fixture.walk(**options)
    assert [item["id"] for page in pages for item in page["entries"]] == _expected(
        wanted,
        sort,
        order,
    )
    for previous, current in pairwise(pages):
        back = browse_fixture.page(cursor=current["previousCursor"], **options)
        assert back["entries"] == previous["entries"]
    assert all(
        " OFFSET " not in sql.upper() and "COUNT(" not in sql.upper()
        for sql in browse_fixture.statements
    )


def test_root_scope_cursor_survives_only_its_own_context(browse_fixture: Any) -> None:
    _seed_mixed(browse_fixture)
    jpg = parse_filters({"v": 1, "type": ["jpg"]})
    page = browse_fixture.page(filters=jpg, scope="root", limit=3)
    assert page["nextCursor"]
    for changed in (
        {"filters": jpg, "scope": "directory"},
        {"filters": parse_filters({"v": 1, "type": ["png"]}), "scope": "root"},
        {"filters": jpg, "scope": "root", "sort": "size"},
    ):
        with pytest.raises(CursorRestartRequired):
            browse_fixture.page(limit=3, cursor=page["nextCursor"], **changed)


def test_root_scope_refuses_a_parent(browse_fixture: Any) -> None:
    folder = browse_fixture.entry(b"folder", kind="directory")
    with pytest.raises(ValueError, match="invalid_catalog_query"):
        browse_fixture.page(parent_id=folder.pk, scope="root")
    with pytest.raises(ValueError, match="invalid_catalog_query"):
        browse_fixture.page(scope="everywhere")


def test_root_scope_page_keeps_the_statement_budget(browse_fixture: Any) -> None:
    _seed_mixed(browse_fixture)
    filters = parse_filters({"v": 1, "type": ["jpeg", "jpg", "png"]})
    assert len(browse_fixture.page(filters=filters, scope="root", limit=2)["entries"]) == 2
    sql = [" ".join(value.upper().split()) for value in browse_fixture.statements]
    data = [value for value in sql if value.startswith(("SELECT", "WITH"))]
    assert len(data) <= 8, data
    assert not any("FOR UPDATE" in value for value in sql)
    statement = next(value for value in sql if value.startswith("WITH CANDIDATES"))
    # Per kind rank, one LATERAL read of every type's keyset (LIMIT n+1 each) and its
    # top-N (LIMIT n+1); then the merge of both ranks. No SQL branch per type value.
    assert statement.count(" LATERAL ") == 2
    assert statement.count("LIMIT 3") == 5
    assert "OFFSET" not in statement


def _descendants(
    node: dict[str, Any],
    ancestors: tuple[dict[str, Any], ...] = (),
) -> list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]]:
    """Every plan node with its ancestors, nearest last."""
    nodes = [(node, ancestors)]
    for child in node.get("Plans", []):
        nodes.extend(_descendants(child, (*ancestors, node)))
    return nodes


def _candidate_plan(statement: str) -> dict[str, Any]:
    """The plan of the candidates CTE: the merged, bounded branch reads."""
    with transaction.atomic(), connection.cursor() as cursor:
        # A small fixture may prefer a seqscan; this proves the composite indexes can
        # supply every branch's order. Physical scale is measured separately.
        cursor.execute("ANALYZE catalog_catalogentry")
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement)
        document = cursor.fetchone()[0]
    return next(
        node
        for node, _ in _descendants(document[0]["Plan"])
        if node.get("Subplan Name") == "CTE candidates"
    )


@pytest.mark.parametrize("sort", ["name", "size", "modified"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("types", [(), ("jpg",), ("jpeg", "jpg", "png")])
def test_root_indexes_serve_every_branch_and_seek_past_the_cursor(
    browse_fixture: Any,
    sort: str,
    order: str,
    types: tuple[str, ...],
) -> None:
    folders = [
        browse_fixture.entry(f"folder-{index}".encode(), kind="directory") for index in range(3)
    ]
    for folder in folders:
        browse_fixture.seed_ties(200, parent=folder)
    for number, type_hint in enumerate(("jpg", "jpeg", "png") * 40):
        browse_fixture.entry(
            f"photo-{number:03d}.{type_hint}".encode(),
            parent=folders[number % 3],
            size=number % 4,
            mtime_ns=number % 5,
        )
    filters = parse_filters({"v": 1, **({"type": list(types)} if types else {})})
    options = {"sort": sort, "order": order, "limit": 2, "filters": filters, "scope": "root"}
    first = browse_fixture.page(**options)
    browse_fixture.statements.clear()
    browse_fixture.page(cursor=first["nextCursor"], **options)
    statement = next(sql for sql in browse_fixture.statements if sql.startswith("WITH candidates"))
    candidates = _candidate_plan(statement)
    assert candidates["Actual Rows"] <= 3
    nodes = _descendants(candidates)
    index = f"catalog_root_type_{sort}" if types else ROOT_INDEXES[sort]
    scans = [
        (node, above)
        for node, above in nodes
        if node.get("Relation Name") == "catalog_catalogentry"
    ]
    assert scans and all(node.get("Index Name") == index for node, _ in scans), (statement, scans)
    assert not any(node["Node Type"] == "Seq Scan" for node, _ in nodes), candidates
    # Every branch read is a keyset in index order under its own LIMIT n+1 ...
    assert all(any(parent["Node Type"] == "Limit" for parent in above) for _, above in scans)
    reads = sum(node["Actual Loops"] for node, _ in scans)
    if len(types) > 1:
        # ... one per type value, from a single planned LATERAL branch per rank.
        assert all(node["Actual Loops"] == len(types) for node, _ in scans), scans
    # ... so a merge or top-N sort only ever sees the bounded branch rows.
    for node, _ in nodes:
        if node["Node Type"] in ("Sort", "Incremental Sort"):
            source = node["Plans"][0]
            assert source["Actual Rows"] * source["Actual Loops"] <= 3 * reads, candidates
    # The cursor boundary is an index condition (a seek), never a filter over earlier rows.
    assert any(
        ">" in node.get("Index Cond", "") or "<" in node.get("Index Cond", "") for node, _ in scans
    ), scans


def test_root_scope_follows_grants_like_the_directory_listing(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import CatalogNotFound

    _tree(browse_fixture)
    foreign = Root.objects.create(
        slot_id=f"foreign-{uuid.uuid4().hex}",
        display_name="Foreign root",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    anchor = CatalogEntry.objects.create(
        root=foreign,
        raw_name=b"",
        display_name="",
        name_key=b"",
        kind="directory",
    )
    CatalogEntry.objects.create(
        root=foreign,
        source_parent=anchor,
        logical_parent=anchor,
        raw_name=b"foreign.jpg",
        display_name="foreign.jpg",
        name_key=b"foreign.jpg",
        type_hint="jpg",
        kind="file",
    )
    jpg = parse_filters({"v": 1, "type": ["jpg"]})
    assert "foreign.jpg" not in _names(browse_fixture.page(filters=jpg, scope="root"))
    RootGrant.objects.filter(root=browse_fixture.root, user=browse_fixture.user).update(
        permissions=int(Permission.PREVIEW),
    )
    with pytest.raises(CatalogNotFound):
        browse_fixture.page(filters=jpg, scope="root")
    browse_fixture.user = User.objects.create_user(username=f"ungranted-{uuid.uuid4().hex}")
    with pytest.raises(CatalogNotFound):
        browse_fixture.page(filters=jpg, scope="root")
