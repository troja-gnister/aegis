from django.db import migrations

# Before this release, starting a scan run kept the previous runs' progress counters, so
# they grew with every generation. The scan functions (installed by the privilege
# synchronization of every deployment) now reset them when a run starts and keep each
# directory counted once, by its terminal state. This one-time repair recounts each root's
# counters by that same definition from the directory work of its latest run, the active
# one included. The table lock waits for in-flight scan checkpoints, which hold
# the root state row from their fence onwards, and holds new ones until commit.
RECOUNT_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (run.root_id) run.root_id, run.id
          FROM public.indexing_scanrun AS run
         ORDER BY run.root_id, run.generation DESC, run.started_at DESC, run.id DESC
    ), totals AS (
        SELECT latest.root_id,
               COALESCE(pg_catalog.sum(work.observed_count), 0) AS observed,
               pg_catalog.count(work.id) FILTER (WHERE work.state = 'complete') AS completed,
               pg_catalog.count(work.id) FILTER (WHERE work.state = 'degraded') AS degraded
          FROM latest
          LEFT JOIN public.indexing_directorywork AS work ON work.run_id = latest.id
         GROUP BY latest.root_id
    )
    UPDATE public.indexing_rootindexstate AS state
       SET observed_entries = totals.observed,
           completed_directories = totals.completed,
           degraded_directories = totals.degraded
      FROM totals
     WHERE state.root_id = totals.root_id
"""


class Migration(migrations.Migration):
    dependencies = [("indexing", "0003_commit_fence")]

    operations = [
        migrations.RunSQL(
            sql=[
                "LOCK TABLE public.indexing_rootindexstate IN EXCLUSIVE MODE",
                RECOUNT_SQL,
            ],
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
