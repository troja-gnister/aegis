from django.db import migrations

FORWARD_SQL = """
CREATE OR REPLACE FUNCTION public.aegis_job_execution_marker_guard()
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

    -- Claim a queued or due retry job. A relinquished attempt is credited and
    -- therefore reuses its attempt count; every other claim advances it.
    IF OLD.state IN ('queued', 'retry_wait')
       AND NEW.state = 'running'
       AND OLD.available_at <= database_now
       AND NEW.available_at IS NOT DISTINCT FROM OLD.available_at
       AND OLD.execution_started_at IS NULL
       AND NEW.execution_started_at IS NULL
       AND NEW.attempt_token = OLD.attempt_token + 1
       AND (
            (
                OLD.state = 'retry_wait'
                AND OLD.safe_error_code = 'claim_relinquished'
                AND OLD.attempts > 0
                AND NEW.attempts = OLD.attempts
            )
            OR
            (
                NOT (
                    OLD.state = 'retry_wait'
                    AND OLD.safe_error_code = 'claim_relinquished'
                    AND OLD.attempts > 0
                )
                AND OLD.attempts < OLD.max_attempts
                AND NEW.attempts = OLD.attempts + 1
            )
       )
       AND NEW.lease_owner IS NOT NULL
       AND NEW.lease_expires_at > database_now
       AND NEW.lease_expires_at <= database_now + interval '300 seconds'
       AND NEW.safe_error_code IS NULL
       AND NEW.safe_error_detail IS NULL
       AND NEW.result IS NULL THEN
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    -- A role-correct takeover may replace only an expired running attempt.
    IF OLD.state = 'running'
       AND NEW.state = 'running'
       AND OLD.lease_owner IS NOT NULL
       AND OLD.lease_expires_at <= database_now
       AND OLD.attempts < OLD.max_attempts
       AND NEW.available_at IS NOT DISTINCT FROM OLD.available_at
       AND NEW.attempts = OLD.attempts + 1
       AND NEW.attempt_token = OLD.attempt_token + 1
       AND NEW.execution_started_at IS NULL
       AND NEW.lease_owner IS NOT NULL
       AND NEW.lease_expires_at > database_now
       AND NEW.lease_expires_at <= database_now + interval '300 seconds'
       AND NEW.safe_error_code IS NULL
       AND NEW.safe_error_detail IS NULL
       AND NEW.result IS NULL THEN
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    -- The first execution start preserves the live lease fence and replaces a
    -- caller-supplied marker with authoritative database time.
    IF OLD.state = 'running'
       AND NEW.state = 'running'
       AND OLD.lease_owner IS NOT NULL
       AND OLD.lease_expires_at > database_now
       AND NEW.available_at IS NOT DISTINCT FROM OLD.available_at
       AND NEW.attempts = OLD.attempts
       AND NEW.attempt_token = OLD.attempt_token
       AND NEW.lease_owner IS NOT DISTINCT FROM OLD.lease_owner
       AND NEW.lease_expires_at IS NOT DISTINCT FROM OLD.lease_expires_at
       AND OLD.execution_started_at IS NULL
       AND NEW.execution_started_at IS NOT NULL
       AND NEW.safe_error_code IS NOT DISTINCT FROM OLD.safe_error_code
       AND NEW.safe_error_detail IS NOT DISTINCT FROM OLD.safe_error_detail
       AND NEW.result IS NOT DISTINCT FROM OLD.result THEN
        NEW.execution_started_at := database_now;
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    -- Lease renewal is monotonic, role fenced, and bounded by the configured
    -- global maximum lease duration.
    IF OLD.state = 'running'
       AND NEW.state = 'running'
       AND OLD.lease_owner IS NOT NULL
       AND OLD.lease_expires_at > database_now
       AND NEW.available_at IS NOT DISTINCT FROM OLD.available_at
       AND NEW.attempts = OLD.attempts
       AND NEW.attempt_token = OLD.attempt_token
       AND NEW.execution_started_at IS NOT DISTINCT FROM OLD.execution_started_at
       AND NEW.lease_owner IS NOT DISTINCT FROM OLD.lease_owner
       AND NEW.lease_expires_at >= OLD.lease_expires_at
       AND NEW.lease_expires_at > database_now
       AND NEW.lease_expires_at <= database_now + interval '300 seconds'
       AND NEW.safe_error_code IS NOT DISTINCT FROM OLD.safe_error_code
       AND NEW.safe_error_detail IS NOT DISTINCT FROM OLD.safe_error_detail
       AND NEW.result IS NOT DISTINCT FROM OLD.result THEN
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    -- Shutdown may relinquish only a live, unstarted claim. Availability and
    -- update timestamps become authoritative database time for immediate reuse.
    IF OLD.state = 'running'
       AND NEW.state = 'retry_wait'
       AND OLD.lease_owner IS NOT NULL
       AND OLD.lease_expires_at > database_now
       AND OLD.execution_started_at IS NULL
       AND NEW.execution_started_at IS NULL
       AND NEW.attempts = OLD.attempts
       AND NEW.attempt_token = OLD.attempt_token
       AND NEW.lease_owner IS NULL
       AND NEW.lease_expires_at IS NULL
       AND NEW.safe_error_code = 'claim_relinquished'
       AND NEW.safe_error_detail = 'The unstarted claim was released safely.'
       AND NEW.result IS NULL THEN
        NEW.available_at := database_now;
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    -- Retry preserves the live fence values, clears the start marker, and may
    -- schedule only within the reviewed maximum backoff plus jitter ceiling.
    IF OLD.state = 'running'
       AND NEW.state = 'retry_wait'
       AND OLD.lease_owner IS NOT NULL
       AND OLD.lease_expires_at > database_now
       AND NEW.available_at > database_now
       AND NEW.available_at <= database_now + interval '86405 seconds'
       AND NEW.attempts = OLD.attempts
       AND NEW.attempt_token = OLD.attempt_token
       AND NEW.execution_started_at IS NULL
       AND NEW.lease_owner IS NULL
       AND NEW.lease_expires_at IS NULL
       AND NEW.safe_error_code = 'retryable_failure'
       AND NEW.safe_error_detail = 'The job will be retried.'
       AND NEW.result IS NULL THEN
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    -- Successful completion requires a live started attempt and the only
    -- currently supported result schema.
    IF OLD.state = 'running'
       AND NEW.state = 'succeeded'
       AND OLD.lease_owner IS NOT NULL
       AND OLD.lease_expires_at > database_now
       AND OLD.execution_started_at IS NOT NULL
       AND NEW.available_at IS NOT DISTINCT FROM OLD.available_at
       AND NEW.attempts = OLD.attempts
       AND NEW.attempt_token = OLD.attempt_token
       AND NEW.execution_started_at IS NOT DISTINCT FROM OLD.execution_started_at
       AND NEW.lease_owner IS NULL
       AND NEW.lease_expires_at IS NULL
       AND NEW.safe_error_code IS NULL
       AND NEW.safe_error_detail IS NULL
       AND NEW.result = '{"ok": true}'::jsonb THEN
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    -- Safe terminal failure may occur before or after start, but it must retain
    -- the attempt fence and use one of the fixed non-sensitive failure details.
    IF OLD.state = 'running'
       AND NEW.state = 'failed'
       AND OLD.lease_owner IS NOT NULL
       AND OLD.lease_expires_at > database_now
       AND NEW.available_at IS NOT DISTINCT FROM OLD.available_at
       AND NEW.attempts = OLD.attempts
       AND NEW.attempt_token = OLD.attempt_token
       AND NEW.execution_started_at IS NOT DISTINCT FROM OLD.execution_started_at
       AND NEW.lease_owner IS NULL
       AND NEW.lease_expires_at IS NULL
       AND NEW.result IS NULL
       AND (
            (
                NEW.safe_error_code = 'authorization_stale'
                AND NEW.safe_error_detail =
                    'Authorization changed before execution completed.'
            )
            OR
            (
                NEW.safe_error_code = 'handler_failed'
                AND NEW.safe_error_detail = 'The job handler failed safely.'
            )
            OR
            (
                OLD.attempts >= OLD.max_attempts
                AND NEW.safe_error_code = 'attempts_exhausted'
                AND NEW.safe_error_detail = 'The job exhausted its attempts.'
            )
       ) THEN
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    -- Claim-loop exhaustion advances the token once and terminally fences a due
    -- queued/retry job or an expired running job. Relinquished attempts remain
    -- eligible for their credited reclaim instead.
    IF NEW.state = 'failed'
       AND OLD.attempts >= OLD.max_attempts
       AND (
            (
                OLD.state IN ('queued', 'retry_wait')
                AND OLD.available_at <= database_now
                AND NOT (
                    OLD.state = 'retry_wait'
                    AND OLD.safe_error_code = 'claim_relinquished'
                    AND OLD.attempts > 0
                )
            )
            OR
            (
                OLD.state = 'running'
                AND OLD.lease_owner IS NOT NULL
                AND OLD.lease_expires_at <= database_now
            )
       )
       AND NEW.available_at IS NOT DISTINCT FROM OLD.available_at
       AND NEW.attempts = OLD.attempts
       AND NEW.attempt_token = OLD.attempt_token + 1
       AND NEW.execution_started_at IS NOT DISTINCT FROM OLD.execution_started_at
       AND NEW.lease_owner IS NULL
       AND NEW.lease_expires_at IS NULL
       AND NEW.safe_error_code = 'attempts_exhausted'
       AND NEW.safe_error_detail = 'The job exhausted its attempts.'
       AND NEW.result IS NULL THEN
        NEW.updated_at := database_now;
        RETURN NEW;
    END IF;

    RAISE EXCEPTION 'job execution transition is invalid'
        USING ERRCODE = '55000';
END;
$aegis_function$;
"""


REVERSE_SQL = """
CREATE OR REPLACE FUNCTION public.aegis_job_execution_marker_guard()
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
"""


class Migration(migrations.Migration):
    dependencies = [
        ("operations", "0008_database_immutability"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
