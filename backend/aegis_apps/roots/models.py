from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any, ClassVar

from aegisctl.mounts import SLOT_ID_RE
from django.conf import settings
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import models

from .manifest import ManifestError, configured_manifest
from .permissions import validate_permission_mask

_grant_delete_capability: ContextVar[object | None] = ContextVar(
    "aegis_root_grant_delete_capability", default=None
)


def _root_deletion_disabled() -> PermissionError:
    return PermissionError("root deletion is disabled; use a supported service")


def _grant_deletion_disabled() -> PermissionError:
    return PermissionError("root grant deletion requires the audited service")


def validate_slot_id(value: str) -> None:
    if not isinstance(value, str) or SLOT_ID_RE.fullmatch(value) is None:
        raise ValidationError("invalid root slot ID")


def validate_display_name(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("root display name must not be blank")


def validate_permissions(value: int) -> None:
    try:
        validate_permission_mask(value)
    except ValueError:
        raise ValidationError("invalid permission mask") from None


class RootQuerySet(models.QuerySet["Root"]):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise _root_deletion_disabled()


class Root(models.Model):
    class Mode(models.TextChoices):
        READ_ONLY = "read_only"
        READ_WRITE = "read_write"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slot_id = models.CharField(max_length=63, unique=True, validators=(validate_slot_id,))
    display_name = models.CharField(max_length=160, validators=(validate_display_name,))
    mode = models.CharField(max_length=16, choices=Mode.choices)
    active = models.BooleanField(default=False)
    authorization_epoch = models.PositiveBigIntegerField(default=0, editable=False)
    capabilities = models.JSONField(default=dict, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RootQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        ordering = ("display_name", "id")
        constraints: ClassVar[list[models.BaseConstraint]] = [
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
        ]

    def clean(self) -> None:
        super().clean()
        try:
            manifest = configured_manifest()
        except ManifestError:
            raise ValidationError({"slot_id": "mount manifest is unavailable"}) from None
        if manifest is None:
            raise ValidationError({"slot_id": "mount manifest is unconfigured"})
        slot = manifest.get(self.slot_id)
        if slot is None:
            raise ValidationError({"slot_id": "root slot is not in the mount manifest"})
        if self.mode == self.Mode.READ_WRITE and slot.mode != "read_write":
            raise ValidationError({"mode": "root mode exceeds the mount manifest mode"})

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        del args, kwargs
        raise _root_deletion_disabled()


class RootGrantQuerySet(models.QuerySet["RootGrant"]):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise _grant_deletion_disabled()


class RootGrant(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    root = models.ForeignKey(
        Root,
        on_delete=models.PROTECT,
        related_name="grants",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="root_grants",
    )
    group = models.ForeignKey(
        Group,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="root_grants",
    )
    permissions = models.SmallIntegerField(default=0, validators=(validate_permissions,))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = RootGrantQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
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
        ]

    def clean(self) -> None:
        super().clean()
        if (self.user_id is None) == (self.group_id is None):
            raise ValidationError("a root grant requires exactly one principal")
        try:
            validate_permission_mask(self.permissions)
        except ValueError:
            raise ValidationError({"permissions": "invalid permission mask"}) from None

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        if _grant_delete_capability.get() is not self:
            raise _grant_deletion_disabled()
        return super().delete(*args, **kwargs)
