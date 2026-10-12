"""Name search (``contains``): a literal, case-insensitive substring of entry names."""

from __future__ import annotations

import random
import uuid
from importlib import import_module
from itertools import pairwise
from typing import Any

import psycopg
import pytest
from aegis_apps.catalog import queries
from aegis_apps.catalog.authorization import CatalogNotFound
from aegis_apps.catalog.cursors import CursorRestartRequired
from aegis_apps.catalog.filters import SearchTooShort, parse_filters
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.catalog.names import source_name
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant
from django.db import connection, transaction
from psycopg import sql

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _search(text: str, **filters: Any) -> Any:
    return parse_filters({"v": 1, "contains": text, **filters})


def _names(page: dict[str, Any]) -> list[str]:
    return [item["displayName"] for item in page["entries"]]


def _tree(browse_fixture: Any) -> dict[str, CatalogEntry]:
    entry = browse_fixture.entry
    trips = entry(b"Trips", kind="directory")
    summer = entry(b"Summer trip", kind="directory", parent=trips)
    return {
        "Trips": trips,
        "Summer trip": summer,
        "trip-map.pdf": entry(b"trip-map.pdf", size=5, mtime_ns=5),
        "IMG_TRIP_1.JPG": entry(b"IMG_TRIP_1.JPG", parent=summer, size=10, mtime_ns=1),
        "strip.png": entry(b"strip.png", parent=trips, size=None, mtime_ns=None),
        "notes.txt": entry(b"notes.txt", parent=summer, size=2, mtime_ns=2),
        "old trip.jpg": entry(b"old trip.jpg", parent=summer, state="missing", size=1),
    }


def test_search_finds_the_text_anywhere_in_names_across_the_root(browse_fixture: Any) -> None:
    tree = _tree(browse_fixture)
    page = browse_fixture.page(filters=_search("TRIP"), scope="root")
    # Folders first, then files by name; anywhere in the name, any case; no tombstone.
    assert _names(page) == ["Summer trip", "Trips", "IMG_TRIP_1.JPG", "strip.png", "trip-map.pdf"]
    located = {item["displayName"]: item["parentName"] for item in page["entries"]}
    assert located == {
        "Summer trip": "Trips",
        "Trips": "",
        "IMG_TRIP_1.JPG": "Summer trip",
        "strip.png": "Trips",
        "trip-map.pdf": "",
    }
    # The folder scope searches that folder's own children only.
    folder = browse_fixture.page(filters=_search("trip"), parent_id=tree["Summer trip"].pk)
    assert _names(folder) == ["IMG_TRIP_1.JPG"]
    assert _names(browse_fixture.page(filters=_search("trip"))) == ["Trips", "trip-map.pdf"]
    missing = browse_fixture.page(
        filters=_search("trip", availability=["missing"]),
        scope="root",
    )
    assert _names(missing) == ["old trip.jpg"]
    assert _names(browse_fixture.page(filters=_search("absent"), scope="root")) == []


@pytest.mark.parametrize(
    ("raw", "text", "decoy"),
    [
        (b"100%_done.txt", "00%_", b"100X-done.txt"),
        (b"a_b.txt", "a_b", b"acb.txt"),
        (b"a%b.txt", "a%b", b"a-long-b.txt"),
        # The display (and so the order key) escapes a backslash as two.
        (b"a\\b.txt", "a\\\\b", b"ab.txt"),
    ],
)
def test_search_text_is_literal(
    browse_fixture: Any,
    raw: bytes,
    text: str,
    decoy: bytes,
) -> None:
    expected = browse_fixture.entry(raw)
    browse_fixture.entry(decoy)
    for scope in ("root", "directory"):
        page = browse_fixture.page(filters=_search(text), scope=scope)
        assert [item["id"] for item in page["entries"]] == [str(expected.pk)]


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        ("\u00c9clair.txt".encode(), "E\u0301CL"),
        ("e\u0301clair.txt".encode(), "\u00c9CL"),
        ("Stra\u00dfe.txt".encode(), "STRASS"),
        (b"STRASSE.txt", "stra\u00df"),
        ("\u01c4ungla.txt".encode(), "\u01c6un"),
    ],
)
def test_case_and_unicode_normalization_match_the_prefix_filter(
    browse_fixture: Any,
    raw: bytes,
    text: str,
) -> None:
    expected = browse_fixture.entry(raw)
    browse_fixture.entry(b"other.txt")
    wanted = [str(expected.pk)]
    for scope in ("root", "directory"):
        found = browse_fixture.page(filters=_search(text), scope=scope)
        prefixed = browse_fixture.page(filters=parse_filters({"v": 1, "prefix": text}))
        assert [item["id"] for item in found["entries"]] == wanted
        assert [item["id"] for item in prefixed["entries"]] == wanted


