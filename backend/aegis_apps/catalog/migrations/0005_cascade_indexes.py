from django.db import migrations, models

# Folders that earlier releases marked missing (or that a file replaced) kept their
# descendants present. Flag them once so the next scan retires those subtrees.
FLAG_STALE_SUBTREES = """
    UPDATE public.catalog_catalogentry AS entry SET retire_children=true
     WHERE (entry.source_state='missing' OR entry.kind<>'directory')
       AND EXISTS (SELECT 1 FROM public.catalog_catalogentry AS child
                    WHERE child.root_id=entry.root_id AND child.source_parent_id=entry.id
                      AND child.source_state<>'missing')
"""


class Migration(migrations.Migration):
    dependencies = [("catalog", "0004_root_scope_indexes")]
    operations = [
        migrations.AddField(
            model_name="catalogentry",
            name="retire_children",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddIndex(
            model_name="catalogentry",
            index=models.Index(
                fields=("root", "id"),
                condition=models.Q(retire_children=True),
                name="catalog_retiring",
            ),
        ),
        migrations.AddIndex(
            model_name="catalogentry",
            index=models.Index(
                fields=("root", "source_parent", "id"),
                condition=~models.Q(source_state="missing"),
                name="catalog_live_children",
            ),
        ),
        migrations.RunSQL(sql=FLAG_STALE_SUBTREES, reverse_sql=migrations.RunSQL.noop),
    ]
