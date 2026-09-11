from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from django.db import connection

RUNTIME_DATABASE_ROLES: Final = (
    "aegis_web",
    "aegis_operations",
    "aegis_indexer",
    "aegis_media",
)
WORKER_DATABASE_ROLE_MAP: Final = {
    "aegis_operations": "operations",
    "aegis_indexer": "indexer",
    "aegis_media": "media",
}
RUNTIME_PROCESS_DATABASE_ROLES: Final = {
    "web": "aegis_web",
    "operations": "aegis_operations",
    "indexer": "aegis_indexer",
    "media": "aegis_media",
}
ALLOWED_ROLE_MEMBERSHIPS: Final[frozenset[tuple[str, str]]] = frozenset()

MANAGED_TABLE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
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

MANAGED_SEQUENCES: Final[tuple[str, ...]] = (
    "auth_group_id_seq",
    "auth_group_permissions_id_seq",
    "auth_permission_id_seq",
    "django_admin_log_id_seq",
    "django_content_type_id_seq",
    "django_migrations_id_seq",
    "identity_user_groups_id_seq",
    "identity_user_user_permissions_id_seq",
)

ROLE_TABLE_PRIVILEGES: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "aegis_web": {
        "django_migrations": ("SELECT",),
        "django_admin_log": ("SELECT", "INSERT"),
        "auth_permission": ("SELECT",),
        "auth_group_permissions": ("SELECT", "INSERT", "DELETE"),
        "auth_group": ("SELECT", "INSERT", "UPDATE"),
        "django_content_type": ("SELECT",),
        "django_session": ("SELECT", "INSERT", "UPDATE", "DELETE"),
        "identity_user_groups": ("SELECT", "INSERT", "DELETE"),
        "identity_user_user_permissions": ("SELECT", "INSERT", "DELETE"),
        "identity_user": ("SELECT", "INSERT", "UPDATE"),
        "identity_groupidentity": ("SELECT", "INSERT"),
        "identity_loginthrottlebucket": ("SELECT", "INSERT", "UPDATE", "DELETE"),
        "audit_auditevent": ("SELECT", "INSERT"),
        "roots_root": ("SELECT", "INSERT", "UPDATE"),
        "roots_rootgrant": ("SELECT", "INSERT", "UPDATE", "DELETE"),
        "operations_operation": ("SELECT", "INSERT"),
        "operations_job": ("SELECT", "INSERT"),
        "operations_workerheartbeat": ("SELECT",),
    },
    "aegis_operations": {
        "django_migrations": ("SELECT",),
        "operations_operation": ("SELECT",),
        "operations_job": ("SELECT",),
        "operations_workerheartbeat": ("SELECT",),
        "audit_auditevent": ("INSERT",),
    },
    "aegis_indexer": {
        "django_migrations": ("SELECT",),
        "operations_operation": ("SELECT",),
        "operations_job": ("SELECT",),
        "operations_workerheartbeat": ("SELECT",),
        "audit_auditevent": ("INSERT",),
    },
    "aegis_media": {
        "django_migrations": ("SELECT",),
        "operations_operation": ("SELECT",),
        "operations_job": ("SELECT",),
        "operations_workerheartbeat": ("SELECT",),
        "audit_auditevent": ("INSERT",),
    },
}

