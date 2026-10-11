"""Interactive derivative requests (spec M3): actor-bound jobs for the media worker.

A request is a ``media.preview`` or ``media.transcode`` operation whose single job targets
the media role. Its intent requires BROWSE and PREVIEW on the entry's root and its
authorization snapshot is captured under the foundation locks, so the worker's re-check
(``validate_authorization_snapshot``) refuses the job after any revocation. The job
payload (``operations.serializers.normalize_media_request_payload``) names the entry and
the source fingerprint the derivative must be built from.

Requests are idempotent per (actor, entry, kind, fingerprint): a ready derivative or an
active request job of the same actor is reported instead of queueing another job. Volume
is bounded (``request_policy``): an actor may hold only a few open requests of each kind
(429), and after failed requests for the same entry, kind and fingerprint a new one is
refused for a doubling backoff (409). Every lookup is served by
``operations_job_media_req_idx`` (kind, state, payload entry) over open and failed media
jobs only.

The root is locked FOR SHARE, which still excludes every grant and root change while the
snapshot is captured but never blocks browsing readers. The actor's row is locked FOR
UPDATE, which serializes the actor's own checks, cap accounting and insert, so concurrent
requests of one actor can neither both insert nor exceed the cap.
"""

from __future__ import annotations

import math
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from django.conf import settings
from django.db import models
from django.db.models import Count, Max
from django.db.models.expressions import RawSQL
from django.utils import timezone

from aegis_apps.catalog.authorization import (
    CatalogAuthenticationRequired,
    CatalogNotFound,
    bounded_read,
    effective_mask,
    reject_not_found,
    require_current_binding,
)
from aegis_apps.catalog.domain import EntryKind, SourceState
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.operations.enums import JobKind, JobState, SafeErrorCode, WorkerRole
from aegis_apps.operations.models import Job
from aegis_apps.operations.serializers import (
    MEDIA_REQUEST_PAYLOAD_VERSION,
    MEDIA_REQUEST_PERMISSIONS,
    normalize_media_request_payload,
)
from aegis_apps.operations.services import create_operation
from aegis_apps.roots.locking import AuthorizationLocks
from aegis_apps.roots.manifest import ManifestError, configured_manifest
from aegis_apps.roots.selectors import authorized_roots

from .details import ACTIVE_JOB_STATES, asset_block
from .enums import DerivativeKind, DerivativeState, MediaKind
from .models import MediaAsset, MediaDerivative
from .request_policy import CAP_RETRY_AFTER_SECONDS, MediaRequestPolicy
from .selectors import availability_sql

type RequestableKind = Literal["preview", "transcode"]
type RequestState = Literal["pending", "ready"]

# Requestable derivative kind -> (job kind, the media kind it applies to).
REQUESTABLE: Final[Mapping[str, tuple[JobKind, MediaKind]]] = MappingProxyType({
    DerivativeKind.PREVIEW.value: (JobKind.MEDIA_PREVIEW, MediaKind.PHOTO),
    DerivativeKind.TRANSCODE.value: (JobKind.MEDIA_TRANSCODE, MediaKind.VIDEO),
})
REQUESTED_DERIVATIVE: Final[Mapping[JobKind, str]] = MappingProxyType({
    JobKind.MEDIA_PREVIEW: DerivativeKind.PREVIEW.value,
    JobKind.MEDIA_TRANSCODE: DerivativeKind.TRANSCODE.value,
})
REQUEST_ID_PREFIX: Final = "media_"


class MediaRequestUnsupported(ValueError):
    """The asset is unsupported or quarantined, or the kind does not apply to it."""

    def __init__(self) -> None:
        super().__init__("media_unsupported")


class MediaRequestRefused(ValueError):
    """A bounded refusal that the client may retry after ``retry_after`` seconds."""

    def __init__(self, code: str, retry_after: int) -> None:
        super().__init__(code)
        self.retry_after = max(1, retry_after)


class MediaRequestLimited(MediaRequestRefused):
    """The actor already holds the maximum number of open requests of this kind (429)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("media_request_limit", retry_after)


class MediaRequestBackoff(MediaRequestRefused):
    """Requests for this entry, kind and fingerprint failed recently (409)."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("media_request_backoff", retry_after)


@dataclass(frozen=True, slots=True)
class MediaRequestOutcome:
    kind: RequestableKind
    state: RequestState
    operation_id: UUID | None


