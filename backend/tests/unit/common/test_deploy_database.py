from unittest.mock import patch

import pytest
from aegis_apps.common.database_privileges import PrivilegeSynchronizationError
from aegis_apps.common.management.commands.deploy_database import Command
from django.core.management.base import CommandError


def test_deploy_refuses_unsafe_database_boundary_before_running_migrations() -> None:
    command = Command()
    with (
        patch(
            "aegis_apps.common.management.commands.deploy_database.current_database_login",
            return_value="aegis_migrator",
        ),
        patch(
            "aegis_apps.common.management.commands.deploy_database."
            "verify_database_deployment_prerequisites",
            side_effect=PrivilegeSynchronizationError("private boundary detail"),
            create=True,
        ),
        patch(
            "aegis_apps.common.management.commands.deploy_database.call_command",
            side_effect=AssertionError("migrations must not run"),
        ) as migrate,
        pytest.raises(CommandError, match=r"^database deployment failed$") as caught,
    ):
        command.handle()

    assert "private" not in str(caught.value)
    migrate.assert_not_called()
