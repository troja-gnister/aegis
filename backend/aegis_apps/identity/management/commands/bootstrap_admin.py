from __future__ import annotations

import secrets
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

from aegis.config import ConfigurationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError

from aegis_apps.identity.services import bootstrap_admin
from aegis_apps.identity.validators import read_private_secret


class Command(BaseCommand):
    help = "Create the initial Aegis administrator from a private password file."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", required=True)
        parser.add_argument("--password-file", required=True)

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            password = read_private_secret(Path(options["password_file"]))
            result = bootstrap_admin(
                username=options["username"],
                email=options["email"],
                password=password,
                request_id=secrets.token_hex(16),
            )
        except (ConfigurationError, DatabaseError, ValueError):
            raise CommandError("administrator bootstrap failed") from None

        created = "true" if result.created else "false"
        self.stdout.write(f"user_id={result.user_id} created={created}")
