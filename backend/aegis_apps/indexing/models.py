from __future__ import annotations

import uuid
from typing import ClassVar

from aegisctl.mounts import MAX_SLOTS, SLOT_ID_RE
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.lookups import Exact, LessThanOrEqual
from django.utils import timezone

from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.common.middleware import REQUEST_ID
from aegis_apps.operations.models import UNCONFIGURED_MANIFEST_IDENTITY
from aegis_apps.roots.models import Root


class IndexDeployment(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    epoch = models.PositiveBigIntegerField(default=1, editable=False)
    manifest_identity = models.CharField(max_length=64)
    slot_ids = models.JSONField(default=list)
    interval_seconds = models.PositiveIntegerField()
    idle_timeout_seconds = models.PositiveSmallIntegerField()
    batch_records = models.PositiveSmallIntegerField()
    readers = models.PositiveSmallIntegerField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(condition=models.Q(id=1), name="indexing_deploy_singleton"),
            models.CheckConstraint(
                condition=models.Q(epoch__gte=1), name="indexing_deploy_epoch_positive"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(interval_seconds__gte=60, interval_seconds__lte=604800)
                    & models.Q(idle_timeout_seconds__gte=30, idle_timeout_seconds__lte=3600)
                    & models.Q(batch_records__gte=100, batch_records__lte=2000)
                    & models.Q(readers__gte=1, readers__lte=4)
                ),
                name="indexing_deploy_policy_bounded",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(manifest_identity=UNCONFIGURED_MANIFEST_IDENTITY)
                    | models.Q(manifest_identity__regex=r"^[0-9a-f]{64}$")
                ),
                name="indexing_deploy_manifest_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Exact(
                        models.Func(
                            "slot_ids",
                            function="jsonb_typeof",
                            output_field=models.CharField(),
                        ),
                        models.Value("array"),
                    )
                    & LessThanOrEqual(
                        models.Func(
                            "slot_ids",
                            function="jsonb_array_length",
                            output_field=models.IntegerField(),
                        ),
                        MAX_SLOTS,
                    )
                ),
                name="indexing_deploy_slots_bounded",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if (
            not isinstance(self.slot_ids, list)
            or len(self.slot_ids) > MAX_SLOTS
            or any(
                not isinstance(slot_id, str) or SLOT_ID_RE.fullmatch(slot_id) is None
                for slot_id in self.slot_ids
            )
            or self.slot_ids != sorted(set(self.slot_ids))
        ):
            raise ValidationError({"slot_ids": "invalid deployment slots"})


class ScanRun(models.Model):
    class State(models.TextChoices):
        QUEUED = "queued"
        RUNNING = "running"
        COMPLETE = "complete"
        DEGRADED = "degraded"
        FENCED = "fenced"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    root = models.ForeignKey(Root, on_delete=models.PROTECT, related_name="scan_runs")
    binding_epoch = models.PositiveBigIntegerField()
    policy_epoch = models.PositiveBigIntegerField()
    root_epoch = models.PositiveBigIntegerField()
    manifest_identity = models.CharField(max_length=64)
    generation = models.PositiveBigIntegerField()
    start_epoch = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16, choices=State.choices)
    started_at = models.DateTimeField(default=timezone.now)
    settled_at = models.DateTimeField(null=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("root", "id"), name="indexing_run_root_id_uniq"
            ),
            models.UniqueConstraint(
                fields=("root", "generation"), name="indexing_run_root_generation_uniq"
            ),
            models.UniqueConstraint(
                fields=("root",),
                condition=models.Q(state__in=("queued", "running")),
                name="indexing_one_active_run_per_root",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=("queued", "running", "complete", "degraded", "fenced")
                ),
                name="indexing_run_state_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(manifest_identity=UNCONFIGURED_MANIFEST_IDENTITY)
                    | models.Q(manifest_identity__regex=r"^[0-9a-f]{64}$")
                ),
                name="indexing_run_manifest_valid",
            ),
        ]


class RootIndexState(models.Model):
    class Status(models.TextChoices):
        NOT_INDEXED = "not_indexed"
        QUEUED = "queued"
        SCANNING = "scanning"
        READY = "ready"
        DEGRADED = "degraded"
        UNAVAILABLE = "unavailable"

    root = models.OneToOneField(
        Root,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="index_state",
    )
    binding_epoch = models.PositiveBigIntegerField()
    policy_epoch = models.PositiveBigIntegerField()
    reconciliation_epoch = models.PositiveBigIntegerField(default=0)
    next_generation = models.PositiveBigIntegerField(default=1)
    due_at = models.DateTimeField(db_index=True)
    active_run = models.OneToOneField(
        ScanRun,
        null=True,
        on_delete=models.PROTECT,
        related_name="active_for_root_state",
        db_constraint=False,
    )
    rescan_requested = models.BooleanField(default=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.NOT_INDEXED)
    observed_entries = models.PositiveBigIntegerField(default=0)
    completed_directories = models.PositiveBigIntegerField(default=0)
    degraded_directories = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    last_completed_at = models.DateTimeField(null=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=(
                        "not_indexed",
                        "queued",
                        "scanning",
                        "ready",
                        "degraded",
                        "unavailable",
                    )
                ),
                name="indexing_root_status_valid",
            ),
        ]


class DirectoryWork(models.Model):
    class State(models.TextChoices):
        PENDING = "pending"
        READING = "reading"
        FINALIZING = "finalizing"
        COMPLETE = "complete"
        DEGRADED = "degraded"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(ScanRun, on_delete=models.PROTECT, related_name="directory_work")
    directory = models.ForeignKey(
        CatalogEntry,
        on_delete=models.PROTECT,
        related_name="directory_work",
    )
    parent_revision = models.PositiveBigIntegerField()
    state = models.CharField(max_length=16, choices=State.choices, default=State.PENDING)
    attempt = models.PositiveIntegerField(default=0)
    lease_owner = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    available_at = models.DateTimeField(default=timezone.now)
    last_batch_sequence = models.PositiveIntegerField(default=0)
    last_batch_hash = models.CharField(max_length=64, null=True)
    observed_count = models.PositiveBigIntegerField(default=0)
    eof_identity = models.JSONField(null=True)
    error_code = models.CharField(max_length=64, null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("run", "directory"), name="indexing_work_run_directory_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=(
                        "pending",
                        "reading",
                        "finalizing",
                        "complete",
                        "degraded",
                    )
                ),
                name="indexing_work_state_valid",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("run", "state", "available_at", "id"),
                name="indexing_work_claim_idx",
            )
        ]


class ScanRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    root = models.ForeignKey(Root, on_delete=models.PROTECT, related_name="scan_requests")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="scan_requests",
    )
    client_request_id = models.CharField(max_length=64)
    run = models.ForeignKey(
        ScanRun,
        on_delete=models.PROTECT,
        related_name="requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("actor", "root", "created_at"), name="indexing_request_recent_idx",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("actor", "client_request_id"),
                name="indexing_request_actor_client_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(client_request_id__regex=r"^[A-Za-z0-9_-]{8,64}$"),
                name="indexing_request_id_valid",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if REQUEST_ID.fullmatch(self.client_request_id) is None:
            raise ValidationError({"client_request_id": "invalid scan request ID"})
