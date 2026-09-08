import logging

from django.conf import settings
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from aegis_apps.operations import selectors as operation_selectors
from aegis_apps.roots.manifest import ManifestError

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


def worker_readiness() -> tuple[bool, dict[str, str]]:
    roles = settings.AEGIS_REQUIRED_WORKER_ROLES
    try:
        manifest_identity = operation_selectors.current_manifest_identity()
    except ManifestError:
        logger.exception(
            "Worker mount manifest readiness check failed",
            extra={
                "event": "health.readiness.workers",
                "error_code": "MOUNT_MANIFEST_INVALID",
            },
        )
        states = {role: "stale" for role in roles}
        return False, states
    states = operation_selectors.worker_role_states(
        roles=roles,
        release_id=settings.AEGIS_RELEASE_ID,
        schema_identity=operation_selectors.current_schema_identity(),
        manifest_identity=manifest_identity,
        now=timezone.now(),
        freshness_seconds=settings.AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS,
    )
    return all(state == "healthy" for state in states.values()), states


def readiness() -> tuple[bool, dict[str, str]]:
    database_ok, database_message = database_status()
    if not database_ok:
        return False, {"database": database_message}
    try:
        workers_ok, worker_states = worker_readiness()
    except operation_selectors.SchemaCompatibilityError:
        return False, {"database": "migrations pending"}
    except Exception:
        logger.exception(
            "Worker readiness check failed",
            extra={
                "event": "health.readiness.workers",
                "error_code": "WORKER_STATUS_UNAVAILABLE",
            },
        )
        return False, {"database": "database unavailable"}
    return workers_ok, {"database": database_message, **worker_states}