@dataclass(frozen=True, slots=True)
class MediaRequestJob:
    """A validated interactive request job, as the media worker consumes it."""

    job_id: UUID
    operation_id: UUID
    kind: RequestableKind
    root_id: UUID
    entry_id: UUID
    fingerprint: str


def parse_media_request_job(job: Job) -> MediaRequestJob:
    """Validate a claimed media request job against its operation (Task 4's input).

    The worker must still re-check the authorization snapshot, that the entry belongs to
    ``root_id``, and that the asset's current fingerprint equals ``fingerprint``.
    """
    if job.target_role != WorkerRole.MEDIA:
        raise ValueError("invalid media request job")
    derivative = next(
        (kind for kind, (job_kind, _) in REQUESTABLE.items() if job_kind == job.kind), None,
    )
    if derivative is None or job.operation.kind != job.kind:
        raise ValueError("invalid media request job")
    payload = normalize_media_request_payload(job.payload, intent=job.operation.intent)
    if payload != job.payload:
        raise ValueError("invalid media request job")
    return MediaRequestJob(
        job_id=job.pk,
        operation_id=job.operation_id,
        kind="preview" if derivative == DerivativeKind.PREVIEW else "transcode",
        root_id=UUID(str(payload["rootId"])),
        entry_id=UUID(str(payload["entryId"])),
        fingerprint=str(payload["fingerprint"]),
    )


def _candidate_root(user: User, entry_id: UUID, requested_epoch: int) -> UUID:
    try:
        manifest = configured_manifest()
    except ManifestError:
        reject_not_found(user, requested_epoch)
    roots = authorized_roots(
        user_id=user.pk, active_manifest_slot_ids=tuple(manifest.slots) if manifest else (),
    ).values("id")
    root_id = CatalogEntry.objects.filter(pk=entry_id, root_id__in=roots).values_list(
        "root_id", flat=True,
    ).first()
    if root_id is None:
        reject_not_found(user, requested_epoch)
    return root_id


def _entry_is_available(root_id: UUID, entry_id: UUID) -> bool:
    row = (
        CatalogEntry.objects.filter(pk=entry_id, root_id=root_id)
        .annotate(_current=RawSQL(
            availability_sql("catalog_catalogentry"), (), output_field=models.BooleanField(),
        ))
        .values("kind", "source_state", "_current")
        .first()
    )
    return (
        row is not None
        and row["kind"] == EntryKind.FILE
        and row["source_state"] == SourceState.PRESENT
        and row["_current"] is True
    )


def media_jobs(job_kind: JobKind) -> models.QuerySet[Job]:
    """Media-role request jobs of one kind (``operations_job_media_req_idx``)."""
    return Job.objects.filter(target_role=WorkerRole.MEDIA, kind=job_kind)


def entry_request_jobs(
    entry_id: UUID, job_kind: JobKind, fingerprint: str,
) -> models.QuerySet[Job]:
    """Request jobs of one kind for one entry and source fingerprint, by any actor."""
    return media_jobs(job_kind).filter(
        payload__entryId=str(entry_id), payload__fingerprint=fingerprint,
    )


def _has_active_request(
    actor_id: UUID, entry_id: UUID, job_kind: JobKind, fingerprint: str,
) -> bool:
    return entry_request_jobs(entry_id, job_kind, fingerprint).filter(
        state__in=ACTIVE_JOB_STATES, operation__actor_id=actor_id,
    ).exists()


def active_request_count(actor_id: UUID, job_kind: JobKind) -> int:
    """Open request jobs of one kind held by one actor (the cap's measure)."""
    return media_jobs(job_kind).filter(
        state__in=ACTIVE_JOB_STATES, operation__actor_id=actor_id,
    ).count()


