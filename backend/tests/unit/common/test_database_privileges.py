from __future__ import annotations

import importlib
import inspect
import uuid
from unittest.mock import patch

from aegis_apps.identity import admin_services as identity_admin_services
from aegis_apps.operations import heartbeats, services
from aegis_apps.operations.models import Operation, WorkerHeartbeat
from aegis_apps.roots import services as root_services
from django.core.management import get_commands
from django.test import override_settings

EXPECTED_TABLE_COLUMNS = {
    "django_migrations": ("id", "app", "name", "applied"),
    "django_admin_log": (
        "id",
        "action_time",
        "object_id",
        "object_repr",
        "action_flag",
        "change_message",
        "content_type_id",
        "user_id",
    ),
    "auth_permission": ("id", "name", "content_type_id", "codename"),
    "auth_group_permissions": ("id", "group_id", "permission_id"),
    "auth_group": ("id", "name"),
    "django_content_type": ("id", "app_label", "model"),
    "django_session": ("session_key", "session_data", "expire_date"),
    "identity_user_groups": ("id", "user_id", "group_id"),
    "identity_user_user_permissions": ("id", "user_id", "permission_id"),
    "identity_user": (
        "password",
        "last_login",
        "is_superuser",
        "username",
        "first_name",
        "last_name",
        "email",
        "is_staff",
        "is_active",
        "date_joined",
        "id",
        "authorization_epoch",
    ),
    "identity_groupidentity": ("id", "group_id"),
    "identity_loginthrottlebucket": (
        "id",
        "kind",
        "key_digest",
        "window_started_at",
        "failures",
        "blocked_until",
    ),
    "audit_auditevent": (
        "id",
        "occurred_at",
        "event_type",
        "outcome",
        "request_id",
        "root_id",
        "object_id",
        "metadata",
        "actor_id",
    ),
    "roots_root": (
        "id",
        "slot_id",
        "display_name",
        "mode",
        "active",
        "authorization_epoch",
        "capabilities",
        "created_at",
        "updated_at",
    ),
    "roots_rootgrant": (
        "id",
        "permissions",
        "created_at",
        "updated_at",
        "group_id",
        "root_id",
        "user_id",
    ),
    "operations_operation": (
        "id",
        "request_id",
        "kind",
        "request_hash",
        "intent",
        "authorization_snapshot",
        "created_at",
        "actor_id",
        "idempotency_namespace",
    ),
    "operations_job": (
        "id",
        "target_role",
        "kind",
        "payload",
        "priority",
        "state",
        "available_at",
        "attempts",
        "attempt_token",
        "max_attempts",
        "lease_owner",
        "lease_expires_at",
        "safe_error_code",
        "safe_error_detail",
        "result",
        "created_at",
        "updated_at",
        "operation_id",
        "execution_started_at",
    ),
    "operations_workerheartbeat": (
        "id",
        "role",
        "worker_id",
        "release_id",
        "schema_identity",
        "manifest_identity",
        "last_seen_at",
        "current_job_id",
        "status",
        "metrics",
    ),
}


def _privileges() -> object:
    return importlib.import_module("aegis_apps.common.database_privileges")


def test_privilege_source_explicitly_assigns_every_table_column_and_sequence() -> None:
    privilege_source = _privileges()

    assert privilege_source.MANAGED_TABLE_COLUMNS == EXPECTED_TABLE_COLUMNS
    assert privilege_source.ROLE_TABLE_PRIVILEGES["aegis_web"][
        "django_migrations"
    ] == ("SELECT",)
    assert privilege_source.ROLE_TABLE_PRIVILEGES["aegis_web"][
        "identity_groupidentity"
    ] == ("SELECT", "INSERT")
    assert privilege_source.ROLE_TABLE_PRIVILEGES["aegis_web"]["auth_group"] == (
        "SELECT",
        "INSERT",
        "UPDATE",
    )
    assert privilege_source.ROLE_COLUMN_PRIVILEGES["aegis_web"][
        "operations_operation"
    ] == {"id": ("UPDATE",)}
    assert privilege_source.ROLE_SEQUENCE_PRIVILEGES["aegis_web"] == {
        "auth_group_id_seq": ("USAGE", "SELECT"),
        "auth_group_permissions_id_seq": ("USAGE", "SELECT"),
        "django_admin_log_id_seq": ("USAGE", "SELECT"),
        "identity_user_groups_id_seq": ("USAGE", "SELECT"),
        "identity_user_user_permissions_id_seq": ("USAGE", "SELECT"),
    }


def test_worker_grants_are_column_fenced_without_authorization_graph_access() -> None:
    privilege_source = _privileges()
    expected_job_updates = {
        column: ("UPDATE",)
        for column in (
            "state",
            "available_at",
            "attempt_token",
            "execution_started_at",
            "lease_owner",
            "lease_expires_at",
            "attempts",
            "safe_error_code",
            "safe_error_detail",
            "result",
            "updated_at",
        )
    }
    expected_root_reads = {
        column: ("SELECT",)
        for column in ("id", "mode", "active", "authorization_epoch")
    }

    for role in ("aegis_operations", "aegis_indexer", "aegis_media"):
        assert privilege_source.ROLE_TABLE_PRIVILEGES[role][
            "django_migrations"
        ] == ("SELECT",)
        assert privilege_source.ROLE_COLUMN_PRIVILEGES[role][
            "operations_job"
        ] == expected_job_updates
        assert privilege_source.ROLE_COLUMN_PRIVILEGES[role][
            "roots_root"
        ] == expected_root_reads
        for table in (
            "identity_user",
            "identity_user_groups",
            "auth_group",
            "auth_group_permissions",
            "roots_rootgrant",
        ):
            assert table not in privilege_source.ROLE_TABLE_PRIVILEGES[role]