def _seed(browse_fixture: Any) -> list[CatalogEntry]:
    """Folders and files of every kind, size, time and state; some names hold "trip"."""
    folders = [browse_fixture.anchor]
    for depth in range(3):
        folders.append(
            browse_fixture.entry(
                f"trip-level-{depth}".encode() if depth != 1 else b"level-1",
                kind="directory",
                parent=folders[-1],
                size=4096,
                mtime_ns=depth,
            )
        )
    entries = list(folders[1:])
    extensions = ("jpg", "JPEG", "png", "txt", "")
    for index in range(160):
        extension = extensions[index % 5]
        stem = ("Trip", "strip", "TRIPOD", "item", "road")[index % 5 if index % 7 else 0]
        name = f"{stem}-{index % 37:02d}-{index:03d}" + (f".{extension}" if extension else "")
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


def _expected(entries: list[CatalogEntry], sort: str, order: str) -> list[str]:
    def rank(entry: CatalogEntry) -> tuple[int, int]:
        value = entry.mtime_ns if sort == "modified" else entry.size
        kind = 0 if entry.kind == "directory" else 1
        return (kind, 0 if sort == "name" or value is not None else 1)

    def rest(entry: CatalogEntry) -> tuple[Any, ...]:
        value = entry.mtime_ns if sort == "modified" else entry.size
        head: tuple[Any, ...] = () if sort == "name" else (int(value or 0),)
        return (*head, bytes(entry.name_key), entry.id)

    groups: dict[tuple[int, int], list[CatalogEntry]] = {}
    for entry in entries:
        groups.setdefault(rank(entry), []).append(entry)
    return [
        str(entry.id)
        for key in sorted(groups)
        for entry in sorted(groups[key], key=rest, reverse=order == "desc")
    ]


# Which read decides a page: the capped trigram read, the ordered windows or, when both
# fall short, the read up to the sample's bound. A cap of one page still leaves a sample
# of more than a page, which the bound needs; windows of one page rarely decide.
STRATEGIES = {
    "capped": {},
    "windows": {"SEARCH_MATCH_FACTOR": 1},
    "bounded": {"SEARCH_MATCH_FACTOR": 1, "SEARCH_WINDOW_FACTOR": 0},
}
# The upper bound of a bounded read: a row comparison that includes the sampled key.
_BOUNDED = (") <= (", ") >= (")


