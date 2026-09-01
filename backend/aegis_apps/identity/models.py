import uuid
from typing import ClassVar

from django.contrib.auth.models import AbstractUser
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
