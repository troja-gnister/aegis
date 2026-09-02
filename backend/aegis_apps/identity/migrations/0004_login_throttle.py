import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("identity", "0003_group_identity"),
    ]

    operations = [
        migrations.CreateModel(
            name="LoginThrottleBucket",
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
                    "kind",
                    models.CharField(
                        choices=[("account", "Account"), ("ip", "Ip")],
                        max_length=16,
                    ),
                ),
                ("key_digest", models.BinaryField(max_length=32)),
                ("window_started_at", models.DateTimeField()),
                ("failures", models.PositiveSmallIntegerField(default=0)),
                ("blocked_until", models.DateTimeField(null=True)),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("kind", "key_digest"),
                        name="identity_throttle_key_uniq",
                    )
                ],
            },
        ),
    ]
