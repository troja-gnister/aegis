from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("indexing", "0001_initial")]

    operations = [
        migrations.RunSQL(
            sql="""
                CREATE FUNCTION public.aegis_directory_counter_guard()
                RETURNS trigger
                LANGUAGE plpgsql
                SET search_path = ''
                AS $guard$
                BEGIN
                    IF NEW.attempt < OLD.attempt
                       OR (
                           NEW.attempt = OLD.attempt
                           AND NEW.last_batch_sequence < OLD.last_batch_sequence
                       ) THEN
                        RAISE EXCEPTION 'directory counters cannot decrease'
                            USING ERRCODE = '55000';
                    END IF;
                    RETURN NEW;
                END;
                $guard$;
                REVOKE ALL ON FUNCTION public.aegis_directory_counter_guard() FROM PUBLIC;
                CREATE TRIGGER aegis_directory_counter_guard
                    BEFORE UPDATE ON public.indexing_directorywork
                    FOR EACH ROW EXECUTE FUNCTION public.aegis_directory_counter_guard();
            """,
            reverse_sql="""
                DROP TRIGGER aegis_directory_counter_guard ON public.indexing_directorywork;
                DROP FUNCTION public.aegis_directory_counter_guard();
            """,
        )
    ]
