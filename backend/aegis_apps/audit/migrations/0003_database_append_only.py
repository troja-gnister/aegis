from django.db import migrations

FORWARD_SQL = """
CREATE FUNCTION public.aegis_audit_event_append_only_guard()
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

    RAISE EXCEPTION 'audit records are append-only'
        USING ERRCODE = '55000';
END;
$aegis_function$;

CREATE TRIGGER aegis_audit_event_append_only
BEFORE UPDATE OR DELETE OR TRUNCATE ON public.audit_auditevent
FOR EACH STATEMENT
EXECUTE FUNCTION public.aegis_audit_event_append_only_guard();
"""


REVERSE_SQL = """
DROP TRIGGER IF EXISTS aegis_audit_event_append_only
    ON public.audit_auditevent;
DROP FUNCTION IF EXISTS public.aegis_audit_event_append_only_guard();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0002_auditevent_manager_names"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
