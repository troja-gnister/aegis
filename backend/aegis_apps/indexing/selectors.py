"""Authorized, stored-only index status reads."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from django.conf import settings
from django.db.models.functions import Now

from aegis_apps.catalog import authorization
from aegis_apps.identity.models import User
from aegis_apps.operations.enums import HeartbeatStatus
from aegis_apps.operations.models import WorkerHeartbeat
from aegis_apps.operations.selectors import current_schema_identity

from .models import IndexDeployment, RootIndexState, ScanRun
from .serializers import index_status_payload

_STATUS_FIELDS = (
    "status",
    "binding_epoch",
    "policy_epoch",
    "next_generation",
    "observed_entries",
    "completed_directories",
    "degraded_directories",
    "updated_at",
    "last_completed_at",
    "active_run_id",
    "active_run__root_id",
    "active_run__binding_epoch",
    "active_run__policy_epoch",
    "active_run__root_epoch",
    "active_run__manifest_identity",
    "active_run__state",
)


def _active_scan_is_current(
    row: dict[str, object],
    *,
    deployment: IndexDeployment,
    root_id: UUID,
    root_epoch: int,
) -> bool:
    run_id = row["active_run_id"]
    if not isinstance(run_id, UUID):
        return False
    status = row["status"]
    expected_run_state = (
        ScanRun.State.QUEUED if status == "queued" else ScanRun.State.RUNNING
    )
    expected = (
        root_id,
        deployment.epoch,
        deployment.epoch,
        root_epoch,
        deployment.manifest_identity,
        expected_run_state,
    )
    actual = (
        row["active_run__root_id"],
        row["active_run__binding_epoch"],
        row["active_run__policy_epoch"],
        row["active_run__root_epoch"],
        row["active_run__manifest_identity"],
        row["active_run__state"],
    )
    return actual == expected


def _compatible_indexer_is_fresh(deployment: IndexDeployment) -> bool:
    """Require a live indexer using this release, schema, and manifest."""
    freshness = timedelta(seconds=float(settings.AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS))
    return WorkerHeartbeat.objects.filter(
        role="indexer",
        last_seen_at__gte=Now() - freshness,
        last_seen_at__lte=Now(),
        status__in=(HeartbeatStatus.IDLE, HeartbeatStatus.RUNNING),
        release_id=settings.AEGIS_RELEASE_ID,
        schema_identity=current_schema_identity(),
        manifest_identity=deployment.manifest_identity,
        current_job_id__isnull=True,
    ).exists()


def index_status(user: User, root_id: UUID) -> dict[str, object]:
    """Return a BROWSE-authorized status without source enumeration."""
    with authorization.browse_context(user, root_id, "index-status") as context:
        deployment = IndexDeployment.objects.get(pk=1)
        row = RootIndexState.objects.filter(root_id=root_id).values(*_STATUS_FIELDS).first()
        if row is None:
            return index_status_payload(None, state="not_indexed")
        stored_state = str(row["status"])
        compatible = (
            row["binding_epoch"] == deployment.epoch
            and row["policy_epoch"] == deployment.epoch
        )
        if compatible and stored_state in ("queued", "scanning"):
            compatible = _active_scan_is_current(
                row,
                deployment=deployment,
                root_id=root_id,
                root_epoch=context.root_epoch,
            )
        if compatible and stored_state in ("queued", "scanning", "ready"):
            compatible = _compatible_indexer_is_fresh(deployment)
        state = stored_state if compatible else "unavailable"
        return index_status_payload(row, state=state)
