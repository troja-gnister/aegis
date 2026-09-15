import json
from datetime import UTC, datetime
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.indexing.models import RootIndexState
from django.db import connection, transaction

from tests.support.database_roles import RoleDatabase

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_ordinary_page_has_eight_data_statement_budget_and_shared_locks(
    browse_fixture: Any,
) -> None:
    browse_fixture.seed_ties(5)
    assert len(browse_fixture.page(limit=2)["entries"]) == 2
    sql = [" ".join(value.upper().split()) for value in browse_fixture.statements]
    data = [value for value in sql if value.startswith(("SELECT", "WITH"))]
    assert len(data) <= 8, data
    assert len(sql) <= 16
    locks = [value for value in sql if "FOR SHARE" in value]
    assert "ROOTS_ROOT" in locks[0]
    assert "IDENTITY_USER" in locks[1]
    assert not any("FOR UPDATE" in value for value in sql)
    assert any("LIMIT 3" in value for value in sql)
    assert not any("RAW_NAME" in value or "INODE" in value for value in data)


def test_details_are_bounded_private_and_cycle_safe(browse_fixture: Any) -> None:
    parent = browse_fixture.anchor
    chain = []
    for index in range(90):
        parent = browse_fixture.entry(str(index).encode(), parent=parent, kind="directory")
        chain.append(parent)
    details = browse_fixture.details(parent.pk)
    assert len(details["ancestors"]) == 64
    assert details["ancestorsTruncated"] is True
    assert details["parentId"] == str(chain[-2].pk)
    assert {e["displayName"] for e in details["ancestors"]} == {str(i) for i in range(25, 89)}
    raw = json.dumps(details)
    assert all(name not in raw for name in ("raw_name", "rawName", "inode", "/srv/", "device"))
    CatalogEntry.objects.filter(pk=chain[0].pk).update(logical_parent=chain[20])
    assert browse_fixture.details(parent.pk)["ancestorsTruncated"] is True


def test_actual_web_login_can_read_without_catalog_writes(
    browse_fixture: Any, role_database: RoleDatabase,
) -> None:
    entry = browse_fixture.entry(b"actual-web", device=9876, inode=1234)
    with role_database.as_django_role("aegis_web"):
        assert browse_fixture.page()["entries"][0]["id"] == str(entry.pk)
        assert browse_fixture.details(entry.pk)["id"] == str(entry.pk)
        with connection.cursor() as cursor:
            cursor.execute("SELECT session_user, has_table_privilege(current_user, "
                           "'catalog_catalogentry', 'UPDATE')")
            assert cursor.fetchone() == ("aegis_web", False)


def test_all_six_order_indexes_exist(browse_fixture: Any) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT indexname, indexdef FROM pg_indexes "
                       "WHERE tablename='catalog_catalogentry' "
                       "AND indexname LIKE 'catalog_browse_%'")
        indexes = cursor.fetchall()
    assert len(indexes) == 6
    for _, definition in indexes:
        assert "root_id, logical_parent_id" in definition
        assert "source_state" in definition


