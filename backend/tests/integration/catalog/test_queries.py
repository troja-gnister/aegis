import uuid
from itertools import pairwise
from typing import Any

import pytest
from aegis_apps.catalog.filters import parse_filters
from aegis_apps.catalog.models import CatalogEntry

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


@pytest.mark.parametrize("sort", ["name", "modified", "size"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_bidirectional_keysets_cover_ties_without_offset(
    browse_fixture: Any, sort: str, order: str,
) -> None:
    browse_fixture.seed_ties(count=603)
    pages = browse_fixture.walk(sort=sort, order=order, limit=100)
    ids = [entry["id"] for page in pages for entry in page["entries"]]
    assert len(ids) == len(set(ids)) == 603
    previous = browse_fixture.page(
        cursor=pages[-1]["previousCursor"], sort=sort, order=order, limit=100,
    )
    assert previous["entries"] == pages[-2]["entries"]
    assert all(" OFFSET " not in sql.upper() for sql in browse_fixture.statements)
    assert all("COUNT(" not in sql.upper() for sql in browse_fixture.statements)
    rows = [entry for page in pages for entry in page["entries"]]
    kinds = [entry["kind"] == "directory" for entry in rows]
    assert kinds == sorted(kinds, reverse=True)
    for kind in ("directory", "file"):
        group = [entry for entry in rows if entry["kind"] == kind]
        field = {"name": "displayName", "size": "size", "modified": "modifiedNs"}[sort]
        values = [entry[field] for entry in group if entry[field] is not None]
        if sort != "name":
            values = list(map(int, values))
        assert values == sorted(values, reverse=order == "desc")
        assert [entry[field] is None for entry in group] == sorted(
            entry[field] is None for entry in group
        )


@pytest.mark.parametrize(("filters", "wanted"), [
    ({}, ["a.txt", "b.unknown", "c", "d.txt"]),
    ({"kind": ["directory"]}, ["d.txt"]),
    ({"type": ["__unknown__"]}, ["c"]),
    ({"type": ["unknown"]}, ["b.unknown"]),
    ({"size": {"min": "10", "max": "10"}}, ["a.txt"]),
    ({"size": {"unknown": True}}, ["c", "d.txt"]),
    ({"modified": {"from": "1970-01-01T00:00:00.000000010Z",
                    "before": "1970-01-01T00:00:00.000000011Z"}}, ["a.txt"]),
    ({"modified": {"unknown": True}}, ["c", "d.txt"]),
    ({"availability": ["missing"]}, ["e.txt"]),
    ({"availability": ["present", "missing"]}, ["a.txt", "d.txt", "e.txt"]),
    ({"availability": ["inaccessible", "unsupported"]}, ["b.unknown", "c"]),
])
def test_filters_are_applied_before_bounded_selection(
    browse_fixture: Any, filters: dict[str, Any], wanted: list[str],
) -> None:
    for raw, state, size, ns, kind in [
        (b"a.txt", "present", 10, 10, "file"),
        (b"b.unknown", "inaccessible", 11, 11, "file"),
        (b"c", "unsupported", None, None, "file"),
        (b"d.txt", "present", None, None, "directory"),
        (b"e.txt", "missing", 10, 10, "file"),
    ]:
        browse_fixture.entry(raw, state=state, size=size, mtime_ns=ns, kind=kind)
    pages = browse_fixture.walk(filters=parse_filters({"v": 1, **filters}), limit=1)
    assert sorted(e["displayName"] for p in pages for e in p["entries"]) == sorted(wanted)


@pytest.mark.parametrize(("raw", "prefix"), [
    (b"100%.txt", "100%"), (b"a_b", "a_"), (b"a\\b", "a\\\\"),
    ("Éclair".encode(), "E\u0301"), ("Straße".encode(), "STRASS"),
])
def test_prefix_is_literal_normalized_text(
    browse_fixture: Any, raw: bytes, prefix: str,
) -> None:
    expected = browse_fixture.entry(raw)
    browse_fixture.entry(b"100X.txt")
    browse_fixture.entry(b"acb")
    page = browse_fixture.page(filters=parse_filters({"v": 1, "prefix": prefix}))
    assert [entry["id"] for entry in page["entries"]] == [str(expected.pk)]


def test_full_name_key_and_uuid_break_numeric_ties(browse_fixture: Any) -> None:
    second = browse_fixture.entry(b"A", id=uuid.UUID(int=2), size=2**64-1, mtime_ns=-(2**63))
    first = browse_fixture.entry(b"a", id=uuid.UUID(int=1), size=2**64-1, mtime_ns=-(2**63))
    pages = browse_fixture.walk(sort="size", limit=1)
    assert [p["entries"][0]["id"] for p in pages] == [str(first.pk), str(second.pk)]
    assert pages[0]["entries"][0]["size"] == str(2**64-1)
    assert pages[0]["entries"][0]["modifiedNs"] == str(-(2**63))


def test_tombstone_only_directory_and_cursor_parent_revision(browse_fixture: Any) -> None:
    from aegis_apps.catalog.cursors import CursorRestartRequired

    browse_fixture.entry(b"missing", state="missing")
    assert browse_fixture.page()["entries"] == []
    browse_fixture.seed_ties(3)
    page = browse_fixture.page(limit=1)
    CatalogEntry.objects.filter(pk=browse_fixture.anchor.pk).update(children_version=1)
    with pytest.raises(CursorRestartRequired):
        browse_fixture.page(limit=1, cursor=page["nextCursor"])


@pytest.mark.parametrize("sort", ["name", "size", "modified"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_literal_full_key_order_and_round_trip(browse_fixture: Any, sort: str, order: str) -> None:
    # Equal numeric keys fall through to the complete binary name and UUID.
    fixtures = [
        (1, b"a", "directory", 2), (2, b"b", "directory", 1),
        (3, b"c", "directory", None), (4, b"d", "file", None),
        (5, b"e", "file", 2), (6, b"f", "file", 2),
        (7, b"F", "file", 2), (8, b"g", "symlink", 1),
    ]
    for number, raw, kind, value in fixtures:
        browse_fixture.entry(raw, id=uuid.UUID(int=number), kind=kind, size=value, mtime_ns=value)
    expected = {
        ("name", "asc"): [1, 2, 3, 4, 5, 6, 7, 8],
        ("name", "desc"): [3, 2, 1, 8, 7, 6, 5, 4],
        ("size", "asc"): [2, 1, 3, 8, 5, 6, 7, 4],
        ("size", "desc"): [1, 2, 3, 7, 6, 5, 8, 4],
        ("modified", "asc"): [2, 1, 3, 8, 5, 6, 7, 4],
        ("modified", "desc"): [1, 2, 3, 7, 6, 5, 8, 4],
    }[sort, order]
    pages = browse_fixture.walk(sort=sort, order=order, limit=2)
    assert [uuid.UUID(e["id"]).int for p in pages for e in p["entries"]] == expected
    for previous, current in pairwise(pages):
        backward = browse_fixture.page(
            sort=sort, order=order, limit=2, cursor=current["previousCursor"],
        )
        assert backward["entries"] == previous["entries"]


@pytest.mark.parametrize("sort", ["name", "size", "modified"])
def test_untruncated_name_keys_identical_displays_and_distinct_raw_names(
    browse_fixture: Any, sort: str,
) -> None:
    first = browse_fixture.entry(b"first", size=5, mtime_ns=5, id=uuid.UUID(int=2))
    second = browse_fixture.entry(b"second", size=5, mtime_ns=5, id=uuid.UUID(int=1))
    for entry, tail in ((first, b"a"), (second, b"b")):
        CatalogEntry.objects.filter(pk=entry.pk).update(
            display_name="Identical", name_key=b"x" * 2047 + tail,
        )
    pages = browse_fixture.walk(sort=sort, limit=1)
    assert [p["entries"][0]["id"] for p in pages] == [str(first.pk), str(second.pk)]
    assert pages[0]["entries"][0]["displayName"] == pages[1]["entries"][0]["displayName"]


def test_source_display_projection_preserves_logical_names(browse_fixture: Any) -> None:
    folder = browse_fixture.entry(b"source-folder", kind="directory", logical_name="Private folder")
    child = browse_fixture.entry(b"source.txt", parent=folder, logical_name="Private document")
    page = browse_fixture.page(
        parent_id=folder.pk, filters=parse_filters({"v": 1, "prefix": "source"}),
    )
    assert page["entries"][0]["displayName"] == "source.txt"
    details = browse_fixture.details(child.pk)
    assert details["displayName"] == "source.txt"
    assert details["ancestors"][-1]["displayName"] == "source-folder"
    child.refresh_from_db()
    folder.refresh_from_db()
    assert child.logical_name == "Private document"
    assert folder.logical_name == "Private folder"


def test_availability_filters_observation_while_dto_reports_stale_ancestry(
    browse_fixture: Any,
) -> None:
    parent = browse_fixture.entry(b"parent", kind="directory", state="missing")
    live = browse_fixture.entry(b"live", parent=parent)
    missing = browse_fixture.entry(b"missing", parent=parent, state="missing")
    present_page = browse_fixture.page(
        parent_id=parent.pk, filters=parse_filters({"v": 1, "availability": ["present"]}),
    )
    assert [e["id"] for e in present_page["entries"]] == [str(live.pk)]
    assert present_page["entries"][0]["sourceState"] == "inaccessible"
    missing_page = browse_fixture.page(
        parent_id=parent.pk, filters=parse_filters({"v": 1, "availability": ["missing"]}),
    )
    assert [e["id"] for e in missing_page["entries"]] == [str(missing.pk)]
    assert missing_page["entries"][0]["sourceState"] == "missing"
