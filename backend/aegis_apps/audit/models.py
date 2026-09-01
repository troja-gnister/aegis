from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable
from contextvars import ContextVar
from typing import Any

from django.conf import settings
from django.db import models

_audit_write_allowed: ContextVar[bool] = ContextVar("aegis_audit_write_allowed", default=False)


def _immutable() -> PermissionError:
    return PermissionError("audit events are append-only")


class AuditEventQuerySet(models.QuerySet["AuditEvent"]):
    def create(self, **kwargs: Any) -> AuditEvent:
        del kwargs
        raise _immutable()

    def get_or_create(self, defaults: Any = None, **kwargs: Any) -> tuple[AuditEvent, bool]:
        del defaults, kwargs
        raise _immutable()

    def update_or_create(
        self, defaults: Any = None, create_defaults: Any = None, **kwargs: Any
    ) -> tuple[AuditEvent, bool]:
        del defaults, create_defaults, kwargs
        raise _immutable()

    def bulk_create(
        self,
        objs: Iterable[AuditEvent],
        batch_size: int | None = None,
        ignore_conflicts: bool = False,
        update_conflicts: bool = False,
        update_fields: Collection[str] | None = None,
        unique_fields: Collection[str] | None = None,
    ) -> list[AuditEvent]:
        del (
            objs,
            batch_size,
            ignore_conflicts,
            update_conflicts,
            update_fields,
            unique_fields,
        )
        raise _immutable()

    def bulk_update(
        self,
        objs: Iterable[AuditEvent],
        fields: Iterable[str],
        batch_size: int | None = None,
    ) -> int:
        del objs, fields, batch_size
        raise _immutable()

    def update(self, **kwargs: Any) -> int:
        del kwargs
        raise _immutable()

    def _update(self, values: Any) -> int:
        del values
        raise _immutable()

    def delete(self) -> tuple[int, dict[str, int]]:
        raise _immutable()

    def _raw_delete(self, using: str | None) -> int:
        del using
        raise _immutable()


AuditEventManager = models.Manager.from_queryset(AuditEventQuerySet)


class AuditEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    occurred_at = models.DateTimeField(auto_now_add=True, db_index=True)
    event_type = models.CharField(max_length=96, db_index=True)
    outcome = models.CharField(max_length=24)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        on_delete=models.PROTECT,
        related_name="audit_events",
    )
    request_id = models.CharField(max_length=64, db_index=True)
    root_id = models.UUIDField(null=True)
    object_id = models.UUIDField(null=True)
    metadata = models.JSONField(default=dict)

    objects = AuditEventManager()

    class Meta:
        ordering = ("-occurred_at", "-id")

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding or not _audit_write_allowed.get():
            raise _immutable()
        kwargs["force_insert"] = True
        super().save(*args, **kwargs)

    def save_base(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding or not _audit_write_allowed.get():
            raise _immutable()
        super().save_base(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        del args, kwargs
        raise _immutable()
