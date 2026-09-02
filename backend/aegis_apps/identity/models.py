import uuid
from typing import ClassVar

from django.contrib.auth.models import AbstractUser, Group
from django.db import models
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    authorization_epoch = models.PositiveBigIntegerField(default=0)

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=~models.Q(username=""),
                name="identity_user_username_nonempty",
            )
        ]


class GroupIdentity(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    group = models.OneToOneField(
        Group,
        on_delete=models.PROTECT,
        related_name="aegis_identity",
    )


class LoginThrottleBucket(models.Model):
    class Kind(models.TextChoices):
        ACCOUNT = "account"
        IP = "ip"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    key_digest = models.BinaryField(max_length=32)
    window_started_at = models.DateTimeField()
    failures = models.PositiveSmallIntegerField(default=0)
    blocked_until = models.DateTimeField(null=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("kind", "key_digest"),
                name="identity_throttle_key_uniq",
            ),
        ]
