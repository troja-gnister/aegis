from __future__ import annotations

import secrets
from argparse import SUPPRESS, Action, ArgumentParser, Namespace
from pathlib import Path
from typing import Any

from aegis.config import ConfigurationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError

from aegis_apps.identity.services import bootstrap_admin
from aegis_apps.identity.validators import read_private_secret

_PASSWORD_FILE_OPTION = "--password-file"
_REJECTED_PASSWORD_OPTIONS = tuple(
    _PASSWORD_FILE_OPTION[:length] for length in range(3, len(_PASSWORD_FILE_OPTION))
)


class _RejectPasswordOption(Action):
    def __call__(
        self,
        parser: ArgumentParser,
        namespace: Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        del namespace, values
        if option_string is None:
            raise AssertionError("rejecting action requires an option string")
        parser.error(f"unrecognized argument: {option_string}")


class Command(BaseCommand):
    help = "Create the initial Aegis administrator from a private password file."

    def create_parser(
        self, prog_name: str, subcommand: str, **kwargs: Any
    ) -> CommandParser:
        kwargs["allow_abbrev"] = False
        return super().create_parser(prog_name, subcommand, **kwargs)

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--username", required=True)
        parser.add_argument("--email", required=True)
        parser.add_argument(
            *_REJECTED_PASSWORD_OPTIONS,
            action=_RejectPasswordOption,
            help=SUPPRESS,
        )
        parser.add_argument(_PASSWORD_FILE_OPTION, required=True)

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
