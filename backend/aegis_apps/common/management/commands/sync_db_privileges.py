from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from aegis_apps.common.database_privileges import (
    PrivilegeSynchronizationError,
    synchronize_database_privileges,
)


class Command(BaseCommand):
    help = "Synchronize the fixed runtime database privilege allowlist."

    def handle(self, *args: Any, **options: Any) -> None:
        del args, options
        try:
            with transaction.atomic(durable=True):
                synchronize_database_privileges()
        except PrivilegeSynchronizationError:
            raise CommandError("database privilege synchronization failed") from None
        self.stdout.write("database privileges synchronized")