def failure_backoff_remaining(
    entry_id: UUID,
    job_kind: JobKind,
    fingerprint: str,
    *,
    policy: MediaRequestPolicy,
    now: datetime,
) -> int:
    """Seconds until another request for this entry, kind and fingerprint is accepted.

    Failures are the terminally failed request jobs of any actor (a revoked
    authorization is not a failure of the file), plus a failed current derivative row.
    The wait doubles with every failure, from the policy's base to its maximum, counted
    from the latest failure.
    """
    jobs = entry_request_jobs(entry_id, job_kind, fingerprint).filter(
        state=JobState.FAILED,
    ).exclude(safe_error_code=SafeErrorCode.AUTHORIZATION_STALE).aggregate(
        count=Count("id"), latest=Max("updated_at"),
    )
    failures: int = jobs["count"]
    latest: datetime | None = jobs["latest"]
    derivative = MediaDerivative.objects.filter(
        asset_id=entry_id,
        kind=REQUESTED_DERIVATIVE[job_kind],
        fingerprint=fingerprint,
        state=DerivativeState.FAILED,
    ).values_list("created_at", flat=True).first()
    if derivative is not None:
        failures += 1
        latest = derivative if latest is None else max(latest, derivative)
    if failures == 0 or latest is None:
        return 0
    until = latest + timedelta(seconds=policy.backoff_for(failures))
    remaining = (until - now).total_seconds()
    return math.ceil(remaining) if remaining > 0 else 0


def request_derivative(
    user: User, entry_id: UUID, kind: str, *, now: datetime | None = None,
) -> MediaRequestOutcome:
    """Queue (or report) an interactive derivative of one entry for *user*."""
    selected = REQUESTABLE.get(kind) if isinstance(kind, str) else None
    if selected is None:
        raise ValueError("invalid media request")
    job_kind, media_kind = selected
    requestable: RequestableKind = "preview" if kind == DerivativeKind.PREVIEW else "transcode"
    policy: MediaRequestPolicy = settings.AEGIS_MEDIA_REQUEST_POLICY
    # Capture before waiting for locks. Never replace the request's identity epoch.
    requested_epoch = user.authorization_epoch
    with bounded_read():
        root_id = _candidate_root(user, entry_id, requested_epoch)
        locks = AuthorizationLocks()
        root = locks.roots((root_id,), shared=True).get(root_id)
        actor = locks.users((user.pk,)).get(user.pk)
        if actor is None or not actor.is_active or actor.authorization_epoch != requested_epoch:
            raise CatalogAuthenticationRequired()
        if root is None or not root.active:
            raise CatalogNotFound()
        require_current_binding(root.slot_id)
        if effective_mask(actor, root_id) & MEDIA_REQUEST_PERMISSIONS != (
            MEDIA_REQUEST_PERMISSIONS
        ):
            raise CatalogNotFound()
        if not _entry_is_available(root_id, entry_id):
            raise CatalogNotFound()
        asset = (
            MediaAsset.objects.filter(entry_id=entry_id, root_id=root_id)
            .values("kind", "fingerprint", "probe_state", "thumbnail_state", "poster_state")
            .first()
        )
        if asset is None:
            raise CatalogNotFound()
        if asset["kind"] != media_kind or asset_block(asset) is not None:
            raise MediaRequestUnsupported()
        fingerprint = str(asset["fingerprint"])
        ready = MediaDerivative.objects.filter(
            asset_id=entry_id,
            asset__fingerprint=fingerprint,
            kind=kind,
            fingerprint=fingerprint,
            state=DerivativeState.READY,
        ).exists()
        if ready:
            return MediaRequestOutcome(requestable, "ready", None)
        if _has_active_request(actor.pk, entry_id, job_kind, fingerprint):
            return MediaRequestOutcome(requestable, "pending", None)
        remaining = failure_backoff_remaining(
            entry_id, job_kind, fingerprint, policy=policy, now=now or timezone.now(),
        )
        if remaining > 0:
            raise MediaRequestBackoff(remaining)
        if active_request_count(actor.pk, job_kind) >= policy.active_limit(requestable):
            raise MediaRequestLimited(CAP_RETRY_AFTER_SECONDS)
        operation = create_operation(
            actor=actor,
            request_id=f"{REQUEST_ID_PREFIX}{secrets.token_hex(16)}",
            kind=job_kind,
            intent={"roots": [{"id": str(root_id), "permissions": MEDIA_REQUEST_PERMISSIONS}]},
            target_role=WorkerRole.MEDIA,
            payload={
                "v": MEDIA_REQUEST_PAYLOAD_VERSION,
                "rootId": str(root_id),
                "entryId": str(entry_id),
                "fingerprint": fingerprint,
            },
            shared_root_locks=True,
        )
        return MediaRequestOutcome(requestable, "pending", operation.pk)