_WORKER_JOB_UPDATES: Final = {
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
_WORKER_ROOT_READS: Final = {
    column: ("SELECT",)
    for column in ("id", "mode", "active", "authorization_epoch")
}
ROLE_COLUMN_PRIVILEGES: Final[
    dict[str, dict[str, dict[str, tuple[str, ...]]]]
] = {
    "aegis_web": {
        "operations_operation": {"id": ("UPDATE",)},
    },
    **{
        database_role: {
            "operations_job": dict(_WORKER_JOB_UPDATES),
            "roots_root": dict(_WORKER_ROOT_READS),
        }
        for database_role in WORKER_DATABASE_ROLE_MAP
    },
}

ROLE_SEQUENCE_PRIVILEGES: Final[dict[str, dict[str, tuple[str, ...]]]] = {
    "aegis_web": {
        "auth_group_id_seq": ("USAGE", "SELECT"),
        "auth_group_permissions_id_seq": ("USAGE", "SELECT"),
        "django_admin_log_id_seq": ("USAGE", "SELECT"),
        "identity_user_groups_id_seq": ("USAGE", "SELECT"),
        "identity_user_user_permissions_id_seq": ("USAGE", "SELECT"),
    },
    "aegis_operations": {},
    "aegis_indexer": {},
    "aegis_media": {},
}

ROLE_FUNCTION_PRIVILEGES: Final[dict[str, tuple[str, ...]]] = {
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

HEARTBEAT_FUNCTION_SIGNATURE: Final = (
    "(text,text,text,text,text,jsonb,uuid,integer,integer)"
)
AUTHORIZATION_FUNCTION_SIGNATURE: Final = "(uuid)"


def _heartbeat_function_sql(
    *,
    function_name: str,
    worker_role: str,
    allocation_lock_key: int,
) -> str:
    return f"""
CREATE OR REPLACE FUNCTION public.{function_name}(
    p_worker_id text,
    p_release_id text,
    p_schema_identity text,
    p_manifest_identity text,
    p_status text,
    p_metrics jsonb,
    p_current_job_id uuid,
    p_retention_seconds integer,
    p_slot_limit integer
)
RETURNS uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $aegis_function$
DECLARE
    database_now timestamptz := pg_catalog.clock_timestamp();
    heartbeat_id uuid;
    occupied_slots integer;
BEGIN
    IF p_worker_id IS NULL
       OR p_release_id IS NULL
       OR p_schema_identity IS NULL
       OR p_manifest_identity IS NULL
       OR p_status IS NULL
       OR p_metrics IS NULL
       OR p_retention_seconds IS NULL
       OR p_slot_limit IS NULL THEN
        RAISE EXCEPTION 'invalid worker heartbeat'
            USING ERRCODE = '22023';
    END IF;

    IF p_worker_id !~ '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
       OR p_worker_id IS DISTINCT FROM (p_worker_id::uuid)::text
       OR p_release_id !~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{{0,95}}$'
       OR p_schema_identity !~ '^[A-Za-z0-9][A-Za-z0-9._:+-]{{0,95}}$'
       OR (
            p_manifest_identity <> 'unconfigured:v1'
            AND p_manifest_identity !~ '^[0-9a-f]{{64}}$'
       )
       OR p_status NOT IN ('idle', 'running', 'stopping')
       OR pg_catalog.jsonb_typeof(p_metrics) <> 'object'
       OR pg_catalog.octet_length(p_metrics::text) > 1024
       OR p_retention_seconds < 1
       OR p_retention_seconds > 604800
       OR p_slot_limit < 1
       OR p_slot_limit > 1024
       OR EXISTS (
            SELECT 1
              FROM pg_catalog.jsonb_object_keys(p_metrics) AS metric(key)
             WHERE metric.key NOT IN (
                'queueAgeSeconds',
                'scanProgress',
                'diskPressure',
                'diskCapacityBytes'
             )
       )
       OR (
            p_metrics ? 'queueAgeSeconds'
            AND (
                pg_catalog.jsonb_typeof(p_metrics -> 'queueAgeSeconds') <> 'number'
                OR (p_metrics ->> 'queueAgeSeconds')::numeric < 0
            )
       )
       OR (
            p_metrics ? 'scanProgress'
            AND (
                pg_catalog.jsonb_typeof(p_metrics -> 'scanProgress') <> 'number'
                OR (p_metrics ->> 'scanProgress')::numeric < 0
                OR (p_metrics ->> 'scanProgress')::numeric > 1
            )
       )
       OR (
            p_metrics ? 'diskPressure'
            AND (
                pg_catalog.jsonb_typeof(p_metrics -> 'diskPressure') <> 'number'
                OR (p_metrics ->> 'diskPressure')::numeric < 0
                OR (p_metrics ->> 'diskPressure')::numeric > 1
            )
       )
       OR (
            p_metrics ? 'diskCapacityBytes'
            AND (
                pg_catalog.jsonb_typeof(p_metrics -> 'diskCapacityBytes') <> 'number'
                OR (p_metrics ->> 'diskCapacityBytes') !~ '^[0-9]+$'
                OR (p_metrics ->> 'diskCapacityBytes')::numeric > 9223372036854775807
            )
       ) THEN
        RAISE EXCEPTION 'invalid worker heartbeat'
            USING ERRCODE = '22023';
    END IF;

    IF p_current_job_id IS NOT NULL AND (
        p_status <> 'running'
        OR NOT EXISTS (
            SELECT 1
              FROM public.operations_job AS job
             WHERE job.id = p_current_job_id
               AND job.target_role = '{worker_role}'
               AND job.state = 'running'
               AND job.lease_owner = p_worker_id
               AND job.lease_expires_at > database_now
        )
    ) THEN
        RAISE EXCEPTION 'invalid current worker job'
            USING ERRCODE = '22023';
    END IF;

    UPDATE public.operations_workerheartbeat AS heartbeat
       SET release_id = p_release_id,
           schema_identity = p_schema_identity,
           manifest_identity = p_manifest_identity,
           last_seen_at = database_now,
           current_job_id = p_current_job_id,
           status = p_status,
           metrics = p_metrics
     WHERE heartbeat.role = '{worker_role}'
       AND heartbeat.worker_id = p_worker_id
       AND heartbeat.last_seen_at < database_now
       AND (
            heartbeat.status <> 'stopping'
            OR p_status = 'stopping'
       )
    RETURNING heartbeat.id INTO heartbeat_id;
    IF heartbeat_id IS NOT NULL THEN
        RETURN heartbeat_id;
    END IF;

    SELECT heartbeat.id
      INTO heartbeat_id
      FROM public.operations_workerheartbeat AS heartbeat
     WHERE heartbeat.role = '{worker_role}'
       AND heartbeat.worker_id = p_worker_id;
    IF heartbeat_id IS NOT NULL THEN
        RETURN heartbeat_id;
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(1095059273, {allocation_lock_key});

    SELECT heartbeat.id
      INTO heartbeat_id
      FROM public.operations_workerheartbeat AS heartbeat
     WHERE heartbeat.role = '{worker_role}'
       AND heartbeat.worker_id = p_worker_id;
    IF heartbeat_id IS NOT NULL THEN
        RETURN heartbeat_id;
    END IF;

    SELECT heartbeat.id
      INTO heartbeat_id
      FROM public.operations_workerheartbeat AS heartbeat
     WHERE heartbeat.role = '{worker_role}'
       AND heartbeat.last_seen_at
           < database_now - pg_catalog.make_interval(secs => p_retention_seconds)
     ORDER BY heartbeat.last_seen_at, heartbeat.id
     LIMIT 1
     FOR UPDATE;
    IF heartbeat_id IS NOT NULL THEN
        UPDATE public.operations_workerheartbeat
           SET worker_id = p_worker_id,
               release_id = p_release_id,
               schema_identity = p_schema_identity,
               manifest_identity = p_manifest_identity,
               last_seen_at = database_now,
               current_job_id = p_current_job_id,
               status = p_status,
               metrics = p_metrics
         WHERE id = heartbeat_id;
        RETURN heartbeat_id;
    END IF;

    SELECT pg_catalog.count(*)
      INTO occupied_slots
      FROM (
          SELECT heartbeat.id
            FROM public.operations_workerheartbeat AS heartbeat
           WHERE heartbeat.role = '{worker_role}'
           LIMIT p_slot_limit
      ) AS occupied;
    IF occupied_slots >= p_slot_limit THEN
        RAISE EXCEPTION 'worker heartbeat capacity exhausted'
            USING ERRCODE = 'P0001';
    END IF;

    heartbeat_id := pg_catalog.gen_random_uuid();
    INSERT INTO public.operations_workerheartbeat (
        id,
        role,
        worker_id,
        release_id,
        schema_identity,
        manifest_identity,
        last_seen_at,
        current_job_id,
        status,
        metrics
    ) VALUES (
        heartbeat_id,
        '{worker_role}',
        p_worker_id,
        p_release_id,
        p_schema_identity,
        p_manifest_identity,
        database_now,
        p_current_job_id,
        p_status,
        p_metrics
    );
    RETURN heartbeat_id;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RAISE EXCEPTION 'invalid worker heartbeat'
            USING ERRCODE = '22023';
END;
$aegis_function$;
"""


HEARTBEAT_FUNCTION_SQL: Final = {
    "aegis_publish_operations_heartbeat": _heartbeat_function_sql(
        function_name="aegis_publish_operations_heartbeat",
        worker_role="operations",
        allocation_lock_key=1,
    ),
    "aegis_publish_indexer_heartbeat": _heartbeat_function_sql(
        function_name="aegis_publish_indexer_heartbeat",
        worker_role="indexer",
        allocation_lock_key=2,
    ),
    "aegis_publish_media_heartbeat": _heartbeat_function_sql(
        function_name="aegis_publish_media_heartbeat",
        worker_role="media",
        allocation_lock_key=3,
    ),
}

AUTHORIZATION_FUNCTION_SQL: Final = """
CREATE OR REPLACE FUNCTION public.aegis_validate_operation_authorization(
    p_operation_id uuid
)
RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $aegis_function$
DECLARE
    caller_worker_role text;
    operation_actor_id uuid;
    operation_intent jsonb;
    operation_snapshot jsonb;
    root_item jsonb;
    root_uuid uuid;
    root_ids uuid[] := ARRAY[]::uuid[];
    root_active boolean;
    root_epoch bigint;
    actor_active boolean;
    actor_epoch bigint;
    required_permissions integer;
    effective_permissions integer;
    item_count integer;
    key_count integer;
BEGIN
    IF p_operation_id IS NULL THEN
        RETURN FALSE;
    END IF;

    caller_worker_role := CASE session_user
        WHEN 'aegis_operations' THEN 'operations'
        WHEN 'aegis_indexer' THEN 'indexer'
        WHEN 'aegis_media' THEN 'media'
        ELSE NULL
    END;
    IF caller_worker_role IS NULL OR NOT EXISTS (
        SELECT 1
          FROM public.operations_job AS job
         WHERE job.operation_id = p_operation_id
           AND job.target_role = caller_worker_role
    ) THEN
        RETURN FALSE;
    END IF;

    SELECT operation.actor_id, operation.intent, operation.authorization_snapshot
      INTO operation_actor_id, operation_intent, operation_snapshot
      FROM public.operations_operation AS operation
     WHERE operation.id = p_operation_id;
    IF NOT FOUND
       OR operation_actor_id IS NULL
       OR operation_intent IS NULL
       OR operation_snapshot IS NULL
       OR pg_catalog.octet_length(operation_intent::text) > 16384
       OR pg_catalog.octet_length(operation_snapshot::text) > 16384 THEN
        RETURN FALSE;
    END IF;

    IF pg_catalog.jsonb_typeof(operation_intent) <> 'object' THEN
        RETURN FALSE;
    END IF;
    SELECT pg_catalog.count(*)
      INTO key_count
      FROM pg_catalog.jsonb_object_keys(operation_intent);
    IF key_count <> 1
       OR NOT operation_intent ? 'roots'
       OR pg_catalog.jsonb_typeof(operation_intent -> 'roots') <> 'array' THEN
        RETURN FALSE;
    END IF;
    item_count := pg_catalog.jsonb_array_length(operation_intent -> 'roots');
    IF item_count > 128 THEN
        RETURN FALSE;
    END IF;

    IF pg_catalog.jsonb_typeof(operation_snapshot) <> 'object' THEN
        RETURN FALSE;
    END IF;
    SELECT pg_catalog.count(*)
      INTO key_count
      FROM pg_catalog.jsonb_object_keys(operation_snapshot);
    IF key_count <> 2
       OR NOT operation_snapshot ? 'userEpoch'
       OR NOT operation_snapshot ? 'rootEpochs'
       OR pg_catalog.jsonb_typeof(operation_snapshot -> 'userEpoch') <> 'number'
       OR (operation_snapshot ->> 'userEpoch') !~ '^[0-9]+$'
       OR (operation_snapshot ->> 'userEpoch')::numeric > 9223372036854775807
       OR pg_catalog.jsonb_typeof(operation_snapshot -> 'rootEpochs') <> 'object'
    THEN
        RETURN FALSE;
    END IF;
    SELECT pg_catalog.count(*)
      INTO key_count
      FROM pg_catalog.jsonb_object_keys(operation_snapshot -> 'rootEpochs');
    IF key_count <> item_count THEN
        RETURN FALSE;
    END IF;

    FOR root_item IN
        SELECT item.value
          FROM pg_catalog.jsonb_array_elements(
              operation_intent -> 'roots'
          ) AS item(value)
    LOOP
        IF pg_catalog.jsonb_typeof(root_item) <> 'object' THEN
            RETURN FALSE;
        END IF;
        SELECT pg_catalog.count(*)
          INTO key_count
          FROM pg_catalog.jsonb_object_keys(root_item);
        IF key_count <> 2
           OR NOT root_item ? 'id'
           OR NOT root_item ? 'permissions'
           OR pg_catalog.jsonb_typeof(root_item -> 'id') <> 'string'
           OR pg_catalog.jsonb_typeof(root_item -> 'permissions') <> 'number'
           OR (root_item ->> 'permissions') !~ '^[1-9][0-9]{0,2}$'
        THEN
            RETURN FALSE;
        END IF;
        root_uuid := (root_item ->> 'id')::uuid;
        IF root_item ->> 'id' IS DISTINCT FROM root_uuid::text
           OR root_uuid = ANY(root_ids)
           OR (root_item ->> 'permissions')::integer > 255
           OR NOT (operation_snapshot -> 'rootEpochs') ? root_uuid::text
           OR pg_catalog.jsonb_typeof(
                (operation_snapshot -> 'rootEpochs') -> root_uuid::text
           ) <> 'number'
           OR (
                (operation_snapshot -> 'rootEpochs') ->> root_uuid::text
           ) !~ '^[0-9]+$'
           OR (
                (operation_snapshot -> 'rootEpochs') ->> root_uuid::text
           )::numeric > 9223372036854775807
        THEN
            RETURN FALSE;
        END IF;
        root_ids := pg_catalog.array_append(root_ids, root_uuid);
    END LOOP;

    FOR root_uuid IN
        SELECT candidate.root_id
          FROM pg_catalog.unnest(root_ids) AS candidate(root_id)
         ORDER BY candidate.root_id
    LOOP
        PERFORM root.id
          FROM public.roots_root AS root
         WHERE root.id = root_uuid
         FOR UPDATE;
        IF NOT FOUND THEN
            RETURN FALSE;
        END IF;
    END LOOP;

    SELECT actor.is_active, actor.authorization_epoch
      INTO actor_active, actor_epoch
      FROM public.identity_user AS actor
     WHERE actor.id = operation_actor_id
     FOR UPDATE;
    IF NOT FOUND
       OR NOT actor_active
       OR actor_epoch IS DISTINCT FROM (
            operation_snapshot ->> 'userEpoch'
       )::bigint THEN
        RETURN FALSE;
    END IF;

    FOR root_item IN
        SELECT item.value
          FROM pg_catalog.jsonb_array_elements(
              operation_intent -> 'roots'
          ) AS item(value)
         ORDER BY (item.value ->> 'id')::uuid
    LOOP
        root_uuid := (root_item ->> 'id')::uuid;
        required_permissions := (root_item ->> 'permissions')::integer;
        SELECT root.active, root.authorization_epoch
          INTO root_active, root_epoch
          FROM public.roots_root AS root
         WHERE root.id = root_uuid;
        IF NOT root_active
           OR root_epoch IS DISTINCT FROM (
                (operation_snapshot -> 'rootEpochs') ->> root_uuid::text
           )::bigint THEN
            RETURN FALSE;
        END IF;

        SELECT COALESCE(
                   pg_catalog.bit_or(root_grant.permissions::integer),
                   0
               )
          INTO effective_permissions
          FROM public.roots_rootgrant AS root_grant
         WHERE root_grant.root_id = root_uuid
           AND (
                root_grant.user_id = operation_actor_id
                OR root_grant.group_id IN (
                    SELECT membership.group_id
                      FROM public.identity_user_groups AS membership
                     WHERE membership.user_id = operation_actor_id
                )
           );
        IF effective_permissions & required_permissions
           <> required_permissions THEN
            RETURN FALSE;
        END IF;
    END LOOP;
    RETURN TRUE;
EXCEPTION
    WHEN invalid_text_representation OR numeric_value_out_of_range THEN
        RETURN FALSE;
END;
$aegis_function$;
"""


MANAGED_FUNCTION_SIGNATURES: Final[dict[str, str]] = {
    **{
        function_name: f"public.{function_name}{HEARTBEAT_FUNCTION_SIGNATURE}"
        for function_name in HEARTBEAT_FUNCTION_SQL
    },
    "aegis_validate_operation_authorization": (
        "public.aegis_validate_operation_authorization"
        f"{AUTHORIZATION_FUNCTION_SIGNATURE}"
    ),
}
MANAGED_FUNCTION_IDENTITY_ARGUMENTS: Final[dict[str, str]] = {
    **{
        function_name: "text, text, text, text, text, jsonb, uuid, integer, integer"
        for function_name in HEARTBEAT_FUNCTION_SQL
    },
    "aegis_validate_operation_authorization": "uuid",
}


class PrivilegeSynchronizationError(RuntimeError):
    """The deployed database cannot satisfy the fixed privilege contract."""


class PrivilegeDriftError(PrivilegeSynchronizationError):
    """The database schema or grants differ from the reviewed manifest."""


def current_database_login() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT session_user")
        row = cursor.fetchone()
    if row is None or not isinstance(row[0], str):
        raise PrivilegeSynchronizationError("database login identity is unavailable")
    return row[0]


def require_runtime_database_login(process_role: object) -> str:
    if not isinstance(process_role, str):
        raise PrivilegeSynchronizationError("runtime database login does not match")
    expected_login = RUNTIME_PROCESS_DATABASE_ROLES.get(process_role)
    if expected_login is None or current_database_login() != expected_login:
        raise PrivilegeSynchronizationError("runtime database login does not match")
    return expected_login


def _normalized_privileges(values: Sequence[str]) -> str:
    allowed = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "USAGE"})
    privileges = tuple(values)
    if not privileges or any(value not in allowed for value in privileges):
        raise PrivilegeSynchronizationError("invalid database privilege manifest")
    return ", ".join(privileges)


