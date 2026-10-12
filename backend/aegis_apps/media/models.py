"""Media assets and derivatives for read-only roots (spec M1, M3, M5).

Only the media worker writes these tables. The web role reads them (entry lists join
``MediaAsset`` by entry; the timeline pages ``media_asset_timeline_idx``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar, Final

from django.db import models
from django.utils import timezone

from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.roots.models import Root

from .enums import (
    CapturedAtSource,
    DerivativeExtension,
    DerivativeKind,
    DerivativeState,
    Lane,
    MediaErrorCode,
    MediaKind,
    SortSource,
    StageState,
)
from .formats import EVICTABLE_DERIVATIVE_KINDS
from .keys import HEX_DIGEST_PATTERN, PROCESSOR_VERSION_PATTERN, UINT64_MAX

# The database bound on MediaAsset.attempts, the count of consecutive failed attempts for
# the current fingerprint. The worker's quarantine threshold (3, spec M2) is far below it.
MAX_ASSET_ATTEMPTS: Final = 16
# The database bound on MediaAsset.environment_failures, the count of consecutive failures
# that were not the file's fault (the source volume, the processor, the derivatives volume).
# The worker saturates it well below this bound; it only lengthens the retry backoff.
MAX_ENVIRONMENT_FAILURES: Final = 1_000
MAX_MEDIA_DIMENSION: Final = 1 << 20
MAX_DURATION_MS: Final = 100 * 24 * 60 * 60 * 1000
MAX_DERIVATIVE_BYTES: Final = 1 << 40

# Trimmed, printable, 1-64 characters (EXIF camera make and model). The processor's
# str.isprintable() and strip() are the real filter; this CHECK is a backstop whose
# [:cntrl:] and [:space:] classes are exact for ASCII but locale-dependent beyond it.
METADATA_TEXT_PATTERN: Final = (
    r"^[^[:cntrl:][:space:]]([^[:cntrl:]]{0,62}[^[:cntrl:][:space:]])?$"
)
# ffprobe codec names and RFC 6381 codec strings, such as "h264" or "avc1.640028".
CODEC_PATTERN: Final = r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$"
# ffprobe format names, which may be comma-separated, such as "mov,mp4,m4a,3gp,3g2,mj2".
CONTAINER_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._,-]{0,63}$"

_WORK_STATES: Final = (StageState.PENDING, StageState.FAILED)
_UNFAILED_STATES: Final = (StageState.PENDING, StageState.READY, StageState.UNSUPPORTED)
_TERMINAL_STATES: Final = (StageState.UNSUPPORTED, StageState.QUARANTINED)

# An asset needs worker attention while any of its stages is pending or failed (and
# not yet quarantined). Leasing queries must use this exact predicate so the planner
# can use the partial indexes media_asset_work_idx and media_asset_backfill_idx.
ASSET_NEEDS_WORK: Final = (
    models.Q(probe_state__in=_WORK_STATES)
    | models.Q(thumbnail_state__in=_WORK_STATES)
    | models.Q(poster_state__in=_WORK_STATES)
)
# A parked asset (its entry is gone or no longer a present media file) waits here, far beyond
# any real retry time, so the claim range never reaches it. See discovery.py.
PARKED_UNTIL: Final = datetime.fromisoformat("9999-01-01T00:00:00+00:00")
# The background assets a newest-first claim looks at (leasing.py): those that need work and
# are not parked. Queries must repeat this exact predicate for media_asset_backfill_idx.
BACKFILL_CANDIDATE: Final = (
    ASSET_NEEDS_WORK
    & models.Q(lane=Lane.BACKGROUND)
    & models.Q(next_attempt_at__lt=PARKED_UNTIL)
)


def _optional_in(field: str, values: Sequence[str]) -> models.Q:
    return models.Q(**{f"{field}__isnull": True}) | models.Q(**{f"{field}__in": values})


def _optional_regex(field: str, pattern: str) -> models.Q:
    return models.Q(**{f"{field}__isnull": True}) | models.Q(**{f"{field}__regex": pattern})


def _dimensions_valid() -> models.Q:
    # Both or neither. The IS NOT NULL terms matter: without them a NULL in one column
    # makes the second branch NULL rather than false, and a CHECK accepts NULL.
    return models.Q(width__isnull=True, height__isnull=True) | models.Q(
        width__isnull=False,
        height__isnull=False,
        width__gte=1,
        width__lte=MAX_MEDIA_DIMENSION,
        height__gte=1,
        height__lte=MAX_MEDIA_DIMENSION,
    )


class MediaAsset(models.Model):
    """One photo or video file of a read-only root, keyed by its catalog entry."""

    entry = models.OneToOneField(
        CatalogEntry,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="media_asset",
    )
    # The timeline indexes lead with root, so the foreign key needs no index of its own.
    root = models.ForeignKey(
        Root, on_delete=models.PROTECT, related_name="media_assets", db_index=False
    )
    kind = models.CharField(max_length=8, choices=MediaKind.choices)
    # Source fingerprint inputs (M1), copied from the catalog when discovered.
    device = models.DecimalField(max_digits=20, decimal_places=0)
    inode = models.DecimalField(max_digits=20, decimal_places=0)
    size = models.DecimalField(max_digits=20, decimal_places=0)
    mtime_ns = models.DecimalField(max_digits=30, decimal_places=0)
    fingerprint = models.CharField(max_length=64)
    # Background pipeline (M3): probe, then thumbnail (photos) or posters (videos).
    probe_state = models.CharField(
        max_length=16, choices=StageState.choices, default=StageState.PENDING
    )
    thumbnail_state = models.CharField(
        max_length=16, choices=StageState.choices, default=StageState.PENDING
    )
    poster_state = models.CharField(max_length=16, choices=StageState.choices, null=True)
    lane = models.CharField(max_length=16, choices=Lane.choices, default=Lane.BACKGROUND)
    # Fenced lease: every claim increments attempt_token; every worker write compares
    # entry, attempt_token and lease_owner, and requires an unexpired lease.
    # attempts counts consecutive failed attempts for the current fingerprint only: it is
    # reset to 0 on every success and on every fingerprint change, it is never incremented
    # by a claim or a re-lease, and the asset is quarantined at the worker's threshold
    # (3), long before the MAX_ASSET_ATTEMPTS CHECK could reject a write.
    attempts = models.PositiveSmallIntegerField(default=0)
    attempt_token = models.PositiveBigIntegerField(default=0)
    # Consecutive environmental failures (never the file's fault), kept apart from attempts:
    # they grow the retry backoff (capped at an hour) and never quarantine. Reset by every
    # recorded result and by every reset or park.
    environment_failures = models.PositiveSmallIntegerField(default=0, db_default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    lease_owner = models.UUIDField(null=True)
    lease_expires_at = models.DateTimeField(null=True)
    processor_version = models.CharField(max_length=64, null=True)
    safe_error_code = models.CharField(max_length=32, choices=MediaErrorCode.choices, null=True)
    # Probe metadata (M5). Coordinates are never stored; only has_location is.
    # width and height are display dimensions (EXIF orientation or video rotation already
    # applied); orientation is the EXIF value 1-8, kept for information.
    width = models.PositiveIntegerField(null=True)
    height = models.PositiveIntegerField(null=True)
    orientation = models.PositiveSmallIntegerField(null=True)
    captured_at = models.DateTimeField(null=True)
    captured_at_source = models.CharField(
        max_length=16, choices=CapturedAtSource.choices, null=True
    )
    camera_make = models.CharField(max_length=64, null=True)
    camera_model = models.CharField(max_length=64, null=True)
    duration_ms = models.PositiveBigIntegerField(null=True)
    video_codec = models.CharField(max_length=64, null=True)
    audio_codec = models.CharField(max_length=64, null=True)
    container = models.CharField(max_length=64, null=True)
    has_location = models.BooleanField(default=False)
    # Timeline key: captured_at when known, otherwise the modification time.
    sort_at = models.DateTimeField()
    sort_source = models.CharField(
        max_length=16, choices=SortSource.choices, default=SortSource.MODIFIED
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("root", "-sort_at", "-entry"), name="media_asset_timeline_idx"
            ),
            models.Index(
                fields=("root", "kind", "-sort_at", "-entry"),
                name="media_asset_timeline_kind_idx",
            ),
            models.Index(
                fields=("lane", "next_attempt_at", "entry"),
                condition=ASSET_NEEDS_WORK,
                name="media_asset_work_idx",
            ),
            # Newest files first: per root, the background assets that need work, by
            # modification time. Parked assets stay out, so they never lengthen the scan.
            models.Index(
                fields=("root", "-mtime_ns", "-entry"),
                condition=BACKFILL_CANDIDATE,
                name="media_asset_backfill_idx",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(kind__in=MediaKind.values), name="media_asset_kind_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(probe_state__in=StageState.values),
                name="media_asset_probe_state_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(thumbnail_state__in=StageState.values),
                name="media_asset_thumbnail_state_valid",
            ),
            models.CheckConstraint(
                condition=_optional_in("poster_state", StageState.values),
                name="media_asset_poster_state_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(lane__in=Lane.values), name="media_asset_lane_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(sort_source__in=SortSource.values),
                name="media_asset_sort_source_valid",
            ),
            models.CheckConstraint(
                condition=_optional_in("captured_at_source", CapturedAtSource.values),
                name="media_asset_captured_source_valid",
            ),
            models.CheckConstraint(
                condition=_optional_in("safe_error_code", MediaErrorCode.values),
                name="media_asset_error_code_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=HEX_DIGEST_PATTERN),
                name="media_asset_fingerprint_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    device__gte=0,
                    device__lte=UINT64_MAX,
                    inode__gte=0,
                    inode__lte=UINT64_MAX,
                    size__gte=0,
                    size__lte=UINT64_MAX,
                ),
                name="media_asset_identity_bounded",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind=MediaKind.PHOTO, poster_state__isnull=True)
                    | models.Q(kind=MediaKind.VIDEO, poster_state__isnull=False)
                ),
                name="media_asset_poster_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(kind=MediaKind.VIDEO)
                | models.Q(
                    duration_ms__isnull=True,
                    video_codec__isnull=True,
                    audio_codec__isnull=True,
                    container__isnull=True,
                ),
                name="media_asset_photo_metadata_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(probe_state=StageState.READY)
                | (
                    ~models.Q(thumbnail_state=StageState.READY)
                    & (
                        models.Q(poster_state__isnull=True)
                        | ~models.Q(poster_state=StageState.READY)
                    )
                ),
                name="media_asset_stage_order",
            ),
            # A probe that ends the pipeline ends every later stage too, so the asset
            # leaves media_asset_work_idx instead of being leased forever.
            models.CheckConstraint(
                condition=~models.Q(probe_state__in=_TERMINAL_STATES)
                | (
                    models.Q(thumbnail_state__in=_TERMINAL_STATES)
                    & _optional_in("poster_state", _TERMINAL_STATES)
                ),
                name="media_asset_terminal_probe_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(safe_error_code__isnull=False)
                | (
                    models.Q(probe_state__in=_UNFAILED_STATES)
                    & models.Q(thumbnail_state__in=_UNFAILED_STATES)
                    & _optional_in("poster_state", _UNFAILED_STATES)
                ),
                name="media_asset_failure_has_code",
            ),
            models.CheckConstraint(
                condition=models.Q(attempts__gte=0, attempts__lte=MAX_ASSET_ATTEMPTS),
                name="media_asset_attempts_bounded",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    environment_failures__gte=0,
                    environment_failures__lte=MAX_ENVIRONMENT_FAILURES,
                ),
                name="media_asset_environment_failures_bounded",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_token__gte=models.F("attempts")),
                name="media_asset_attempt_token_monotonic",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(lease_owner__isnull=True, lease_expires_at__isnull=True)
                    | models.Q(lease_owner__isnull=False, lease_expires_at__isnull=False)
                ),
                name="media_asset_lease_shape",
            ),
            models.CheckConstraint(
                condition=_optional_regex("processor_version", PROCESSOR_VERSION_PATTERN),
                name="media_asset_processor_version_valid",
            ),
            models.CheckConstraint(
                condition=_dimensions_valid(), name="media_asset_dimensions_valid"
            ),
            models.CheckConstraint(
                condition=models.Q(orientation__isnull=True)
                | models.Q(orientation__gte=1, orientation__lte=8),
                name="media_asset_orientation_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(captured_at__isnull=True, captured_at_source__isnull=True)
                    | models.Q(captured_at__isnull=False, captured_at_source__isnull=False)
                ),
                name="media_asset_captured_shape",
            ),
            models.CheckConstraint(
                condition=_optional_regex("camera_make", METADATA_TEXT_PATTERN),
                name="media_asset_camera_make_valid",
            ),
            models.CheckConstraint(
                condition=_optional_regex("camera_model", METADATA_TEXT_PATTERN),
                name="media_asset_camera_model_valid",
            ),
            models.CheckConstraint(
                condition=_optional_regex("video_codec", CODEC_PATTERN),
                name="media_asset_video_codec_valid",
            ),
            models.CheckConstraint(
                condition=_optional_regex("audio_codec", CODEC_PATTERN),
                name="media_asset_audio_codec_valid",
            ),
            models.CheckConstraint(
                condition=_optional_regex("container", CONTAINER_PATTERN),
                name="media_asset_container_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(duration_ms__isnull=True)
                | models.Q(duration_ms__gte=0, duration_ms__lte=MAX_DURATION_MS),
                name="media_asset_duration_bounded",
            ),
            models.CheckConstraint(
                condition=models.Q(sort_source=SortSource.MODIFIED)
                | models.Q(
                    sort_source=SortSource.CAPTURED,
                    captured_at__isnull=False,
                    sort_at=models.F("captured_at"),
                ),
                name="media_asset_sort_shape",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.entry_id}"


class MediaDerivative(models.Model):
    """One derivative file (or a failed or stale attempt at one) of an asset."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # media_derivative_identity_idx leads with asset, so the foreign key needs no index of
    # its own.
    asset = models.ForeignKey(
        MediaAsset, on_delete=models.PROTECT, related_name="derivatives", db_index=False
    )
    kind = models.CharField(max_length=16, choices=DerivativeKind.choices)
    # The asset fingerprint this derivative was built from. Delivery serves a derivative
    # only when it equals both the asset's current fingerprint and the URL's ``f``.
    fingerprint = models.CharField(max_length=64)
    params_digest = models.CharField(max_length=64)
    key = models.CharField(max_length=64)
    extension = models.CharField(max_length=8, choices=DerivativeExtension.choices)
    bytes = models.PositiveBigIntegerField(null=True)
    width = models.PositiveIntegerField(null=True)
    height = models.PositiveIntegerField(null=True)
    state = models.CharField(
        max_length=16, choices=DerivativeState.choices, default=DerivativeState.PENDING
    )
    evictable = models.BooleanField()
    created_at = models.DateTimeField(auto_now_add=True)
    last_access_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("asset", "kind", "fingerprint"), name="media_derivative_identity_idx"
            ),
            # INCLUDE (bytes) keeps the eviction budget sum index-only.
            models.Index(
                fields=("last_access_at", "id"),
                include=("bytes",),
                condition=models.Q(evictable=True, state=DerivativeState.READY),
                name="media_derivative_lru_idx",
            ),
            models.Index(
                fields=("created_at", "id"),
                condition=models.Q(state=DerivativeState.STALE),
                name="media_derivative_stale_idx",
            ),
        ]
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=("key",), name="media_derivative_key_uniq"),
            models.UniqueConstraint(
                fields=("asset", "kind"),
                condition=~models.Q(state=DerivativeState.STALE),
                name="media_derivative_one_current_per_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=DerivativeKind.values),
                name="media_derivative_kind_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=DerivativeState.values),
                name="media_derivative_state_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(extension__in=DerivativeExtension.values),
                name="media_derivative_extension_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind=DerivativeKind.TRANSCODE, extension=DerivativeExtension.MP4)
                    | models.Q(
                        kind__in=(
                            DerivativeKind.THUMBNAIL,
                            DerivativeKind.PREVIEW,
                            DerivativeKind.POSTER,
                        ),
                        extension=DerivativeExtension.WEBP,
                    )
                ),
                name="media_derivative_extension_matches_kind",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(evictable=True, kind__in=sorted(EVICTABLE_DERIVATIVE_KINDS))
                    | (
                        models.Q(evictable=False)
                        & ~models.Q(kind__in=sorted(EVICTABLE_DERIVATIVE_KINDS))
                    )
                ),
                name="media_derivative_evictable_matches_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(key__regex=HEX_DIGEST_PATTERN),
                name="media_derivative_key_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=HEX_DIGEST_PATTERN),
                name="media_derivative_fingerprint_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(params_digest__regex=HEX_DIGEST_PATTERN),
                name="media_derivative_params_digest_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(bytes__isnull=True)
                | models.Q(bytes__gte=1, bytes__lte=MAX_DERIVATIVE_BYTES),
                name="media_derivative_bytes_bounded",
            ),
            models.CheckConstraint(
                condition=_dimensions_valid(), name="media_derivative_dimensions_valid"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state=DerivativeState.READY,
                        bytes__isnull=False,
                        width__isnull=False,
                        height__isnull=False,
                    )
                    | models.Q(
                        state__in=(DerivativeState.PENDING, DerivativeState.FAILED),
                        bytes__isnull=True,
                        width__isnull=True,
                        height__isnull=True,
                    )
                    | models.Q(state=DerivativeState.STALE)
                ),
                name="media_derivative_state_shape",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.kind} {self.key}"
