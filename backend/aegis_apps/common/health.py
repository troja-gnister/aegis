import logging

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

logger = logging.getLogger(__name__)


def database_status() -> tuple[bool, str]:
    try:
        connection.ensure_connection()
        executor = MigrationExecutor(connection)
        targets = executor.loader.graph.leaf_nodes()
        if executor.migration_plan(targets):
            return False, "migrations pending"
    except Exception:
        logger.exception(
            "Database readiness check failed",
            extra={
                "event": "health.readiness.database",
                "error_code": "DATABASE_UNAVAILABLE",
            },
        )
        return False, "database unavailable"
    return True, "ok"


def readiness() -> tuple[bool, dict[str, str]]:
    database_ok, database_message = database_status()
    return database_ok, {"database": database_message}