def _quoted(identifier: str) -> str:
    if not identifier or not identifier.replace("_", "").isalnum():
        raise PrivilegeSynchronizationError("invalid database privilege identifier")
    return f'"{identifier}"'


def _verify_schema_manifest(cursor: Any) -> None:
    cursor.execute(
        """
        SELECT table_name, column_name
          FROM information_schema.columns
         WHERE table_schema = 'public'
         ORDER BY table_name, ordinal_position
        """
    )
    actual: dict[str, list[str]] = {}
    for table_name, column_name in cursor.fetchall():
        actual.setdefault(table_name, []).append(column_name)
    normalized = {table: tuple(columns) for table, columns in actual.items()}
    if normalized != MANAGED_TABLE_COLUMNS:
        raise PrivilegeDriftError("database table or column privilege manifest drift")

    cursor.execute(
        """
        SELECT sequencename
          FROM pg_catalog.pg_sequences
         WHERE schemaname = 'public'
         ORDER BY sequencename
        """
    )
    sequences = tuple(row[0] for row in cursor.fetchall())
    if sequences != MANAGED_SEQUENCES:
        raise PrivilegeDriftError("database sequence privilege manifest drift")


def _verify_role_boundaries(cursor: Any) -> None:
    cursor.execute("SELECT session_user, current_user")
    if cursor.fetchone() != ("aegis_migrator", "aegis_migrator"):
        raise PrivilegeSynchronizationError(
            "database privilege synchronization requires the migrator login"
        )

    cursor.execute(
        """
        SELECT rolcanlogin, rolsuper, rolinherit, rolcreaterole,
               rolcreatedb, rolreplication, rolbypassrls
          FROM pg_catalog.pg_roles
         WHERE rolname = 'aegis_migrator'
        """
    )
    if cursor.fetchone() != (True, False, False, False, False, False, False):
        raise PrivilegeDriftError("migrator database role attributes are unsafe")

    cursor.execute(
        """
        SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole,
               rolcreatedb, rolreplication, rolbypassrls
          FROM pg_catalog.pg_roles
         WHERE rolname = ANY(%s)
         ORDER BY rolname
        """,
        [list(RUNTIME_DATABASE_ROLES)],
    )
    roles = cursor.fetchall()
    if len(roles) != len(RUNTIME_DATABASE_ROLES) or any(
        row[1:] != (True, False, False, False, False, False, False)
        for row in roles
    ):
        raise PrivilegeDriftError("runtime database role attributes are unsafe")

    cursor.execute(
        """
        SELECT member.rolname, granted.rolname
          FROM pg_catalog.pg_auth_members AS membership
          JOIN pg_catalog.pg_roles AS member
            ON member.oid = membership.member
          JOIN pg_catalog.pg_roles AS granted
            ON granted.oid = membership.roleid
         WHERE member.rolname = ANY(%s)
            OR granted.rolname = ANY(%s)
        """,
        [list(RUNTIME_DATABASE_ROLES), list(RUNTIME_DATABASE_ROLES)],
    )
    memberships = frozenset((row[0], row[1]) for row in cursor.fetchall())
    if memberships != ALLOWED_ROLE_MEMBERSHIPS:
        raise PrivilegeDriftError("runtime database role membership is unsafe")

    cursor.execute(
        """
        SELECT pg_catalog.pg_get_userbyid(database.datdba),
               pg_catalog.pg_get_userbyid(namespace.nspowner)
          FROM pg_catalog.pg_database AS database
          JOIN pg_catalog.pg_namespace AS namespace
            ON namespace.nspname = 'public'
         WHERE database.datname = pg_catalog.current_database()
        """
    )
    if cursor.fetchone() != ("aegis_migrator", "aegis_migrator"):
        raise PrivilegeDriftError(
            "database and application schema must be owned by the migrator"
        )

    cursor.execute(
        """
        SELECT tablename, tableowner
          FROM pg_catalog.pg_tables
         WHERE schemaname = 'public'
           AND tablename = ANY(%s)
        """,
        [list(MANAGED_TABLE_COLUMNS)],
    )
    owners = cursor.fetchall()
    if len(owners) != len(MANAGED_TABLE_COLUMNS) or any(
        owner != "aegis_migrator" for _table, owner in owners
    ):
        raise PrivilegeDriftError("application tables must be owned by the migrator")

    cursor.execute(
        """
        SELECT sequencename, sequenceowner
          FROM pg_catalog.pg_sequences
         WHERE schemaname = 'public'
           AND sequencename = ANY(%s)
        """,
        [list(MANAGED_SEQUENCES)],
    )
    sequence_owners = cursor.fetchall()
    if len(sequence_owners) != len(MANAGED_SEQUENCES) or any(
        owner != "aegis_migrator" for _sequence, owner in sequence_owners
    ):
        raise PrivilegeDriftError(
            "application sequences must be owned by the migrator"
        )


