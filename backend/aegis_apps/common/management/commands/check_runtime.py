from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from aegis_apps.common.runtime_checks import RUNTIME_ROLES, check_runtime_boundary


class Command(BaseCommand):
    help = "Verify this container's fixed runtime security boundary."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--role", required=True, choices=RUNTIME_ROLES)
        parser.add_argument("--require-http", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        try:
            check_runtime_boundary(
                options.get("role"),
                require_http=options.get("require_http") is True,
            )
        except Exception:
            raise CommandError("runtime boundary check failed") from None
        self.stdout.write("runtime boundary ok")