@pytest.mark.parametrize("strategy", list(STRATEGIES))
@pytest.mark.parametrize("sort", ["name", "modified", "size"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize(
    "selection",
    [
        {},
        {"type": ["jpg"]},
        {"type": ["jpeg", "jpg", "__unknown__"]},
        {"availability": ["present", "missing"]},
        {"kind": ["file", "symlink"], "size": {"min": "1", "max": "4"}},
        {"modified": {"unknown": True}},
        {"prefix": "trip"},
    ],
)
def test_search_pages_every_match_once_in_full_key_order_both_ways(
    browse_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
    sort: str,
    order: str,
    selection: dict[str, Any],
) -> None:
    for name, value in STRATEGIES[strategy].items():
        monkeypatch.setattr(queries, name, value)
    entries = _seed(browse_fixture)
    filters = _search("trip", **selection)
    states = filters.availability or ("present", "unsupported", "inaccessible")
    wanted = [
        entry
        for entry in entries
        if "trip" in entry.display_name.casefold()
        and entry.source_state in states
        and (not filters.type or (entry.type_hint or "__unknown__") in filters.type)
        and (not filters.kind or entry.kind in filters.kind)
        and (filters.size is None or (entry.size is not None and 1 <= entry.size <= 4))
        and (filters.modified is None or entry.mtime_ns is None)
        and (not filters.prefix or entry.display_name.casefold().startswith(filters.prefix))
    ]
    assert wanted
    options = {"sort": sort, "order": order, "limit": 7, "filters": filters, "scope": "root"}
    pages = browse_fixture.walk(**options)
    found = [item["id"] for page in pages for item in page["entries"]]
    assert found == _expected(wanted, sort, order)
    for previous, current in pairwise(pages):
        back = browse_fixture.page(cursor=current["previousCursor"], **options)
        assert back["entries"] == previous["entries"]
    files = any(entry.kind != "directory" for entry in wanted)
    reads = browse_fixture.statements
    assert any('"_walked"' in sql for sql in reads) == (strategy != "capped" and files)
    if strategy == "capped":
        assert not any(marker in sql for sql in reads for marker in _BOUNDED)


@pytest.mark.parametrize("text", ["...", " - ", "%%%", "-_-", "a--", "\U0001f600" * 3])
def test_text_no_trigram_index_can_look_up_is_refused_in_both_scopes(
    browse_fixture: Any,
    text: str,
) -> None:
    browse_fixture.entry(f"a{text}b.txt".encode())
    for scope in ("root", "directory"):
        with pytest.raises(SearchTooShort):
            browse_fixture.page(filters=_search(text), scope=scope)


@pytest.mark.parametrize(
    ("text", "raw"),
    [
        ("a.b", b"xa.by"),
        ("--a", b"x--ay"),
        ("(1)", b"x(1)y"),
        ("\u00e4\u00f6\u00fc", "x\u00e4\u00f6\u00fcy".encode()),
    ],
)
def test_text_with_a_trigram_is_searched_however_short_its_runs(
    browse_fixture: Any,
    text: str,
    raw: bytes,
) -> None:
    expected = browse_fixture.entry(raw)
    browse_fixture.entry(b"other.txt")
    for scope in ("root", "directory"):
        page = browse_fixture.page(filters=_search(text), scope=scope)
        assert [item["id"] for item in page["entries"]] == [str(expected.pk)]


def test_the_trigram_rule_follows_the_database_character_type() -> None:
    letters = "\u00e4\u00f6\u00fc"

    def indexable(text: str, collation: str = "") -> bool:
        sql = queries._INDEXABLE_SQL.replace("%(text)s", f"(%(text)s{collation})")
        with connection.cursor() as cursor:
            cursor.execute(sql, {"text": text})
            return bool(cursor.fetchone()[0])

    def word(text: str) -> bool:
        """Whether pg_trgm reads *text* as word characters in this database."""
        with connection.cursor() as cursor:
            cursor.execute("SELECT aegis_extensions.show_trgm(%s) <> '{}'", [text])
            return bool(cursor.fetchone()[0])

    # A database made with a UTF-8 locale (as installs are) reads these letters as word
    # characters, and the rule reads them as pg_trgm does, whatever the database's locale.
    for text in (letters, "\u6771\u4eac\u99c5"):
        assert indexable(text) is word(text), text
    # Under a C character type it reads only ASCII, like pg_trgm.
    assert not indexable(letters, ' COLLATE "C"')
    assert indexable("abc", ' COLLATE "C"')
    for text, wanted in (
        ("a.b", True),
        ("--a", True),
        ("ab-", True),
        ("a--", False),
        ("-a-", True),
        ("---", False),
        ("abc", True),
        ("a b", True),
    ):
        assert indexable(text) is wanted, text


def test_the_migration_refuses_a_database_not_encoded_in_utf8() -> None:
    migration = import_module("aegis_apps.catalog.migrations.0006_name_search")
    settings = connection.settings_dict
    params = {
        "user": settings["USER"],
        "password": settings["PASSWORD"],
        "host": settings["HOST"],
        "port": settings["PORT"],
    }
    database = f"aegis_ascii_{uuid.uuid4().hex}"
    name = sql.Identifier(database)
    with psycopg.connect(dbname="postgres", autocommit=True, **params) as admin:
        admin.execute(
            sql.SQL(
                "CREATE DATABASE {} ENCODING 'SQL_ASCII' LC_COLLATE 'C' LC_CTYPE 'C' "
                "TEMPLATE template0"
            ).format(name)
        )
        try:
            with psycopg.connect(dbname=database, autocommit=True, **params) as other:
                with pytest.raises(
                    psycopg.errors.RaiseException,
                    match="name search needs a UTF8 database, not SQL_ASCII",
                ):
                    other.execute(migration.EXTENSIONS)
                # It refuses before making anything.
                made = other.execute(
                    "SELECT count(*) FROM pg_catalog.pg_namespace "
                    "WHERE nspname = 'aegis_extensions'"
                ).fetchone()
                assert made == (0,)
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(name))


def test_a_cursor_binds_its_search_text(browse_fixture: Any) -> None:
    _seed(browse_fixture)
    page = browse_fixture.page(filters=_search("trip"), scope="root", limit=3)
    assert page["nextCursor"]
    continued = browse_fixture.page(
        filters=_search("TRIP"),
        scope="root",
        limit=3,
        cursor=page["nextCursor"],
    )
    assert continued["entries"]
    for changed in (
        {"filters": _search("strip"), "scope": "root"},
        {"filters": parse_filters({"v": 1}), "scope": "root"},
        {"filters": parse_filters({"v": 1, "prefix": "trip"}), "scope": "root"},
        {"filters": _search("trip"), "scope": "directory"},
        {"filters": _search("trip", type=["jpg"]), "scope": "root"},
    ):
        with pytest.raises(CursorRestartRequired):
            browse_fixture.page(limit=3, cursor=page["nextCursor"], **changed)


def test_search_follows_grants_like_the_listing(browse_fixture: Any) -> None:
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
        raw_name=b"trip.jpg",
        display_name="trip.jpg",
        name_key=b"trip.jpg",
        type_hint="jpg",
        kind="file",
    )
    assert "trip.jpg" not in _names(browse_fixture.page(filters=_search("trip"), scope="root"))
    browse_fixture.user = User.objects.create_user(username=f"ungranted-{uuid.uuid4().hex}")
    with pytest.raises(CatalogNotFound):
        browse_fixture.page(filters=_search("trip"), scope="root")
    RootGrant.objects.create(root=browse_fixture.root, user=browse_fixture.user, permissions=1)
    assert _names(browse_fixture.page(filters=_search("trip"), scope="root"))


