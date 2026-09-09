from __future__ import annotations

from django.db import migrations, models

DEMOTE_AMBIGUOUS_NULL_ACTOR_KEYS = """
LOCK TABLE operations_operation IN ACCESS EXCLUSIVE MODE;

WITH ambiguous_keys AS (
    SELECT request_id
    FROM operations_operation
    WHERE actor_id IS NULL
      AND idempotency_namespace = 'v1'
    GROUP BY request_id
    HAVING COUNT(*) > 1
)
UPDATE operations_operation AS operation
SET idempotency_namespace = NULL
FROM ambiguous_keys
WHERE operation.actor_id IS NULL
  AND operation.idempotency_namespace = 'v1'
  AND operation.request_id = ambiguous_keys.request_id
"""


class Migration(migrations.Migration):
    dependencies = [("operations", "0006_job_execution_started_at")]

    operations = [
        migrations.RunSQL(
            sql=DEMOTE_AMBIGUOUS_NULL_ACTOR_KEYS,
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RemoveConstraint(
            model_name="operation",
            name="operations_operation_actor_request_v1_uniq",
        ),
        migrations.AddConstraint(
            model_name="operation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("idempotency_namespace", "v1")),
                fields=("actor", "request_id"),
                name="operations_operation_actor_request_v1_uniq",
                nulls_distinct=False,
            ),
        ),
    ]
