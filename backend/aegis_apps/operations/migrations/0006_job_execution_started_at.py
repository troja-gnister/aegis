from __future__ import annotations

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("operations", "0005_operation_idempotency_namespace")]

    operations = [
        migrations.AddField(
            model_name="job",
            name="execution_started_at",
            field=models.DateTimeField(editable=False, null=True),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(execution_started_at__isnull=True)
                    | models.Q(state__in=("running", "succeeded", "failed"))
                ),
                name="operations_job_execution_start_state",
            ),
        ),
    ]