@pytest.mark.parametrize("strategy", ["capped", "bounded"])
def test_search_lists_only_entries_the_listing_lists(
    browse_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
) -> None:
    for name, value in STRATEGIES[strategy].items():
        monkeypatch.setattr(queries, name, value)
    listed = [browse_fixture.entry(f"trip-{number}.txt".encode()) for number in range(6)]
    # Other names first, so a window of a page's entries holds no match.
    for number in range(10):
        browse_fixture.entry(f"album-{number}.txt".encode())
    # An entry without a logical parent is never listed, so it is never found either.
    hidden = browse_fixture.entry(b"trip-2b.txt")
    CatalogEntry.objects.filter(pk=hidden.pk).update(logical_parent=None)
    wanted = [str(entry.pk) for entry in listed]
    pages = browse_fixture.walk(filters=_search("trip"), scope="root", limit=2)
    assert [item["id"] for page in pages for item in page["entries"]] == wanted
    prefixed = browse_fixture.page(filters=parse_filters({"v": 1, "prefix": "trip"}), scope="root")
    assert [item["id"] for item in prefixed["entries"]] == wanted
    if strategy == "bounded":
        assert any(marker in sql for sql in browse_fixture.statements for marker in _BOUNDED)


def _descendants(
    node: dict[str, Any],
    ancestors: tuple[dict[str, Any], ...] = (),
) -> list[tuple[dict[str, Any], tuple[dict[str, Any], ...]]]:
    nodes = [(node, ancestors)]
    for child in node.get("Plans", []):
        nodes.extend(_descendants(child, (*ancestors, node)))
    return nodes


