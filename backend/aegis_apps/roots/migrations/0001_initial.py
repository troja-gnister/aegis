import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import aegis_apps.roots.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Root",
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
                    "slot_id",
                    models.CharField(
                        max_length=63,
                        unique=True,
                        validators=(aegis_apps.roots.models.validate_slot_id,),
                    ),
                ),
                (
                    "display_name",
                    models.CharField(
                        max_length=160,
                        validators=(aegis_apps.roots.models.validate_display_name,),
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[("read_only", "Read Only"), ("read_write", "Read Write")],
                        max_length=16,
                    ),
                ),
                ("active", models.BooleanField(default=False)),
                (
                    "authorization_epoch",
                    models.PositiveBigIntegerField(default=0, editable=False),
                ),
                (
                    "capabilities",
                    models.JSONField(blank=True, default=dict, editable=False),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "base_manager_name": "objects",
                "default_manager_name": "objects",
                "ordering": ("display_name", "id"),
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(slot_id__regex=r"^[a-z][a-z0-9-]{0,62}$"),
                        name="roots_root_slot_id_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(display_name__regex=r"\S"),
                        name="roots_root_display_name_nonblank",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(mode__in=("read_only", "read_write")),
                        name="roots_root_mode_valid",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(authorization_epoch__gte=0),
                        name="roots_root_authorization_epoch_nonnegative",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="RootGrant",
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
                    "permissions",
                    models.SmallIntegerField(
                        default=0,
                        validators=(aegis_apps.roots.models.validate_permissions,),
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "group",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="root_grants",
                        to="auth.group",
                    ),
                ),
                (
                    "root",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="grants",
                        to="roots.root",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="root_grants",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "base_manager_name": "objects",
                "default_manager_name": "objects",
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            models.Q(user__isnull=False, group__isnull=True)
                            | models.Q(user__isnull=True, group__isnull=False)
                        ),
                        name="roots_grant_exactly_one_principal",
                    ),
                    models.UniqueConstraint(
                        fields=("root", "user"),
                        condition=models.Q(user__isnull=False),
                        name="roots_grant_root_user_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("root", "group"),
                        condition=models.Q(group__isnull=False),
                        name="roots_grant_root_group_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(permissions__gte=0, permissions__lte=255),
                        name="roots_grant_permissions_bounded",
                    ),
                ],
            },
        ),
    ]
