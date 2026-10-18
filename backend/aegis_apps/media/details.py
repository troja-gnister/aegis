"""``GET /api/v1/entries/{id}/media``: metadata, derivative states and delivery URLs."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from aegis_apps.catalog.authorization import CatalogNotFound, entry_context
from aegis_apps.catalog.domain import EntryKind
from aegis_apps.catalog.queries import media_links
from aegis_apps.identity.models import User
from aegis_apps.operations.enums import JobKind, JobState, WorkerRole
from aegis_apps.operations.models import Job

from . import selectors
from .enums import DerivativeKind, DerivativeState, MediaKind, StageState
from .formats import DERIVATIVE_KINDS_BY_MEDIA_KIND, original_content_type
from .links import (
    MediaLinks,
    derivative_url,
    download_url,
    fingerprint_token,
    original_url,
    rfc3339_utc,
)
from .playback import playback_type

ACTIVE_JOB_STATES: Final = (JobState.QUEUED, JobState.RUNNING, JobState.RETRY_WAIT)
REQUEST_JOB_KINDS: Final[Mapping[str, JobKind]] = {
    DerivativeKind.PREVIEW.value: JobKind.MEDIA_PREVIEW,
    DerivativeKind.TRANSCODE.value: JobKind.MEDIA_TRANSCODE,
}
_DERIVATIVE_BY_JOB_KIND: Final = {job: kind for kind, job in REQUEST_JOB_KINDS.items()}
# Kinds whose state the background pipeline tracks on the asset itself.
_STAGE_FIELDS: Final = {
    DerivativeKind.THUMBNAIL.value: "thumbnail_state",
    DerivativeKind.POSTER.value: "poster_state",
}
_TERMINAL: Final = (StageState.UNSUPPORTED, StageState.QUARANTINED)


def asset_block(asset: Mapping[str, Any]) -> str | None:
    """``unsupported`` or ``quarantined`` when the asset as a whole cannot be processed."""
    probe = asset["probe_state"]
    if probe in _TERMINAL:
        return str(probe)
    if StageState.QUARANTINED in (asset["thumbnail_state"], asset["poster_state"]):
        return StageState.QUARANTINED.value
    return None


def pending_request_kinds(
    actor_id: UUID, entry_id: UUID, fingerprint: str,
) -> frozenset[str]:
    """Derivative kinds this actor already has an active request job for."""
    kinds = Job.objects.filter(
        target_role=WorkerRole.MEDIA,
        kind__in=tuple(REQUEST_JOB_KINDS.values()),
        state__in=ACTIVE_JOB_STATES,
        operation__actor_id=actor_id,
        payload__entryId=str(entry_id),
        payload__fingerprint=fingerprint,
    ).values_list("kind", flat=True)
    return frozenset(_DERIVATIVE_BY_JOB_KIND[JobKind(kind)] for kind in kinds)


def derivative_state(
    kind: str,
    asset: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
    pending: Collection[str],
) -> str:
    """The contract state of one derivative kind of the asset's current fingerprint."""
    row = rows.get(kind)
    if row is not None and row["state"] == DerivativeState.READY:
        return "ready"
    stage_field = _STAGE_FIELDS.get(kind)
    if stage_field is not None:
        stage = asset[stage_field]
        # A stage marked ready without a current ready row has nothing to deliver.
        return "missing" if stage in (None, StageState.READY) else str(stage)
    block = asset_block(asset)
    if block is not None:
        return block
    if kind in pending or (row is not None and row["state"] == DerivativeState.PENDING):
        return "pending"
    if row is not None and row["state"] == DerivativeState.FAILED:
        return "failed"
    return "missing"


def _metadata(asset: Mapping[str, Any]) -> dict[str, object]:
    captured = asset["captured_at"]
    return {
        "width": asset["width"],
        "height": asset["height"],
        "orientation": asset["orientation"],
        "capturedAt": rfc3339_utc(captured) if isinstance(captured, datetime) else None,
        "capturedAtSource": asset["captured_at_source"] if captured is not None else None,
        "cameraMake": asset["camera_make"],
        "cameraModel": asset["camera_model"],
        "durationMs": asset["duration_ms"],
        "videoCodec": asset["video_codec"],
        "audioCodec": asset["audio_codec"],
        "container": asset["container"],
        "hasLocation": bool(asset["has_location"]),
    }


def details_payload(
    *,
    entry: selectors.EntryFile,
    asset: Mapping[str, Any],
    rows: Mapping[str, Mapping[str, Any]],
    pending: Collection[str],
    links: MediaLinks,
) -> dict[str, object]:
    entry_id = entry.entry_id
    media_kind = str(asset["kind"])
    if media_kind not in MediaKind.values:
        raise ValueError("invalid media kind")
    fingerprint = str(asset["fingerprint"])
    # The catalog observed a different file than media discovery did: every derivative
    # shows the old bytes and is refused at delivery until rediscovery refreshes it.
    stale = entry.source_fingerprint != fingerprint
    derivatives: dict[str, dict[str, object]] = {}
    for kind in DERIVATIVE_KINDS_BY_MEDIA_KIND[media_kind]:
        state = "pending" if stale else derivative_state(kind, asset, rows, pending)
        url = None
        if state == "ready" and links.preview and entry.available:
            url = derivative_url(
                entry_id, kind, fingerprint,
                namespace=links.namespace, root_epoch=links.root_epoch,
            )
        derivatives[kind] = {"state": state, "url": url}
    inline_type = original_content_type(entry.type_hint, inline=True)
    inline_url = None
    if links.preview and entry.available and inline_type is not None:
        inline_url = original_url(
            entry_id, "inline", namespace=links.namespace, root_epoch=links.root_epoch,
        )
    playback = None
    if media_kind == MediaKind.VIDEO and inline_url is not None:
        playback = playback_type(
            type_hint=entry.type_hint,
            container=asset["container"],
            video_codec=asset["video_codec"],
            audio_codec=asset["audio_codec"],
            width=asset["width"],
            height=asset["height"],
        )
    return {
        "entryId": str(entry_id),
        "kind": media_kind,
        # The keyed token, never the fingerprint (which would let a client recover the
        # file's device and inode offline); derivative URLs carry the same token.
        "fingerprint": fingerprint_token(fingerprint),
        "metadata": _metadata(asset),
        "derivatives": derivatives,
        "original": {
            "contentType": original_content_type(entry.type_hint, inline=False),
            "inlineUrl": inline_url,
            # The same URL entry details issue for every downloadable file.
            "downloadUrl": download_url(
                entry_id, kind=EntryKind.FILE, available=entry.available, links=links,
            ),
            "playbackType": playback,
        },
        "permissions": {"preview": links.preview, "export": links.export},
    }


def media_details(user: User, entry_id: UUID, namespace: str) -> dict[str, object]:
    """Details of a media entry the user may browse; 404 for anything else."""
    with entry_context(user, entry_id, namespace) as context:
        entry = selectors.entry_file(context, entry_id)
        asset = None if entry is None else selectors.media_asset(context, entry_id)
        if entry is None or asset is None:
            raise CatalogNotFound()
        fingerprint = str(asset["fingerprint"])
        rows = selectors.current_derivatives(context, entry_id, fingerprint)
        pending = pending_request_kinds(user.pk, entry_id, fingerprint)
        return details_payload(
            entry=entry, asset=asset, rows=rows, pending=pending, links=media_links(context),
        )
