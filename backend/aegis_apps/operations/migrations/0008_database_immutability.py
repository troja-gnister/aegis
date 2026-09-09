from django.db import migrations

FORWARD_SQL = """
CREATE FUNCTION public.aegis_operation_namespace_insert_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $aegis_function$
BEGIN
    IF NEW.idempotency_namespace IS NULL THEN
        RAISE EXCEPTION 'operation idempotency namespace is reserved'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$aegis_function$;

CREATE TRIGGER aegis_operation_namespace_insert
BEFORE INSERT ON public.operations_operation
FOR EACH ROW
EXECUTE FUNCTION public.aegis_operation_namespace_insert_guard();

CREATE FUNCTION public.aegis_operation_intent_append_only_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $aegis_function$
DECLARE
    relation_owner oid;
BEGIN
    SELECT relation.relowner
      INTO relation_owner
      FROM pg_catalog.pg_class AS relation
     WHERE relation.oid = TG_RELID;

    IF relation_owner IS NOT NULL
       AND pg_catalog.pg_has_role(current_user, relation_owner, 'MEMBER') THEN
        RETURN NULL;
    END IF;

    RAISE EXCEPTION 'operation intents are append-only'
        USING ERRCODE = '55000';
END;
$aegis_function$;

CREATE TRIGGER aegis_operation_intent_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON public.operations_operation
FOR EACH STATEMENT
EXECUTE FUNCTION public.aegis_operation_intent_append_only_guard();

CREATE FUNCTION public.aegis_job_immutable_fields_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $aegis_function$
DECLARE
    relation_owner oid;
BEGIN
    SELECT relation.relowner
      INTO relation_owner
      FROM pg_catalog.pg_class AS relation
     WHERE relation.oid = TG_RELID;

    IF relation_owner IS NOT NULL
       AND pg_catalog.pg_has_role(current_user, relation_owner, 'MEMBER') THEN
        RETURN NEW;
    END IF;

    IF OLD.id IS DISTINCT FROM NEW.id
       OR OLD.operation_id IS DISTINCT FROM NEW.operation_id
       OR OLD.target_role IS DISTINCT FROM NEW.target_role
       OR OLD.kind IS DISTINCT FROM NEW.kind
       OR OLD.payload IS DISTINCT FROM NEW.payload
       OR OLD.priority IS DISTINCT FROM NEW.priority
       OR OLD.max_attempts IS DISTINCT FROM NEW.max_attempts
       OR OLD.created_at IS DISTINCT FROM NEW.created_at THEN
        RAISE EXCEPTION 'job immutable fields cannot change'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$aegis_function$;

CREATE TRIGGER aegis_job_immutable_fields
BEFORE UPDATE ON public.operations_job
FOR EACH ROW
EXECUTE FUNCTION public.aegis_job_immutable_fields_guard();

CREATE FUNCTION public.aegis_job_retention_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $aegis_function$
DECLARE
    relation_owner oid;
BEGIN
    SELECT relation.relowner
      INTO relation_owner
      FROM pg_catalog.pg_class AS relation
     WHERE relation.oid = TG_RELID;

    IF relation_owner IS NOT NULL
       AND pg_catalog.pg_has_role(current_user, relation_owner, 'MEMBER') THEN
        RETURN NULL;
    END IF;

    RAISE EXCEPTION 'durable jobs require an explicit retention boundary'
        USING ERRCODE = '55000';
END;
$aegis_function$;

CREATE TRIGGER aegis_job_retention
BEFORE DELETE OR TRUNCATE ON public.operations_job
FOR EACH STATEMENT
EXECUTE FUNCTION public.aegis_job_retention_guard();

CREATE FUNCTION public.aegis_job_execution_marker_guard()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = ''
AS $aegis_function$
DECLARE
    relation_owner oid;
    runtime_role text;
    database_now timestamptz := pg_catalog.clock_timestamp();
BEGIN
    SELECT relation.relowner
      INTO relation_owner
      FROM pg_catalog.pg_class AS relation
     WHERE relation.oid = TG_RELID;

    IF relation_owner IS NOT NULL
       AND pg_catalog.pg_has_role(current_user, relation_owner, 'MEMBER') THEN
        RETURN NEW;
    END IF;

    runtime_role := CASE current_user
        WHEN 'aegis_operations' THEN 'operations'
        WHEN 'aegis_indexer' THEN 'indexer'
        WHEN 'aegis_media' THEN 'media'
        ELSE NULL
    END;
    IF runtime_role IS NULL OR OLD.target_role IS DISTINCT FROM runtime_role THEN
        RAISE EXCEPTION 'job update is outside the database role boundary'
            USING ERRCODE = '42501';
    END IF;

    IF OLD.execution_started_at IS NOT DISTINCT FROM NEW.execution_started_at THEN
        RETURN NEW;
    END IF;

    IF OLD.execution_started_at IS NULL
       AND NEW.execution_started_at IS NOT NULL
       AND OLD.state = 'running'
       AND NEW.state = 'running'
       AND OLD.attempt_token = NEW.attempt_token
       AND OLD.attempts = NEW.attempts
       AND OLD.lease_owner IS NOT NULL
       AND OLD.lease_owner IS NOT DISTINCT FROM NEW.lease_owner
       AND OLD.lease_expires_at IS NOT DISTINCT FROM NEW.lease_expires_at
       AND OLD.lease_expires_at > database_now THEN
        NEW.execution_started_at := database_now;
        RETURN NEW;
    END IF;

    IF OLD.execution_started_at IS NOT NULL
       AND NEW.execution_started_at IS NULL
       AND OLD.state = 'running'
       AND OLD.lease_owner IS NOT NULL
       AND (
            (
                NEW.state = 'retry_wait'
                AND OLD.attempt_token = NEW.attempt_token
                AND OLD.attempts = NEW.attempts
                AND OLD.lease_expires_at > database_now
                AND NEW.lease_owner IS NULL
                AND NEW.lease_expires_at IS NULL
                AND NEW.safe_error_code = 'retryable_failure'
                AND NEW.available_at > database_now
                AND NEW.available_at <= database_now + interval '86405 seconds'
            )
            OR
            (
                NEW.state = 'running'
                AND OLD.lease_expires_at <= database_now
                AND NEW.attempt_token = OLD.attempt_token + 1
                AND NEW.attempts = OLD.attempts + 1
                AND NEW.lease_owner IS NOT NULL
                AND NEW.lease_expires_at > database_now
                AND NEW.safe_error_code IS NULL
                AND NEW.safe_error_detail IS NULL
                AND NEW.result IS NULL
            )
       ) THEN
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'job execution marker transition is invalid'
        USING ERRCODE = '55000';
END;
$aegis_function$;

CREATE TRIGGER aegis_job_execution_marker
BEFORE UPDATE ON public.operations_job
FOR EACH ROW
EXECUTE FUNCTION public.aegis_job_execution_marker_guard();
"""


REVERSE_SQL = """
DROP TRIGGER IF EXISTS aegis_job_execution_marker
    ON public.operations_job;
DROP FUNCTION IF EXISTS public.aegis_job_execution_marker_guard();
DROP TRIGGER IF EXISTS aegis_job_retention
    ON public.operations_job;
DROP FUNCTION IF EXISTS public.aegis_job_retention_guard();
DROP TRIGGER IF EXISTS aegis_job_immutable_fields
    ON public.operations_job;
DROP FUNCTION IF EXISTS public.aegis_job_immutable_fields_guard();
DROP TRIGGER IF EXISTS aegis_operation_intent_append_only
    ON public.operations_operation;
DROP FUNCTION IF EXISTS public.aegis_operation_intent_append_only_guard();
DROP TRIGGER IF EXISTS aegis_operation_namespace_insert
    ON public.operations_operation;
DROP FUNCTION IF EXISTS public.aegis_operation_namespace_insert_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0007_operation_idempotency_namespace_boundary"),
        ("roots", "0001_initial"),
        ("identity", "0004_login_throttle"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