def test_function_execution_allowlist_is_exact_and_public_is_never_a_grantee() -> None:
    privilege_source = _privileges()

    assert privilege_source.ROLE_FUNCTION_PRIVILEGES == {
        "aegis_web": (),
        "aegis_operations": (
            "aegis_publish_operations_heartbeat",
            "aegis_validate_operation_authorization",
        ),
        "aegis_indexer": (
            "aegis_publish_indexer_heartbeat",
            "aegis_validate_operation_authorization",
        ),
        "aegis_media": (
            "aegis_publish_media_heartbeat",
            "aegis_validate_operation_authorization",
        ),
    }
    assert "PUBLIC" not in privilege_source.ROLE_FUNCTION_PRIVILEGES
    assert set(privilege_source.HEARTBEAT_FUNCTION_SQL) == {
        "aegis_publish_operations_heartbeat",
        "aegis_publish_indexer_heartbeat",
        "aegis_publish_media_heartbeat",
    }
    for function_name, sql in privilege_source.HEARTBEAT_FUNCTION_SQL.items():
        role = function_name.removeprefix("aegis_publish_").removesuffix(
            "_heartbeat"
        )
        normalized = " ".join(sql.split()).lower()
        assert "security definer" in normalized
        assert "set search_path = ''" in normalized
        assert f"'{role}'" in normalized
        assert "current_user" not in normalized
        assert "session_user" not in normalized
        assert "role text" not in normalized
        for argument in (
            "p_worker_id",
            "p_release_id",
            "p_schema_identity",
            "p_manifest_identity",
            "p_status",
            "p_metrics",
            "p_retention_seconds",
            "p_slot_limit",
        ):
            assert f"{argument} is null" in normalized


def test_authorization_boundary_bounds_json_and_does_not_mask_database_faults() -> None:
    privilege_source = _privileges()
    normalized = " ".join(privilege_source.AUTHORIZATION_FUNCTION_SQL.split()).lower()

    assert "octet_length(operation_intent::text) > 16384" in normalized
    assert "octet_length(operation_snapshot::text) > 16384" in normalized
    assert "when others" not in normalized
    assert "invalid_text_representation or numeric_value_out_of_range" in normalized


def test_privilege_sync_requires_an_explicit_database_transaction() -> None:
    privilege_source = _privileges()
    source = inspect.getsource(privilege_source.synchronize_database_privileges)

    assert "connection.in_atomic_block" in source
    assert "requires an atomic transaction" in source


def test_privilege_sync_requires_authenticated_migrator_not_set_role() -> None:
    privilege_source = _privileges()
    source = inspect.getsource(privilege_source._verify_role_boundaries)

    assert "SELECT session_user, current_user" in source
    assert "(\"aegis_migrator\", \"aegis_migrator\")" in source


def test_boundary_function_metadata_is_verified_exactly() -> None:
    privilege_source = _privileges()
    source = inspect.getsource(privilege_source._verify_function_boundaries)

    assert "prosecdef" in source
    assert "proowner" in source
    assert "proconfig" in source
    assert "oidvectortypes" in source
    assert "aegis_migrator" in source


def test_public_function_execution_is_checked_from_catalog_acl() -> None:
    privilege_source = _privileges()
    source = inspect.getsource(privilege_source._verify_effective_grants)

    assert "aclexplode" in source
    assert "acl.grantee = 0" in source
    assert "has_function_privilege('public'" not in source.lower()


def test_runtime_roles_must_have_no_membership_edges() -> None:
    privilege_source = _privileges()

    assert privilege_source.RUNTIME_DATABASE_ROLES == (
        "aegis_web",
        "aegis_operations",
        "aegis_indexer",
        "aegis_media",
    )
    assert frozenset() == privilege_source.ALLOWED_ROLE_MEMBERSHIPS


def test_group_identity_creation_does_not_request_update_lock_privilege() -> None:
    assert "GroupIdentity.objects.select_for_update()" not in inspect.getsource(
        identity_admin_services
    )
    assert "GroupIdentity.objects.select_for_update()" not in inspect.getsource(
        root_services
    )


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_role_runtime_heartbeat_dispatches_only_to_fixed_database_function() -> None:
    expected = WorkerHeartbeat(id=uuid.uuid4(), role="operations")
    with (
        patch.object(
            heartbeats,
            "current_database_login",
            return_value="aegis_operations",
            create=True,
        ),
        patch.object(
            heartbeats,
            "_publish_heartbeat_via_database",
            return_value=expected,
            create=True,
        ) as publish_boundary,
    ):
        result = heartbeats.publish_heartbeat(
            role="operations",
            worker_id=str(uuid.uuid4()),
            release_id="release-11",
            schema_identity="sha256:" + "a" * 64,
            manifest_identity="b" * 64,
        )

    assert result is expected
    publish_boundary.assert_called_once()
    assert publish_boundary.call_args.kwargs["role"] == "operations"


@override_settings(AEGIS_PROCESS_ROLE="operations")
def test_role_runtime_authorization_uses_opaque_database_boundary() -> None:
    operation = Operation(id=uuid.uuid4())
    with (
        patch.object(
            services,
            "current_database_login",
            return_value="aegis_operations",
            create=True,
        ),
        patch.object(
            services,
            "_validate_authorization_snapshot_via_database",
            return_value=True,
            create=True,
        ) as validation_boundary,
    ):
        assert services.validate_authorization_snapshot(operation) is True

    validation_boundary.assert_called_once_with(operation.id)


def test_deployment_commands_are_discoverable() -> None:
    commands = get_commands()

    assert commands["sync_db_privileges"] == "aegis_apps.common"
    assert commands["deploy_database"] == "aegis_apps.common"
