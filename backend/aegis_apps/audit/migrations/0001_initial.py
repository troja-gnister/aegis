import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("identity", "0002_identity_constraints"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AuditEvent",
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
                ("occurred_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("event_type", models.CharField(db_index=True, max_length=96)),
                ("outcome", models.CharField(max_length=24)),
                ("request_id", models.CharField(db_index=True, max_length=64)),
                ("root_id", models.UUIDField(null=True)),
                ("object_id", models.UUIDField(null=True)),
                ("metadata", models.JSONField(default=dict)),
                (
                    "actor",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ("-occurred_at", "-id")},
        ),
    ]
