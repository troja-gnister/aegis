from __future__ import annotations

import json
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.migrations.executor import MigrationExecutor

from aegis_apps.common.database_privileges import (
    PrivilegeSynchronizationError,
    current_database_login,
    synchronize_database_privileges,
)
from aegis_apps.operations.config import validated_release_identity
from aegis_apps.operations.selectors import current_schema_identity


class Command(BaseCommand):
    help = "Apply migrations and the fixed database deployment boundary."

    def handle(self, *args: Any, **options: Any) -> None:
        del args, options
        try:
            if current_database_login() != "aegis_migrator":
                raise PrivilegeSynchronizationError(
                    "database deployment requires the migrator login"
                )
            release_id = validated_release_identity(
                settings.AEGIS_RELEASE_ID,
                production=settings.AEGIS_ENVIRONMENT == "production",
            )
            call_command("migrate", interactive=False, verbosity=1)
            executor = MigrationExecutor(connection)
            targets = executor.loader.graph.leaf_nodes()
            if executor.migration_plan(targets):
                raise PrivilegeSynchronizationError("database migrations remain pending")
            with transaction.atomic(durable=True):
                synchronize_database_privileges()
                schema_identity = current_schema_identity()
                metadata = json.dumps(
                    {
                        "application": "aegis",
                        "releaseId": release_id,
                        "schemaIdentity": schema_identity,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                with connection.cursor() as cursor:
                    cursor.execute("COMMENT ON SCHEMA public IS %s", [metadata])
        except (PrivilegeSynchronizationError, ValueError):
            raise CommandError("database deployment failed") from None
        self.stdout.write(
            json.dumps(
                {
                    "status": "deployed",
                    "releaseId": release_id,
                    "schemaIdentity": schema_identity,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
