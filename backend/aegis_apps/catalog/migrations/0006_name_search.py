from django.contrib.postgres.indexes import GinIndex
from django.db import migrations, models

# pg_trgm and btree_gin are trusted extensions, so the migrator (the database's owner)
# installs them. Their objects belong to the bootstrap superuser, so they live in a schema
# of their own, outside the application schema whose functions privilege sync revokes.
# The search name is the order key as text: name keys are UTF-8, and so must the database
# be, or a restore elsewhere would decode them wrongly.
EXTENSIONS = """
    DO $encoding$ BEGIN
        IF pg_catalog.getdatabaseencoding() <> 'UTF8' THEN
            RAISE EXCEPTION 'name search needs a UTF8 database, not %',
                pg_catalog.getdatabaseencoding();
        END IF;
    END $encoding$;
    CREATE SCHEMA IF NOT EXISTS aegis_extensions;
    REVOKE ALL ON SCHEMA aegis_extensions FROM PUBLIC;
    CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA aegis_extensions;
    CREATE EXTENSION IF NOT EXISTS btree_gin WITH SCHEMA aegis_extensions;
    CREATE FUNCTION public.aegis_search_name(name_key bytea) RETURNS text
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
        RETURN pg_catalog.convert_from(name_key, 'UTF8');
    REVOKE ALL ON FUNCTION public.aegis_search_name(bytea) FROM PUBLIC;
"""
DROP_EXTENSIONS = """
    DROP FUNCTION public.aegis_search_name(bytea);
    DROP EXTENSION btree_gin;
    DROP EXTENSION pg_trgm;
    DROP SCHEMA aegis_extensions;
"""
OPCLASSES = ("aegis_extensions.uuid_ops", "aegis_extensions.gin_trgm_ops")


class Migration(migrations.Migration):
    dependencies = [("catalog", "0005_cascade_indexes")]
    operations = [
        migrations.RunSQL(sql=EXTENSIONS, reverse_sql=DROP_EXTENSIONS),
        # Adding a stored column rewrites the table and its indexes once.
        migrations.AddField(
            model_name="catalogentry",
            name="search_name",
            field=models.GeneratedField(
                db_persist=True,
                expression=models.Func(
                    models.F("name_key"),
                    function="public.aegis_search_name",
                    output_field=models.TextField(),
                ),
                output_field=models.TextField(),
            ),
        ),
        migrations.AddIndex(
            model_name="catalogentry",
            index=GinIndex(
                condition=models.Q(kind="directory"),
                fields=["root", "search_name"],
                name="catalog_search_directories",
                opclasses=OPCLASSES,
            ),
        ),
        migrations.AddIndex(
            model_name="catalogentry",
            index=GinIndex(
                condition=~models.Q(kind="directory"),
                fields=["root", "search_name"],
                name="catalog_search_files",
                opclasses=OPCLASSES,
            ),
        ),
    ]
