from __future__ import annotations

import inspect
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from aegis_apps.common import database_privileges
from aegis_apps.identity import admin_services as identity_admin_services
from aegis_apps.operations import heartbeats, services
from aegis_apps.operations.models import Operation, WorkerHeartbeat
from aegis_apps.roots import services as root_services
from django.core.management import get_commands
from django.test import override_settings


@pytest.mark.parametrize("broken", ["missing", "duplicate", "unknown"])
def test_scan_sql_installation_rejects_invalid_fixed_markers(tmp_path: Path, broken: str) -> None:
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    periodic = "    /* AEGIS_START_PERIODIC_RUN */"
    manual = "        /* AEGIS_START_MANUAL_RUN */"
    template = periodic + "\n" + manual
    if broken == "missing":
        template = periodic
    elif broken == "duplicate":
        template += "\n" + periodic
    else:
        template += "\n/* AEGIS_START_CALLER_SELECTED_RUN */"
    (sql_dir / "schedule.sql").write_text(template)
    (sql_dir / "start_run.sql").write_text("SELECT 1;\n")
    (sql_dir / "lease.sql").write_text("SELECT 2;\n")
    with (
        patch("aegis_apps.common.database_privileges.files", return_value=tmp_path),
        pytest.raises(
            database_privileges.PrivilegeSynchronizationError,
        ),
    ):
        database_privileges._scan_function_sql()


