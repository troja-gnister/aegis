"""Code-side schema identity is cached per process; the database check stays live."""
from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aegis_apps.operations import selectors
from aegis_apps.operations.selectors import SchemaCompatibilityError, current_schema_identity
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


class _RollBack(Exception):
    pass


@pytest.fixture(autouse=True)
def _fresh_code_schema() -> Iterator[None]:
    selectors._code_schema.cache_clear()
    yield
    selectors._code_schema.cache_clear()


def _legacy_identity() -> str:
    """The identity exactly as computed before caching (per-call executor graph)."""
    executor = MigrationExecutor(connection)
    targets = sorted(executor.loader.graph.leaf_nodes())
    assert executor.migration_plan(targets) == []
    return selectors._schema_identity(targets)


def test_identity_is_unchanged_from_the_per_call_migration_graph() -> None:
    assert current_schema_identity() == _legacy_identity()


def test_code_graph_is_built_once_but_every_call_reads_the_database_history() -> None:
    with (
        patch.object(selectors, "MigrationLoader", wraps=MigrationLoader) as loader,
        CaptureQueriesContext(connection) as captured,
    ):
        first = current_schema_identity()
        second = current_schema_identity()

    assert first == second
    assert loader.call_count == 1
    history = [query["sql"] for query in captured.captured_queries
               if "django_migrations" in query["sql"]]
    assert len(history) == 2
    assert len(captured.captured_queries) == 2


def test_an_unrecorded_code_migration_fails_closed_even_with_a_warm_cache() -> None:
    current_schema_identity()
    with pytest.raises(_RollBack), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM django_migrations WHERE app = 'operations' "
                "AND name = (SELECT min(name) FROM django_migrations WHERE app = 'operations')"
            )
            assert cursor.rowcount == 1
        with pytest.raises(SchemaCompatibilityError, match="migrations pending"):
            current_schema_identity()
        raise _RollBack

    assert current_schema_identity() == _legacy_identity()


def test_a_missing_migration_history_fails_closed() -> None:
    current_schema_identity()
    with pytest.raises(_RollBack), transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE django_migrations RENAME TO aegis_moved_migrations")
        with pytest.raises(SchemaCompatibilityError, match="migrations pending"):
            current_schema_identity()
        raise _RollBack


def test_squashed_migrations_use_the_per_call_database_graph() -> None:
    squashed = SimpleNamespace(replacements={("app", "0001_squashed"): object()})
    with (
        patch.object(selectors, "MigrationLoader", return_value=squashed),
        patch.object(selectors, "_graph_schema_identity", return_value="sha256:graph") as graph,
    ):
        assert current_schema_identity() == "sha256:graph"
        assert current_schema_identity() == "sha256:graph"
    assert graph.call_count == 2
