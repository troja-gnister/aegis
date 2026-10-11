"""Closed vocabularies of the media data model (spec M1, M3, M4, M5).

Every enum here is CHECK-constrained in the database; adding a value needs a migration.
"""

from django.db import models


class MediaKind(models.TextChoices):
    PHOTO = "photo", "Photo"
    VIDEO = "video", "Video"


class StageState(models.TextChoices):
    """State of one background stage of an asset: probe, thumbnail or poster.

    The values are the API contract's thumbnail states. ``failed`` means the last
    attempt failed and the stage may be retried at ``next_attempt_at``;
    ``quarantined`` is terminal for the current fingerprint (poison input after
    bounded retries); ``unsupported`` is terminal and was decided by the probe.

    Retries are counted by ``MediaAsset.attempts``: the number of consecutive failed
    attempts for the current fingerprint. It is reset to 0 on every success and on every
    fingerprint change, and the worker quarantines the asset once it reaches its policy
    limit (3 failures, spec M2), well below the database bound
    ``MAX_ASSET_ATTEMPTS``. Claims are counted by ``attempt_token``, never by
    ``attempts``.
    """

    PENDING = "pending", "Pending"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"
    UNSUPPORTED = "unsupported", "Unsupported"
    QUARANTINED = "quarantined", "Quarantined"


class Lane(models.TextChoices):
    """Work lane. The interactive lane is always served before the background lane."""

    INTERACTIVE = "interactive", "Interactive"
    BACKGROUND = "background", "Background"


class SortSource(models.TextChoices):
    """Which timestamp the timeline sorts by; a fallback is never a capture date."""

    CAPTURED = "captured", "Captured"
    MODIFIED = "modified", "Modified"


class CapturedAtSource(models.TextChoices):
    EXIF = "exif", "EXIF"
    CONTAINER = "container", "Container"


class DerivativeKind(models.TextChoices):
    """Derivative kinds of the API contract.

    Photos have ``thumbnail`` and ``preview``. Videos have ``thumbnail`` (the poster
    frame at thumbnail size), ``poster`` (the poster frame at preview size) and
    ``transcode``.
    """

    THUMBNAIL = "thumbnail", "Thumbnail"
    PREVIEW = "preview", "Preview"
    POSTER = "poster", "Poster"
    TRANSCODE = "transcode", "Transcode"


class DerivativeState(models.TextChoices):
    """State of one derivative row, written only by the media worker.

    ``ready`` rows have a published file. ``pending`` and ``failed`` rows have none.
    ``stale`` rows belong to an older fingerprint, processor version or parameter set;
    their file, if any, is removed by eviction (row first, then file), never in place.
    """

    PENDING = "pending", "Pending"
    READY = "ready", "Ready"
    FAILED = "failed", "Failed"
    STALE = "stale", "Stale"


class DerivativeExtension(models.TextChoices):
    WEBP = "webp", "WebP"
    MP4 = "mp4", "MP4"


class MediaErrorCode(models.TextChoices):
    """Safe, non-identifying failure classes recorded on an asset.

    The first group mirrors the processor protocol's safe error codes so the worker can
    record them unchanged; the second group is decided by the worker itself.
    """

    UNSUPPORTED_FORMAT = "unsupported_format", "Unsupported format"
    TOO_LARGE = "too_large", "Too large"
    NO_VIDEO_STREAM = "no_video_stream", "No video stream"
    SOURCE_CHANGED = "source_changed", "Source changed"
    SOURCE_UNAVAILABLE = "source_unavailable", "Source unavailable"
    DECODE_FAILED = "decode_failed", "Decode failed"
    TOOL_FAILED = "tool_failed", "Tool failed"
    TOOL_MISSING = "tool_missing", "Tool missing"
    MEMORY_LIMIT = "memory_limit", "Memory limit"
    CPU_LIMIT = "cpu_limit", "CPU limit"
    OUTPUT_LIMIT = "output_limit", "Output limit"
    CRASHED = "crashed", "Crashed"
    INVALID_JOB = "invalid_job", "Invalid job"
    INVALID_RESULT = "invalid_result", "Invalid result"
    INTERRUPTED = "interrupted", "Interrupted"
    SPAWN_FAILED = "spawn_failed", "Spawn failed"
    TIMEOUT = "timeout", "Timeout"
    OUTPUT_INVALID = "output_invalid", "Output invalid"
    PUBLISH_FAILED = "publish_failed", "Publish failed"
    PROCESSOR_UNAVAILABLE = "processor_unavailable", "Processor unavailable"
