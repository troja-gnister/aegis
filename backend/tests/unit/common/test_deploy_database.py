from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from aegis_apps.common.database_privileges import PrivilegeSynchronizationError
from aegis_apps.common.management.commands.deploy_database import Command
from aegis_apps.indexing.config import ScanPolicy
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


def test_deploy_installs_validated_index_binding_in_durable_phase(settings: Any) -> None:
    command = Command()
    manifest = SimpleNamespace(digest="a" * 64, slots={})
    settings.AEGIS_SCAN_POLICY = ScanPolicy(3600, 120, 500, 2)
    with (
        patch(
            "aegis_apps.common.management.commands.deploy_database.current_database_login",
            return_value="aegis_migrator",
        ),
        patch(
            "aegis_apps.common.management.commands.deploy_database."
            "verify_database_deployment_prerequisites"
        ),
        patch(
            "aegis_apps.common.management.commands.deploy_database.configured_manifest",
            return_value=manifest,
        ),
        patch("aegis_apps.common.management.commands.deploy_database.call_command"),
        patch(
            "aegis_apps.common.management.commands.deploy_database.MigrationExecutor"
        ) as executor,
        patch("aegis_apps.common.management.commands.deploy_database.transaction.atomic"),
        patch("aegis_apps.common.management.commands.deploy_database.synchronize_database_privileges"),
        patch(
            "aegis_apps.common.management.commands.deploy_database.install_index_binding"
        ) as install,
        patch(
            "aegis_apps.common.management.commands.deploy_database.current_schema_identity",
            return_value="schema-v1",
        ),
        patch("aegis_apps.common.management.commands.deploy_database.connection") as database,
    ):
        executor.return_value.loader.graph.leaf_nodes.return_value = []
        executor.return_value.migration_plan.return_value = []
        command.handle()

    install.assert_called_once_with(manifest, settings.AEGIS_SCAN_POLICY)
    database.cursor.assert_called_once()