@pytest.mark.parametrize("sort", ["name", "size", "modified"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_order_indexes_support_real_query_plans(browse_fixture: Any, sort: str, order: str) -> None:
    browse_fixture.seed_ties(251)
    browse_fixture.page(sort=sort, order=order, limit=2)
    statement = next(sql for sql in browse_fixture.statements if sql.startswith("WITH candidates"))
    with transaction.atomic(), connection.cursor() as cursor:
        # A small fixture may prefer a seqscan; this proves the exact composite
        # index can supply candidate order. Physical scale is a later gate.
        cursor.execute("ANALYZE catalog_catalogentry")
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + statement)
        document = cursor.fetchone()[0]
    nodes = []
    pending = [document[0]["Plan"]]
    while pending:
        node = pending.pop()
        nodes.append(node)
        pending.extend(node.get("Plans", []))
    candidate_limit = next(node for node in nodes if node.get("Subplan Name") == "CTE candidates")
    assert candidate_limit["Actual Rows"] == 3
    assert any(node.get("Index Name") == f"catalog_browse_{sort}_{order}" for node in nodes), (
        statement, candidate_limit,
    )
    assert not any(node["Node Type"] == "Seq Scan" for node in nodes)


def test_mixed_visibility_is_merged_as_bounded_sql_candidates(browse_fixture: Any) -> None:
    from aegis_apps.catalog.filters import parse_filters

    browse_fixture.seed_ties(10)
    browse_fixture.entry(b"missing", state="missing")
    page = browse_fixture.page(
        filters=parse_filters({"v": 1, "availability": ["present", "missing"]}), limit=2,
    )
    assert len(page["entries"]) == 2
    statement = next(sql for sql in browse_fixture.statements if sql.startswith("WITH candidates"))
    assert "UNION ALL" in statement
    assert statement.count("LIMIT 3") == 3
    assert "OFFSET" not in statement


def test_wire_fields_and_index_status_preserve_integer_precision(browse_fixture: Any) -> None:
    instant = datetime(2026, 9, 15, tzinfo=UTC)
    RootIndexState.objects.create(
        root=browse_fixture.root, binding_epoch=1, policy_epoch=1, due_at=instant,
        status="scanning", next_generation=42, observed_entries=2**53 + 1,
        completed_directories=123, degraded_directories=2, last_completed_at=instant,
    )
    entry = browse_fixture.entry(b"wire", size=2**64 - 1, mtime_ns=2**63 - 1,
                                 catalog_version=2**53 + 1)
    page = browse_fixture.page()
    assert set(page) == {"entries", "nextCursor", "previousCursor", "directoryId",
                         "directoryVersion", "contractVersion", "indexStatus"}
    summary = page["entries"][0]
    assert summary == {
        "id": str(entry.pk), "rootId": str(browse_fixture.root.pk), "displayName": "wire",
        "kind": "file", "typeHint": None, "size": "18446744073709551615",
        "modifiedNs": "9223372036854775807", "sourceState": "present",
        "version": "9007199254740993",
    }
    status = page["indexStatus"]
    assert status["state"] == "scanning"
    assert status["generation"] == "41"
    assert status["observedEntries"] == "9007199254740993"
    assert status["completedDirectories"] == "123"
    assert status["degradedDirectories"] == "2"
    assert status["lastCompletedAt"] == instant.isoformat()
    assert set(browse_fixture.details(entry.pk)) == set(summary) | {
        "parentId", "ancestors", "ancestorsTruncated",
    }


@pytest.mark.parametrize("options", [
    {"limit": 0}, {"limit": 251}, {"limit": True}, {"sort": "id; DROP TABLE roots_root"},
    {"order": "DESC NULLS FIRST"},
])
def test_query_options_fail_without_executing_user_sql(
    browse_fixture: Any, options: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="invalid_catalog_query"):
        browse_fixture.page(**options)


def test_maximum_page_and_actual_web_mixed_cursor_read(
    browse_fixture: Any, role_database: RoleDatabase,
) -> None:
    from aegis_apps.catalog.filters import parse_filters

    browse_fixture.seed_ties(251)
    browse_fixture.entry(b"missing", state="missing")
    with role_database.as_django_role("aegis_web"):
        page = browse_fixture.page(limit=250)
        assert len(page["entries"]) == 250
        for sort in ("name", "size", "modified"):
            for order in ("asc", "desc"):
                options = dict(sort=sort, order=order, limit=2, filters=parse_filters(
                    {"v": 1, "availability": ["present", "missing"]},
                ))
                first = browse_fixture.page(**options)
                second = browse_fixture.page(cursor=first["nextCursor"], **options)
                back = browse_fixture.page(cursor=second["previousCursor"], **options)
                assert back["entries"] == first["entries"]


def test_ancestor_outside_label_budget_still_controls_availability(browse_fixture: Any) -> None:
    parent = browse_fixture.anchor
    chain = []
    for index in range(70):
        parent = browse_fixture.entry(str(index).encode(), parent=parent, kind="directory")
        chain.append(parent)
    CatalogEntry.objects.filter(pk=chain[0].pk).update(source_state="missing")
    details = browse_fixture.details(parent.pk)
    assert details["sourceState"] == "inaccessible"
    assert details["ancestorsTruncated"] is True
    assert [a["id"] for a in details["ancestors"]] == [str(e.pk) for e in chain[5:69]]