def _plan(statement: str, scan: str | None) -> dict[str, Any]:
    """The executed plan of *statement* under the settings the search applies to it."""
    with connection.cursor() as cursor:
        # As autovacuum keeps it: the trigram indexes' pending entries merged and their
        # statistics current (they were built empty, with the test database).
        cursor.execute("VACUUM (ANALYZE) catalog_catalogentry")
    with transaction.atomic(), connection.cursor() as cursor:
        for setting in queries._SCANS[scan] if scan else ():
            cursor.execute(f"SET LOCAL {setting} = off")
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement)
        plan: dict[str, Any] = cursor.fetchone()[0][0]["Plan"]
        return plan


def _seed_scale(browse_fixture: Any, ties: int = 200) -> None:
    """Many non-matching names besides the photos: every third photo is a holiday one."""
    folders = [
        browse_fixture.entry(f"Album {index}".encode(), kind="directory") for index in range(3)
    ]
    for folder in folders:
        browse_fixture.seed_ties(ties, parent=folder)
    for number in range(150):
        stem = "holiday" if number % 3 == 0 else "photo"
        browse_fixture.entry(
            f"{number:03d}-{stem}.jpg".encode(),
            parent=folders[number % 3],
            size=number % 4,
            mtime_ns=number % 5,
        )


_FILES = 'NOT ("catalog_catalogentry"."kind" = \'directory\')'


def _search_statements(browse_fixture: Any, marker: str) -> list[str]:
    return [sql for sql in browse_fixture.statements if marker in sql]


def _heap_rows(nodes: list[dict[str, Any]]) -> float:
    """Every row the plan's heap scans read, kept or removed."""
    return float(
        sum(
            (
                node["Actual Rows"]
                + node.get("Rows Removed by Filter", 0)
                + node.get("Rows Removed by Index Recheck", 0)
            )
            * node["Actual Loops"]
            for node in nodes
            if node.get("Relation Name") == "catalog_catalogentry"
        )
    )


def _bitmap_only(statement: str) -> list[dict[str, Any]]:
    nodes = [node for node, _ in _descendants(_plan(statement, "bitmap"))]
    relations = [node for node in nodes if node.get("Relation Name") == "catalog_catalogentry"]
    assert relations and all(node["Node Type"] == "Bitmap Heap Scan" for node in relations)
    return nodes