EXPECTED_TABLE_COLUMNS = {
    "catalog_catalogentry": (
        "id",
        "raw_name",
        "display_name",
        "name_key",
        "logical_name",
        "kind",
        "type_hint",
        "source_state",
        "size",
        "mtime_ns",
        "ctime_ns",
        "device",
        "inode",
        "source_revision",
        "catalog_version",
        "children_version",
        "observation_epoch",
        "seen_generation",
        "seen_attempt",
        "observed_at",
        "logical_parent_id",
        "root_id",
        "source_parent_id",
        "source_parent_revision",
    ),
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
    "indexing_directorywork": (
        "id",
        "parent_revision",
        "state",
        "attempt",
        "lease_owner",
        "lease_expires_at",
        "available_at",
        "last_batch_sequence",
        "observed_count",
        "eof_identity",
        "error_code",
        "updated_at",
        "directory_id",
        "run_id",
        "last_batch_hash",
    ),
    "indexing_indexdeployment": (
        "id",
        "epoch",
        "manifest_identity",
        "slot_ids",
        "interval_seconds",
        "idle_timeout_seconds",
        "batch_records",
        "readers",
        "updated_at",
    ),
    "indexing_rootindexstate": (
        "root_id",
        "binding_epoch",
        "policy_epoch",
        "reconciliation_epoch",
        "next_generation",
        "due_at",
        "rescan_requested",
        "status",
        "observed_entries",
        "completed_directories",
        "degraded_directories",
        "updated_at",
        "last_completed_at",
        "active_run_id",
    ),
    "indexing_scanrequest": (
        "id",
        "client_request_id",
        "created_at",
        "actor_id",
        "root_id",
        "run_id",
    ),
    "indexing_scanrun": (
        "id",
        "binding_epoch",
        "policy_epoch",
        "root_epoch",
        "manifest_identity",
        "generation",
        "start_epoch",
        "state",
        "started_at",
        "settled_at",
        "root_id",
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
    "media_mediaasset": (
        "entry_id",
        "kind",
        "device",
        "inode",
        "size",
        "mtime_ns",
        "fingerprint",
        "probe_state",
        "thumbnail_state",
        "poster_state",
        "lane",
        "attempts",
        "attempt_token",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
        "processor_version",
        "safe_error_code",
        "width",
        "height",
        "orientation",
        "captured_at",
        "captured_at_source",
        "camera_make",
        "camera_model",
        "duration_ms",
        "video_codec",
        "audio_codec",
        "container",
        "has_location",
        "sort_at",
        "sort_source",
        "created_at",
        "updated_at",
        "root_id",
    ),
    "media_mediaderivative": (
        "id",
        "kind",
        "fingerprint",
        "params_digest",
        "key",
        "extension",
        "bytes",
        "width",
        "height",
        "state",
        "evictable",
        "created_at",
        "last_access_at",
        "asset_id",
    ),
}


def test_privilege_source_explicitly_assigns_every_table_column_and_sequence() -> None:
    privilege_source = database_privileges

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
    privilege_source = database_privileges
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
        root_reads = privilege_source.ROLE_COLUMN_PRIVILEGES[role][
            "roots_root"
        ]
        if role in ("aegis_indexer", "aegis_media"):
            assert root_reads == {**expected_root_reads, "slot_id": ("SELECT",)}
        else:
            assert root_reads == expected_root_reads
        for table in (
            "identity_user",
            "identity_user_groups",
            "auth_group",
            "auth_group_permissions",
            "roots_rootgrant",
        ):
            assert table not in privilege_source.ROLE_TABLE_PRIVILEGES[role]


def test_catalog_is_select_only_and_media_reads_only_its_fixed_columns() -> None:
    expected_media_columns = (
        "id",
        "root_id",
        "source_parent_id",
        "raw_name",
        "kind",
        "type_hint",
        "source_state",
        "size",
        "mtime_ns",
        "device",
        "inode",
    )
    assert expected_media_columns == database_privileges.MEDIA_CATALOG_READ_COLUMNS
    assert set(expected_media_columns) < set(
        database_privileges.MANAGED_TABLE_COLUMNS["catalog_catalogentry"]
    )
    for role in database_privileges.RUNTIME_DATABASE_ROLES:
        assert database_privileges.ROLE_TABLE_PRIVILEGES[role].get("catalog_catalogentry", ()) == (
            ("SELECT",) if role in ("aegis_web", "aegis_indexer") else ()
        )
        column_grants = database_privileges.ROLE_COLUMN_PRIVILEGES[role].get(
            "catalog_catalogentry"
        )
        if role == "aegis_media":
            assert column_grants == {
                column: ("SELECT",) for column in expected_media_columns
            }
        else:
            assert column_grants is None
    media_catalog = database_privileges.ROLE_COLUMN_PRIVILEGES["aegis_media"][
        "catalog_catalogentry"
    ]
    for hidden in (
        "display_name",
        "name_key",
        "logical_name",
        "logical_parent_id",
        "ctime_ns",
        "source_revision",
        "observed_at",
    ):
        assert hidden not in media_catalog


def test_media_tables_are_media_owned_and_web_readable_only() -> None:
    media_tables = ("media_mediaasset", "media_mediaderivative")
    expected = {
        "aegis_web": {
            "media_mediaasset": ("SELECT",),
            "media_mediaderivative": ("SELECT",),
        },
        "aegis_media": {
            "media_mediaasset": ("SELECT", "INSERT", "UPDATE"),
            "media_mediaderivative": ("SELECT", "INSERT", "UPDATE", "DELETE"),
        },
        "aegis_operations": {},
        "aegis_indexer": {},
    }
    for role in database_privileges.RUNTIME_DATABASE_ROLES:
        granted = {
            table: privileges
            for table, privileges in database_privileges.ROLE_TABLE_PRIVILEGES[role].items()
            if table.startswith("media_")
        }
        assert granted == expected[role]
        for table in media_tables:
            assert table not in database_privileges.ROLE_COLUMN_PRIVILEGES[role]
            assert "TRUNCATE" not in granted.get(table, ())
        assert not any(
            sequence.startswith("media_")
            for sequence in database_privileges.ROLE_SEQUENCE_PRIVILEGES[role]
        )
    assert set(media_tables) <= set(database_privileges.MANAGED_TABLE_COLUMNS)
    assert not any(
        sequence.startswith("media_") for sequence in database_privileges.MANAGED_SEQUENCES
    )


def test_media_grants_add_no_other_media_role_privileges() -> None:
    assert database_privileges.ROLE_TABLE_PRIVILEGES["aegis_media"] == {
        "django_migrations": ("SELECT",),
        "operations_operation": ("SELECT",),
        "operations_job": ("SELECT",),
        "operations_workerheartbeat": ("SELECT",),
        "audit_auditevent": ("INSERT",),
        "media_mediaasset": ("SELECT", "INSERT", "UPDATE"),
        "media_mediaderivative": ("SELECT", "INSERT", "UPDATE", "DELETE"),
    }
    assert set(database_privileges.ROLE_COLUMN_PRIVILEGES["aegis_media"]) == {
        "operations_job",
        "roots_root",
        "catalog_catalogentry",
    }
    assert database_privileges.ROLE_SEQUENCE_PRIVILEGES["aegis_media"] == {}


def test_indexing_state_is_select_only_for_web_and_indexer() -> None:
    shared = {
        "indexing_directorywork",
        "indexing_indexdeployment",
        "indexing_rootindexstate",
        "indexing_scanrun",
    }
    assert {
        table
        for table, privileges in database_privileges.ROLE_TABLE_PRIVILEGES[
            "aegis_indexer"
        ].items()
        if table.startswith("indexing_") and privileges == ("SELECT",)
    } == shared
    assert {
        table
        for table, privileges in database_privileges.ROLE_TABLE_PRIVILEGES[
            "aegis_web"
        ].items()
        if table.startswith("indexing_") and privileges == ("SELECT",)
    } == shared | {"indexing_scanrequest"}
    for role in ("aegis_operations", "aegis_media"):
        assert not any(
            table.startswith("indexing_")
            for table in database_privileges.ROLE_TABLE_PRIVILEGES[role]
        )


def test_function_execution_allowlist_is_exact_and_public_is_never_a_grantee() -> None:
    privilege_source = database_privileges

    assert privilege_source.ROLE_FUNCTION_PRIVILEGES == {
        "aegis_web": ("aegis_request_root_scan", "aegis_touch_media_derivative"),
        "aegis_operations": (
            "aegis_publish_operations_heartbeat",
            "aegis_validate_operation_authorization",
        ),
        "aegis_indexer": (
            "aegis_publish_indexer_heartbeat",
            "aegis_validate_operation_authorization",
            "aegis_schedule_root_scan",
            "aegis_claim_scan_directory",
            "aegis_renew_scan_directory",
            "aegis_record_scan_batch",
            "aegis_seal_scan_directory",
            "aegis_finalize_scan_directory",
            "aegis_fail_scan_directory",
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


def test_media_touch_boundary_is_web_only_and_bounded() -> None:
    privilege_source = database_privileges
    normalized = " ".join(privilege_source.MEDIA_TOUCH_FUNCTION_SQL.split()).lower()

    assert privilege_source.MANAGED_FUNCTION_SIGNATURES["aegis_touch_media_derivative"] == (
        "public.aegis_touch_media_derivative(text)"
    )
    assert (
        privilege_source.MANAGED_FUNCTION_IDENTITY_ARGUMENTS["aegis_touch_media_derivative"]
        == "text"
    )
    assert [
        role
        for role, functions in privilege_source.ROLE_FUNCTION_PRIVILEGES.items()
        if "aegis_touch_media_derivative" in functions
    ] == ["aegis_web"]
    assert "security definer" in normalized
    assert "set search_path = ''" in normalized
    assert "returns void" in normalized
    assert "p_key is null or p_key !~ '^[0-9a-f]{64}$'" in normalized
    assert (
        "update public.media_mediaderivative as derivative "
        "set last_access_at = pg_catalog.now() "
        "where derivative.id = ( select candidate.id "
        "from public.media_mediaderivative as candidate "
        "where candidate.key = p_key "
        "and candidate.evictable "
        "and candidate.state = 'ready' "
        "and candidate.last_access_at < pg_catalog.now() - "
        "pg_catalog.make_interval(secs => 3600) "
        "for update skip locked );"
    ) in normalized
    assert privilege_source.MEDIA_TOUCH_INTERVAL_SECONDS == 3600
    for forbidden in (
        "session_user",
        "current_user",
        "delete",
        "insert",
        "execute ",
        "nowait",
        "lock table",
    ):
        assert forbidden not in normalized
    assert normalized.count("update public.") == 1
    assert normalized.count("for update") == 1
    install = inspect.getsource(privilege_source._install_boundary_functions)
    assert "MEDIA_TOUCH_FUNCTION_SQL" in install
    assert "aegis_touch_media_derivative" not in privilege_source.ROLE_TABLE_PRIVILEGES[
        "aegis_web"
    ]
    assert privilege_source.ROLE_TABLE_PRIVILEGES["aegis_web"]["media_mediaderivative"] == (
        "SELECT",
    )


def test_authorization_boundary_bounds_json_and_does_not_mask_database_faults() -> None:
    privilege_source = database_privileges
    normalized = " ".join(privilege_source.AUTHORIZATION_FUNCTION_SQL.split()).lower()

    assert "octet_length(operation_intent::text) > 16384" in normalized
    assert "octet_length(operation_snapshot::text) > 16384" in normalized
    assert "when others" not in normalized
    assert "invalid_text_representation or numeric_value_out_of_range" in normalized


def test_privilege_sync_requires_an_explicit_database_transaction() -> None:
    privilege_source = database_privileges
    source = inspect.getsource(privilege_source.synchronize_database_privileges)

    assert "connection.in_atomic_block" in source
    assert "requires an atomic transaction" in source


def test_privilege_sync_requires_authenticated_migrator_not_set_role() -> None:
    privilege_source = database_privileges
    source = inspect.getsource(privilege_source._verify_role_boundaries)

    assert "SELECT session_user, current_user" in source
    assert "(\"aegis_migrator\", \"aegis_migrator\")" in source


def test_boundary_function_metadata_is_verified_exactly() -> None:
    privilege_source = database_privileges
    source = inspect.getsource(privilege_source._verify_function_boundaries)

    assert "prosecdef" in source
    assert "proowner" in source
    assert "proconfig" in source
    assert "oidvectortypes" in source
    assert "aegis_migrator" in source


def test_public_function_execution_is_checked_from_catalog_acl() -> None:
    privilege_source = database_privileges
    source = inspect.getsource(privilege_source._verify_effective_grants)

    assert "aclexplode" in source
    assert "acl.grantee = 0" in source
    assert "has_function_privilege('public'" not in source.lower()


def test_runtime_roles_must_have_no_membership_edges() -> None:
    privilege_source = database_privileges

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


@override_settings(AEGIS_ENVIRONMENT="production", AEGIS_PROCESS_ROLE="operations")
def test_miswired_worker_heartbeat_never_falls_back_to_owner_orm() -> None:
    with (
        patch.object(
            heartbeats,
            "current_database_login",
            return_value="aegis_migrator",
            create=True,
        ),
        patch.object(heartbeats, "_publish_heartbeat", create=True) as owner_publish,
        pytest.raises(ValueError, match="database role boundary"),
    ):
        heartbeats.publish_heartbeat(
            role="operations",
            worker_id=str(uuid.uuid4()),
            release_id="release-11",
            schema_identity="sha256:" + "a" * 64,
            manifest_identity="b" * 64,
        )

    owner_publish.assert_not_called()


@override_settings(AEGIS_ENVIRONMENT="production", AEGIS_PROCESS_ROLE="operations")
@pytest.mark.django_db
def test_miswired_worker_authorization_never_falls_back_to_owner_orm() -> None:
    operation = Operation(id=uuid.uuid4())
    with (
        patch.object(
            services,
            "current_database_login",
            return_value="aegis_migrator",
            create=True,
        ),
        patch.object(Operation.objects, "filter") as owner_query,
        pytest.raises(ValueError, match="database role boundary"),
    ):
        services.validate_authorization_snapshot(operation)

    owner_query.assert_not_called()


def test_deployment_commands_are_discoverable() -> None:
    commands = get_commands()

    assert commands["sync_db_privileges"] == "aegis_apps.common"
    assert commands["deploy_database"] == "aegis_apps.common"
    assert commands["check_runtime"] == "aegis_apps.common"