def _install_boundary_functions(cursor: Any) -> None:
    for function_sql in HEARTBEAT_FUNCTION_SQL.values():
        cursor.execute(function_sql)
    cursor.execute(AUTHORIZATION_FUNCTION_SQL)


def _apply_grants(cursor: Any) -> None:
    role_list = ", ".join(_quoted(role) for role in RUNTIME_DATABASE_ROLES)
    cursor.execute("SELECT pg_catalog.current_database()")
    database_row = cursor.fetchone()
    if database_row is None or not isinstance(database_row[0], str):
        raise PrivilegeSynchronizationError("database identity is unavailable")
    database = connection.ops.quote_name(database_row[0])

    cursor.execute(f"REVOKE ALL PRIVILEGES ON DATABASE {database} FROM PUBLIC")
    cursor.execute(f"REVOKE ALL PRIVILEGES ON DATABASE {database} FROM {role_list}")
    cursor.execute(f"GRANT CONNECT ON DATABASE {database} TO {role_list}")
    cursor.execute("REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC")
    cursor.execute(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {role_list}")
    cursor.execute(f"GRANT USAGE ON SCHEMA public TO {role_list}")
    cursor.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM PUBLIC")
    cursor.execute(f"REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {role_list}")
    cursor.execute("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC")
    cursor.execute(
        f"REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {role_list}"
    )
    cursor.execute("REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC")
    cursor.execute(
        f"REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public FROM {role_list}"
    )

    for table, table_column_names in MANAGED_TABLE_COLUMNS.items():
        quoted_columns = ", ".join(
            _quoted(column) for column in table_column_names
        )
        cursor.execute(
            f"REVOKE ALL PRIVILEGES ({quoted_columns}) "
            f"ON TABLE public.{_quoted(table)} FROM PUBLIC"
        )
        cursor.execute(
            f"REVOKE ALL PRIVILEGES ({quoted_columns}) "
            f"ON TABLE public.{_quoted(table)} FROM {role_list}"
        )

    for role, table_privileges in ROLE_TABLE_PRIVILEGES.items():
        for table, privileges in table_privileges.items():
            cursor.execute(
                f"GRANT {_normalized_privileges(privileges)} "
                f"ON TABLE public.{_quoted(table)} TO {_quoted(role)}"
            )
    for role, table_columns in ROLE_COLUMN_PRIVILEGES.items():
        for table, column_privileges in table_columns.items():
            by_privilege: dict[str, list[str]] = {}
            for column, privileges in column_privileges.items():
                for privilege in privileges:
                    by_privilege.setdefault(privilege, []).append(column)
            for privilege, column_names in by_privilege.items():
                quoted_columns = ", ".join(_quoted(column) for column in column_names)
                cursor.execute(
                    f"GRANT {privilege} ({quoted_columns}) "
                    f"ON TABLE public.{_quoted(table)} TO {_quoted(role)}"
                )
    for role, sequence_privileges in ROLE_SEQUENCE_PRIVILEGES.items():
        for sequence, privileges in sequence_privileges.items():
            cursor.execute(
                f"GRANT {_normalized_privileges(privileges)} "
                f"ON SEQUENCE public.{_quoted(sequence)} TO {_quoted(role)}"
            )

    for function_name, signature in MANAGED_FUNCTION_SIGNATURES.items():
        cursor.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        cursor.execute(f"REVOKE ALL ON FUNCTION {signature} FROM {role_list}")
        for role, functions in ROLE_FUNCTION_PRIVILEGES.items():
            if function_name in functions:
                cursor.execute(
                    f"GRANT EXECUTE ON FUNCTION {signature} TO {_quoted(role)}"
                )


def _verify_function_boundaries(cursor: Any) -> None:
    cursor.execute(
        """
        SELECT function.proname,
               function.prosecdef,
               pg_catalog.pg_get_userbyid(function.proowner),
               function.proconfig,
               pg_catalog.oidvectortypes(function.proargtypes),
               language.lanname
          FROM pg_catalog.pg_proc AS function
          JOIN pg_catalog.pg_namespace AS namespace
            ON namespace.oid = function.pronamespace
          JOIN pg_catalog.pg_language AS language
            ON language.oid = function.prolang
         WHERE namespace.nspname = 'public'
           AND function.proname = ANY(%s)
         ORDER BY function.proname
        """,
        [list(MANAGED_FUNCTION_SIGNATURES)],
    )
    functions = cursor.fetchall()
    if len(functions) != len(MANAGED_FUNCTION_SIGNATURES):
        raise PrivilegeDriftError("database boundary function identity drift")
    for name, security_definer, owner, config, arguments, language in functions:
        if (
            name not in MANAGED_FUNCTION_SIGNATURES
            or security_definer is not True
            or owner != "aegis_migrator"
            or tuple(config or ()) != ('search_path=""',)
            or arguments != MANAGED_FUNCTION_IDENTITY_ARGUMENTS[name]
            or language != "plpgsql"
        ):
            raise PrivilegeDriftError("database boundary function metadata drift")


def _verify_effective_grants(cursor: Any) -> None:
    for role in RUNTIME_DATABASE_ROLES:
        cursor.execute(
            """
            SELECT pg_catalog.has_database_privilege(
                       %s, pg_catalog.current_database(), 'CONNECT'
                   ),
                   pg_catalog.has_database_privilege(
                       %s, pg_catalog.current_database(), 'CREATE'
                   ),
                   pg_catalog.has_database_privilege(
                       %s, pg_catalog.current_database(), 'TEMPORARY'
                   ),
                   pg_catalog.has_schema_privilege(%s, 'public', 'USAGE'),
                   pg_catalog.has_schema_privilege(%s, 'public', 'CREATE')
            """,
            [role, role, role, role, role],
        )
        if cursor.fetchone() != (True, False, False, True, False):
            raise PrivilegeDriftError("database or schema privilege drift")

        for table, columns in MANAGED_TABLE_COLUMNS.items():
            table_grants = ROLE_TABLE_PRIVILEGES[role].get(table, ())
            column_grants = ROLE_COLUMN_PRIVILEGES[role].get(table, {})
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            ):
                cursor.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    [role, f"public.{table}", privilege],
                )
                if cursor.fetchone() != (privilege in table_grants,):
                    raise PrivilegeDriftError("database table privilege drift")
            for column in columns:
                for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    expected = privilege in table_grants or privilege in column_grants.get(
                        column, ()
                    )
                    cursor.execute(
                        "SELECT has_column_privilege(%s, %s, %s, %s)",
                        [role, f"public.{table}", column, privilege],
                    )
                    if cursor.fetchone() != (expected,):
                        raise PrivilegeDriftError("database column privilege drift")

        for sequence in MANAGED_SEQUENCES:
            expected_privileges = ROLE_SEQUENCE_PRIVILEGES[role].get(sequence, ())
            for privilege in ("USAGE", "SELECT", "UPDATE"):
                cursor.execute(
                    "SELECT has_sequence_privilege(%s, %s, %s)",
                    [role, f"public.{sequence}", privilege],
                )
                if cursor.fetchone() != (privilege in expected_privileges,):
                    raise PrivilegeDriftError("database sequence privilege drift")

        for function_name, signature in MANAGED_FUNCTION_SIGNATURES.items():
            expected = function_name in ROLE_FUNCTION_PRIVILEGES[role]
            cursor.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                [role, signature],
            )
            if cursor.fetchone() != (expected,):
                raise PrivilegeDriftError("database function privilege drift")

    cursor.execute(
        """
        SELECT pg_catalog.has_database_privilege(
                   'public', pg_catalog.current_database(), 'CONNECT'
               ),
               pg_catalog.has_database_privilege(
                   'public', pg_catalog.current_database(), 'CREATE'
               ),
               pg_catalog.has_database_privilege(
                   'public', pg_catalog.current_database(), 'TEMPORARY'
               ),
               pg_catalog.has_schema_privilege('public', 'public', 'USAGE'),
               pg_catalog.has_schema_privilege('public', 'public', 'CREATE')
        """
    )
    if cursor.fetchone() != (False, False, False, False, False):
        raise PrivilegeDriftError("public database or schema privilege drift")

    for table, columns in MANAGED_TABLE_COLUMNS.items():
        for privilege in (
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
        ):
            cursor.execute(
                "SELECT pg_catalog.has_table_privilege('public', %s, %s)",
                [f"public.{table}", privilege],
            )
            if cursor.fetchone() != (False,):
                raise PrivilegeDriftError("public table privilege drift")
        for column in columns:
            for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                cursor.execute(
                    "SELECT pg_catalog.has_column_privilege('public', %s, %s, %s)",
                    [f"public.{table}", column, privilege],
                )
                if cursor.fetchone() != (False,):
                    raise PrivilegeDriftError("public column privilege drift")

    for sequence in MANAGED_SEQUENCES:
        for privilege in ("USAGE", "SELECT", "UPDATE"):
            cursor.execute(
                "SELECT pg_catalog.has_sequence_privilege('public', %s, %s)",
                [f"public.{sequence}", privilege],
            )
            if cursor.fetchone() != (False,):
                raise PrivilegeDriftError("public sequence privilege drift")

    for signature in MANAGED_FUNCTION_SIGNATURES.values():
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM pg_catalog.pg_proc AS function
                  CROSS JOIN LATERAL pg_catalog.aclexplode(
                      COALESCE(
                          function.proacl,
                          pg_catalog.acldefault('f', function.proowner)
                      )
                  ) AS acl
                 WHERE function.oid = %s::pg_catalog.regprocedure
                   AND acl.grantee = 0
                   AND acl.privilege_type = 'EXECUTE'
            )
            """,
            [signature],
        )
        if cursor.fetchone() != (False,):
            raise PrivilegeDriftError("database function public privilege drift")


def synchronize_database_privileges() -> None:
    if not connection.in_atomic_block:
        raise PrivilegeSynchronizationError(
            "database privilege synchronization requires an atomic transaction"
        )
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s, %s)", [1095059273, 11])
        _verify_role_boundaries(cursor)
        _verify_schema_manifest(cursor)
        _install_boundary_functions(cursor)
        _apply_grants(cursor)
        _verify_function_boundaries(cursor)
        _verify_effective_grants(cursor)