@pytest.mark.parametrize("sort", ["name", "size", "modified"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("deep", [False, True])
def test_trigram_reads_use_bitmaps_and_the_last_one_reads_up_to_its_bound(
    browse_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    sort: str,
    order: str,
    deep: bool,
) -> None:
    # A cap of 6 matches and windows of 3 entries: holidays are too sparse for either,
    # so the page comes from the bounded read.
    monkeypatch.setattr(queries, "SEARCH_MATCH_FACTOR", 2)
    monkeypatch.setattr(queries, "SEARCH_WINDOW_FACTOR", 0)
    # Enough other names that reading the whole root costs more than the trigram index.
    _seed_scale(browse_fixture, ties=2000)
    options = {"sort": sort, "order": order, "limit": 2, "scope": "root"}
    first = browse_fixture.page(filters=_search("holiday"), **options)
    if deep:
        browse_fixture.statements.clear()
        browse_fixture.page(filters=_search("holiday"), cursor=first["nextCursor"], **options)
    capped = _search_statements(browse_fixture, '"_matches"')
    bounded = [
        sql for sql in browse_fixture.statements if any(marker in sql for marker in _BOUNDED)
    ]
    # Folders lie behind a cursor among the files; none holds the term.
    assert len(capped) == (1 if deep else 2) and len(bounded) == 1
    for statement in capped:
        kind = "files" if _FILES in statement else "directories"
        nodes = _bitmap_only(statement)
        assert {node["Index Name"] for node in nodes if "Index Name" in node} == {
            f"catalog_search_{kind}"
        }
        # The capped read keeps at most cap + 1 matches; on page one, it reads no more.
        heap = [node for node in nodes if node.get("Relation Name") == "catalog_catalogentry"]
        assert sum(node["Actual Rows"] * node["Actual Loops"] for node in heap) <= 2 * 3 + 1
        assert deep or _heap_rows(nodes) <= 2 * 3 + 1
    _bitmap_only(bounded[0])


def test_several_types_read_per_type_windows_and_otherwise_bitmaps(
    browse_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(queries, "SEARCH_MATCH_FACTOR", 1)
    _seed_scale(browse_fixture)
    filters = _search("holiday", type=["jpg", "png"], size={"min": "1"})
    browse_fixture.page(filters=filters, sort="size", limit=2, scope="root")
    windows = _search_statements(browse_fixture, '"_walked"')
    assert len(windows) == 1 and " LATERAL " in windows[0]
    walks = [
        node
        for node, _ in _descendants(_plan(windows[0], "index"))
        if node.get("Relation Name") == "catalog_catalogentry"
    ]
    # One planned window per rank reads each selected type's keyset in turn.
    assert walks and all(
        node.get("Index Name") == "catalog_root_type_size" and node["Actual Loops"] == 2
        for node in walks
    ), walks
    for statement in _search_statements(browse_fixture, "AS matched"):
        _bitmap_only(statement)


def _seed_photos(browse_fixture: Any) -> None:
    """Dense matches in every order and rank: two of every three names hold "photo"."""
    folders = [
        browse_fixture.entry(f"Album {index}".encode(), kind="directory") for index in range(3)
    ]
    for number in range(150):
        stem = "holiday" if number % 3 == 0 else "photo"
        browse_fixture.entry(
            f"{number:03d}-{stem}.jpg".encode(),
            parent=folders[number % 3],
            size=None if number % 7 == 0 else number % 4,
            mtime_ns=None if number % 8 == 0 else number % 5,
        )


@pytest.mark.parametrize("sort", ["name", "size", "modified"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_windows_read_the_root_indexes_in_order_and_stop_when_the_page_is_full(
    browse_fixture: Any,
    monkeypatch: pytest.MonkeyPatch,
    sort: str,
    order: str,
) -> None:
    monkeypatch.setattr(queries, "SEARCH_MATCH_FACTOR", 1)
    _seed_photos(browse_fixture)
    options = {"sort": sort, "order": order, "limit": 2, "scope": "root"}
    first = browse_fixture.page(filters=_search("photo"), **options)
    browse_fixture.statements.clear()
    browse_fixture.page(filters=_search("photo"), cursor=first["nextCursor"], **options)
    windows = _search_statements(browse_fixture, '"_walked"')
    assert len(windows) == 1
    nodes = _descendants(_plan(windows[0], "index"))
    assert not any(node["Node Type"] == "Seq Scan" for node, _ in nodes)
    walks = [
        (node, above)
        for node, above in nodes
        if node.get("Relation Name") == "catalog_catalogentry"
    ]
    assert walks and all(node.get("Index Name") == f"catalog_root_{sort}" for node, _ in walks), [
        (node["Node Type"], node.get("Index Name")) for node, _ in walks
    ]
    window = queries.SEARCH_WINDOW_FACTOR * 3
    for node, above in walks:
        # Under its window's LIMIT, numbered in index order: no sort between the walk and
        # its window, so a dense window stops after its page.
        assert any(parent["Node Type"] == "Limit" for parent in above)
        numbered = next(parent for parent in reversed(above) if parent["Node Type"] == "WindowAgg")
        assert not any(
            child["Node Type"] in ("Sort", "Incremental Sort")
            for child, _ in _descendants(numbered)
        )
        assert node["Actual Rows"] * node["Actual Loops"] < window
    # The cursor boundary is an index condition (a seek), never a filter over earlier rows.
    assert any(
        ">" in node.get("Index Cond", "") or "<" in node.get("Index Cond", "") for node, _ in walks
    )


def test_search_page_keeps_the_statement_budget(browse_fixture: Any) -> None:
    _seed_scale(browse_fixture)
    assert len(browse_fixture.page(filters=_search("holiday"), scope="root", limit=2)["entries"])
    sql = [" ".join(value.upper().split()) for value in browse_fixture.statements]
    data = [value for value in sql if value.lstrip("(").startswith(("SELECT", "WITH"))]
    # A root-scope page's statements, plus the trigram check and the capped reads.
    assert len(data) <= 10, data
    assert not any("FOR UPDATE" in value for value in sql)


def _library(browse_fixture: Any) -> dict[str, list[CatalogEntry]]:
    """A mixed library: photos, then (by name) tracks, pictures and documents before them.

    Tracks are larger and newer than photos, so in name ascending, size descending and
    modified descending order a long run of tracks comes before the first photo. Photos
    are stored in a shuffled order, so the trigram index finds them in no order.
    """
    folders = [
        browse_fixture.entry(f"Album {index}".encode(), kind="directory") for index in range(4)
    ]
    numbers = list(range(2000))
    random.Random(7).shuffle(numbers)
    groups: dict[str, list[tuple[bytes, int, int]]] = {
        "photos": [
            (f"IMG_{n:04d}.{'jpeg' if n % 5 == 0 else 'jpg'}".encode(), 10**6 + n, 10**18 + n)
            for n in numbers
        ],
        "tracks": [
            (f"{n % 20 + 1:02d} - Song {n:04d}.flac".encode(), 5 * 10**7 + n, 2 * 10**18 + n)
            for n in range(1500)
        ],
        "pictures": [(f"Cafe {n:04d}.png".encode(), 1000 + n, n) for n in range(500)],
        "documents": [(f"doc {n:04d}.txt".encode(), 2000 + n, n) for n in range(1000)],
    }
    created: dict[str, list[CatalogEntry]] = {}
    for group, rows in groups.items():
        entries = []
        for index, (raw, size, mtime) in enumerate(rows):
            name, folder = source_name(raw), folders[index % len(folders)]
            entries.append(
                CatalogEntry(
                    root=browse_fixture.root,
                    source_parent=folder,
                    logical_parent=folder,
                    source_parent_revision=folder.source_revision,
                    raw_name=name.raw,
                    display_name=name.display,
                    name_key=name.order_key,
                    type_hint=name.type_hint,
                    kind="file",
                    size=size,
                    mtime_ns=mtime,
                )
            )
        created[group] = CatalogEntry.objects.bulk_create(entries)
    return created


def _ordered(entries: list[CatalogEntry], sort: str, order: str) -> list[str]:
    def key(entry: CatalogEntry) -> tuple[Any, ...]:
        value = {"name": None, "size": entry.size, "modified": entry.mtime_ns}[sort]
        return (*(() if value is None else (int(value),)), bytes(entry.name_key), entry.id)

    return [str(entry.pk) for entry in sorted(entries, key=key, reverse=order == "desc")]


def _pages(browse_fixture: Any, count: int, **options: Any) -> list[str]:
    found, cursor = [], None
    for _ in range(count):
        browse_fixture.statements.clear()
        page = browse_fixture.page(cursor=cursor, **options)
        found += [item["id"] for item in page["entries"]]
        cursor = page["nextCursor"]
    return found


GAP_ORDERS = [("name", "asc"), ("size", "desc"), ("modified", "desc")]
ALL_ORDERS = [(sort, order) for sort in ("name", "size", "modified") for order in ("asc", "desc")]


@pytest.mark.parametrize(("sort", "order"), ALL_ORDERS)
def test_a_common_term_with_two_types_is_decided_by_bounded_per_type_windows(
    browse_fixture: Any,
    sort: str,
    order: str,
) -> None:
    library = _library(browse_fixture)
    options = {
        "sort": sort,
        "order": order,
        "limit": 5,
        "scope": "root",
        "filters": _search("img", type=["jpg", "jpeg"]),
    }
    # 2,000 matches overflow the capped read (600); five pages reach a deep cursor.
    assert _pages(browse_fixture, 5, **options) == _ordered(library["photos"], sort, order)[:25]
    reads = browse_fixture.statements
    assert not any(marker in sql for sql in reads for marker in _BOUNDED)
    windows = _search_statements(browse_fixture, '"_walked"')
    assert len(windows) == 1
    walks = [
        node
        for node, _ in _descendants(_plan(windows[0], "index"))
        if node.get("Relation Name") == "catalog_catalogentry"
    ]
    assert walks and all(node.get("Index Name") == f"catalog_root_type_{sort}" for node in walks)
    # Each type's window stops once it holds a page: dense matches read about a page each.
    assert sum(node["Actual Rows"] * node["Actual Loops"] for node in walks) <= 2 * 2 * 6


@pytest.mark.parametrize(("sort", "order"), ALL_ORDERS)
def test_types_without_matches_never_make_the_page_read_other_types(
    browse_fixture: Any,
    sort: str,
    order: str,
) -> None:
    library = _library(browse_fixture)
    selected = ["jpg", "jpeg", "png", "flac"]
    options = {
        "sort": sort,
        "order": order,
        "limit": 5,
        "scope": "root",
        "filters": _search("img", type=selected),
    }
    expected = _ordered(library["photos"], sort, order)
    assert _pages(browse_fixture, 1, **options) == expected[:5]
    bounded = [
        sql for sql in browse_fixture.statements if any(marker in sql for marker in _BOUNDED)
    ]
    assert _pages(browse_fixture, 2, **options) == expected[:10]
    # Unless photos come first, the first page's windows of pictures or tracks see no
    # match, and the bounded read decides.
    assert bool(bounded) == ((sort, order) != ("name", "desc"))
    for statement in bounded:
        # Pictures and tracks hold no match; documents are not selected.
        assert _heap_rows(_bitmap_only(statement)) <= 2000 + 500 + 1500


@pytest.mark.parametrize(("sort", "order"), GAP_ORDERS)
def test_a_long_run_of_other_names_is_skipped_by_the_type_window(
    browse_fixture: Any,
    sort: str,
    order: str,
) -> None:
    library = _library(browse_fixture)
    jpg = [entry for entry in library["photos"] if entry.type_hint == "jpg"]
    options = {
        "sort": sort,
        "order": order,
        "limit": 5,
        "scope": "root",
        "filters": _search("img", type=["jpg"]),
    }
    assert _pages(browse_fixture, 1, **options) == _ordered(jpg, sort, order)[:5]
    assert not any(marker in sql for sql in browse_fixture.statements for marker in _BOUNDED)


@pytest.mark.parametrize(("sort", "order"), GAP_ORDERS)
def test_a_long_run_of_other_names_without_types_reads_up_to_the_bound(
    browse_fixture: Any,
    sort: str,
    order: str,
) -> None:
    library = _library(browse_fixture)
    options = {"sort": sort, "order": order, "limit": 5, "scope": "root", "filters": _search("img")}
    # The 1,500 tracks fill the windows (120 entries each): the bounded read decides.
    assert _pages(browse_fixture, 1, **options) == _ordered(library["photos"], sort, order)[:5]
    bounded = [
        sql for sql in browse_fixture.statements if any(marker in sql for marker in _BOUNDED)
    ]
    assert len(bounded) == 1
    _bitmap_only(bounded[0])
