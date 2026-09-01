import uuid
from typing import Any

import django.db.models.deletion
from django.db import migrations, models


def backfill_group_identities(apps: Any, schema_editor: Any) -> None:
    del schema_editor
    group_model = apps.get_model("auth", "Group")
    identity_model = apps.get_model("identity", "GroupIdentity")
    identity_model.objects.bulk_create(
        identity_model(id=uuid.uuid4(), group_id=group_id)
        for group_id in group_model.objects.values_list("pk", flat=True).iterator()
    )


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0002_identity_constraints"),
    ]

    operations = [
        migrations.CreateModel(
            name="GroupIdentity",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "group",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="aegis_identity",
                        to="auth.group",
                    ),
                ),
            ],
        ),
        migrations.RunPython(backfill_group_identities, migrations.RunPython.noop),
    ]
