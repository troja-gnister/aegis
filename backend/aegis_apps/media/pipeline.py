"""The media pipeline's decisions: which job to run, with what limits, and what a result means.

Stages (M3): **probe**, then **thumbnail** (photos) or **poster** (videos: one job writes both
the thumbnail-size frame, the video's ``thumbnail``, and the preview-size ``poster``).
Interactive requests run **preview** (photos, 2048 px) and **transcode** (videos, MP4).

Results, for a stage held under a fenced lease (``leasing.py``):

- ``ok``: a probe records the metadata (``sorting.py`` decides the timeline key) and re-queues
  the asset at the tail for its next stage; a thumbnail or poster is published
  (``publish.py``). ``attempts`` returns to 0.
- ``unsupported``: the stage, and for a probe every later stage, becomes ``unsupported``.
- ``changed``: the entry is re-discovered at once (``discovery.rediscover_entry``). A new
  fingerprint resets the asset; a gone entry parks it; an identical fingerprint is a failure
  (``source_changed``) so a source the processor can never match cannot loop (Task 3 review,
  I3).
- ``failed`` / ``timeout``, and results the worker rejects (``output_invalid``): one more
  consecutive failure. Below :data:`QUARANTINE_AFTER` the stage is ``failed`` and retried at
  ``now + backoff`` (exponential, with jitter); at 3 the stage (for a probe, every stage) is
  ``quarantined`` and is never retried for this fingerprint. ``interrupted`` (the processor
  died with the job) counts too.
- Environmental failures (:data:`ENVIRONMENT_CODES`: the source volume, a missing tool, the
  sandbox, publishing or the processor itself) are not the file's fault: they never count
  toward quarantine. Each one increments the asset's own ``environment_failures``, and the
  retry waits ``30 s * 2**(n - 1)`` (jittered), capped at an hour, so an unreadable file settles
  at one attempt an hour instead of churning. The processor-wide ones
  (:data:`PROCESSOR_WIDE_CODES`) also trip the worker's circuit breaker (``worker.py``),
  which pauses all dispatching with a growing backoff.

``attempts`` is therefore exactly Task 2's definition: consecutive counted failures for the
current fingerprint, reset by every success, by ``unsupported`` and by every reset.
``environment_failures`` is reset by every recorded result that is not environmental and by
every reset or park.
"""

from __future__ import annotations

import math
import random
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from aegis_media_processor import protocol
from django.db import connection

from .config import MediaPolicy
from .enums import Lane, MediaErrorCode, MediaKind, SortSource, StageState
from .keys import derivative_key, params_digest
from .leasing import AssetLease, fenced
from .models import MAX_ENVIRONMENT_FAILURES, MediaAsset
from .sorting import captured_sort_at, modified_sort_at

QUARANTINE_AFTER: Final = 3
RETRY_BASE_SECONDS: Final = 60.0
RETRY_MAX_SECONDS: Final = 3_600.0
ENVIRONMENT_RETRY_BASE_SECONDS: Final = 30.0
ENVIRONMENT_RETRY_MAX_SECONDS: Final = 3_600.0
MAX_PATH_DEPTH: Final = protocol.MAX_COMPONENTS
# Codes the worker decides itself; every other MediaErrorCode is a processor code.
WORKER_ERROR_CODES: Final = frozenset({
    MediaErrorCode.OUTPUT_INVALID.value,
    MediaErrorCode.PUBLISH_FAILED.value,
    MediaErrorCode.PROCESSOR_UNAVAILABLE.value,
})
ENVIRONMENT_CODES: Final = frozenset({
    MediaErrorCode.SOURCE_UNAVAILABLE.value,
    MediaErrorCode.TOOL_MISSING.value,
    MediaErrorCode.SPAWN_FAILED.value,
    MediaErrorCode.PUBLISH_FAILED.value,
    MediaErrorCode.PROCESSOR_UNAVAILABLE.value,
})
# Environmental codes that mean the processor (or the worker's own volumes) cannot work at
# all, whatever the file: they trip the worker's circuit breaker. source_unavailable is
# per-file (permissions, a missing mount of one root) and only backs off that asset.
PROCESSOR_WIDE_CODES: Final = frozenset({
    MediaErrorCode.TOOL_MISSING.value,
    MediaErrorCode.SPAWN_FAILED.value,
    MediaErrorCode.PUBLISH_FAILED.value,
    MediaErrorCode.PROCESSOR_UNAVAILABLE.value,
})
# Interactive requests go back to the operation queue (retryable_failure, no failed
# derivative) for these; every other failure is the content's, and ends the request.
# interrupted is also what the processor reports for jobs it stops at its own shutdown.
INTERACTIVE_RETRY_CODES: Final = ENVIRONMENT_CODES | frozenset({
    MediaErrorCode.INTERRUPTED.value,
})
STAGE_OPERATIONS: Final = frozenset({"probe", "thumbnail", "poster"})
REQUEST_OPERATIONS: Final[Mapping[str, protocol.Op]] = {
    "preview": "preview", "transcode": "transcode",
}
# CPU seconds allowed per wall second: the decoders and ffmpeg run up to this many threads
# (Task 3: two for images and posters, four for transcodes).
_CPU_FACTOR: Final[Mapping[str, int]] = {
    "probe": 2, "thumbnail": 2, "preview": 2, "poster": 2, "transcode": 4,
}
_OUTPUT_BYTES: Final[Mapping[str, int]] = {
    "probe": 64 << 10, "thumbnail": 8 << 20, "preview": 64 << 20, "poster": 64 << 20,
}
TRANSCODE_BYTES_PER_SECOND: Final = 4 << 20
TRANSCODE_MIN_BYTES: Final = 256 << 20
TRANSCODE_MAX_BYTES: Final = 32 << 30
TRANSCODE_UNKNOWN_BYTES: Final = 8 << 30
_WORK_STATES: Final = (StageState.PENDING, StageState.FAILED)
_TERMINAL: Final = (StageState.UNSUPPORTED, StageState.QUARANTINED)


# -- jobs ---------------------------------------------------------------------------------


def next_operation(row: Mapping[str, Any]) -> protocol.Op | None:
    """The stage a leased asset needs next, or ``None`` when nothing is left."""
    if row["probe_state"] in _WORK_STATES:
        return "probe"
    if row["probe_state"] != StageState.READY:
        return None
    if row["kind"] == MediaKind.PHOTO and row["thumbnail_state"] in _WORK_STATES:
        return "thumbnail"
    if row["kind"] == MediaKind.VIDEO and (
        row["thumbnail_state"] in _WORK_STATES or row["poster_state"] in _WORK_STATES
    ):
        return "poster"
    return None


def output_bytes(op: str, duration_ms: int | None) -> int:
    """Each operation's bound on any file it writes (the processor's ``RLIMIT_FSIZE``)."""
    if op != "transcode":
        return _OUTPUT_BYTES[op]
    if duration_ms is None:
        return TRANSCODE_UNKNOWN_BYTES
    seconds = math.ceil(max(duration_ms, 0) / 1000)
    return min(TRANSCODE_MAX_BYTES, max(TRANSCODE_MIN_BYTES, seconds * TRANSCODE_BYTES_PER_SECOND))


def job_limits(op: str, policy: MediaPolicy, duration_ms: int | None) -> protocol.Limits:
    stage = policy.limits_for(op)
    wall = stage.timeout_seconds
    if op == "transcode":
        wall = protocol.transcode_wall_seconds(duration_ms, stage.timeout_seconds)
    wall_minimum, wall_maximum = protocol.LIMIT_BOUNDS["wall_seconds"]
    wall = min(max(wall, wall_minimum), wall_maximum)
    cpu_maximum = protocol.LIMIT_BOUNDS["cpu_seconds"][1]
    return protocol.Limits(
        wall_seconds=wall,
        cpu_seconds=min(cpu_maximum, wall * _CPU_FACTOR[op]),
        memory_bytes=stage.memory_bytes,
        output_bytes=output_bytes(op, duration_ms),
    )


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


def derivative_parameters(op: protocol.Op, params: protocol.Params) -> dict[str, object]:
    """The parameters hashed into a derivative key: the operation and its params (M1)."""
    document = protocol.params_document(params)
    return {"op": op, **{_camel(name): value for name, value in document.items()}}


@dataclass(frozen=True, slots=True)
class DerivativeIdentity:
    kind: str
    key: str
    params_digest: str
    extension: str


def derivative_identities(
    fingerprint: str, op: protocol.Op, params: protocol.Params,
) -> tuple[DerivativeIdentity, ...]:
    """The key of every derivative an operation writes for one source fingerprint."""
    digest = params_digest(derivative_parameters(op, params))
    return tuple(
        DerivativeIdentity(
            kind=spec.name,
            key=derivative_key(fingerprint, protocol.PROCESSOR_VERSION, spec.name, digest),
            params_digest=digest,
            extension=spec.format,
        )
        for spec in protocol.OP_OUTPUTS[op]
    )


def build_job(
    op: protocol.Op,
    *,
    kind: str,
    lane: str,
    slot: str,
    path: bytes,
    identity: tuple[int, int, int, int],
    policy: MediaPolicy,
    duration_ms: int | None = None,
) -> protocol.Job:
    """A validated processor job (raises ``ProtocolError``); its id is never reused."""
    device, inode, size, mtime_ns = identity
    job = protocol.Job(
        id=secrets.token_hex(16),
        op=op,
        kind="photo" if kind == MediaKind.PHOTO else "video",
        lane="interactive" if lane == Lane.INTERACTIVE else "background",
        input=protocol.SourceInput(
            slot=slot, path=path, device=device, inode=inode, size=size, mtime_ns=mtime_ns,
        ),
        params=protocol.DEFAULT_PARAMS[op],
        limits=job_limits(op, policy, duration_ms),
    )
    return protocol.check_job(job)


def source_path(root_id: uuid.UUID, entry_id: uuid.UUID) -> bytes | None:
    """The entry's raw path relative to its root, from granted catalog columns only.

    Every ancestor must be a present directory up to the root's anchor (an empty name with
    no parent), the entry a present file, with no cycle, at most ``MAX_PATH_DEPTH``
    components and ``protocol.MAX_PATH_BYTES`` bytes. Anything else is ``None``.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            """
            WITH RECURSIVE chain AS (
                SELECT id, source_parent_id, raw_name, kind, source_state, 0 AS depth
                  FROM catalog_catalogentry WHERE id = %s AND root_id = %s
                UNION ALL
                SELECT p.id, p.source_parent_id, p.raw_name, p.kind, p.source_state,
                       c.depth + 1
                  FROM catalog_catalogentry p JOIN chain c ON p.id = c.source_parent_id
                 WHERE p.root_id = %s AND c.depth <= %s
            ) CYCLE id SET cyclic USING visited
            SELECT raw_name, source_parent_id IS NULL, kind, source_state, cyclic
              FROM chain ORDER BY depth DESC
            """,
            [entry_id, root_id, root_id, MAX_PATH_DEPTH],
        )
        rows = cursor.fetchall()
    if len(rows) < 2 or len(rows) > MAX_PATH_DEPTH + 1 or any(row[4] for row in rows):
        return None
    anchor_name, anchor_is_top, anchor_kind, _, _ = rows[0]
    if not anchor_is_top or bytes(anchor_name) != b"" or anchor_kind != "directory":
        return None
    names: list[bytes] = []
    for index, (name, is_top, kind, state, _) in enumerate(rows[1:], start=1):
        expected_kind = "file" if index == len(rows) - 1 else "directory"
        raw = bytes(name)
        if is_top or kind != expected_kind or state != "present" or not raw:
            return None
        names.append(raw)
    path = b"/".join(names)
    try:
        protocol.split_relative_path(path)
    except protocol.ProtocolError:
        return None
    return path


# -- retries ------------------------------------------------------------------------------


def retry_delay(
    failures: int,
    *,
    base: float = RETRY_BASE_SECONDS,
    maximum: float = RETRY_MAX_SECONDS,
    uniform: Callable[[], float] = random.random,
) -> timedelta:
    """Exponential backoff with jitter: ``base * 2**(failures - 1)``, capped, times 0.5-1."""
    exponent = min(max(failures - 1, 0), 30)
    delay = min(maximum, base * (2**exponent))
    return timedelta(seconds=delay * (0.5 + 0.5 * uniform()))


def is_counted(code: str) -> bool:
    return code not in ENVIRONMENT_CODES


# -- recording stage results ---------------------------------------------------------------


def _stage_columns(op: str, kind: str, *, all_stages: bool) -> tuple[str, ...]:
    if op == "probe" and not all_stages:
        return ("probe_state",)
    if op == "probe":
        columns: tuple[str, ...] = ("probe_state", "thumbnail_state")
    elif op == "thumbnail":
        columns = ("thumbnail_state",)
    else:
        columns = ("thumbnail_state",)
    if kind == MediaKind.VIDEO and op in ("probe", "poster"):
        columns += ("poster_state",)
    return columns


def _closed_lease(now: datetime) -> dict[str, Any]:
    return {"lease_owner": None, "lease_expires_at": None, "updated_at": now}


def metadata_columns(
    metadata: protocol.Metadata, *, mtime_ns: Any,
) -> dict[str, Any]:
    """Asset columns for probe metadata; the sort key follows ``sorting.py``."""
    captured = (
        None if metadata.captured_at is None
        else captured_sort_at(metadata.captured_at, metadata.captured_at_source)
    )
    values: dict[str, Any] = {
        "width": metadata.width,
        "height": metadata.height,
        "orientation": metadata.orientation,
        "camera_make": metadata.camera_make,
        "camera_model": metadata.camera_model,
        "duration_ms": metadata.duration_ms,
        "video_codec": metadata.video_codec,
        "audio_codec": metadata.audio_codec,
        "container": metadata.container,
        "has_location": metadata.has_location,
    }
    if captured is None:
        values.update(
            captured_at=None, captured_at_source=None, sort_at=modified_sort_at(mtime_ns),
            sort_source=SortSource.MODIFIED,
        )
    else:
        values.update(
            captured_at=captured, captured_at_source=metadata.captured_at_source,
            sort_at=captured, sort_source=SortSource.CAPTURED,
        )
    return values


def record_probe(
    lease: AssetLease, metadata: protocol.Metadata, *, mtime_ns: Any, now: datetime,
) -> bool:
    """A successful probe: metadata, ``probe = ready``, back in the queue at its tail."""
    return MediaAsset.objects.filter(fenced(lease, now=now)).update(
        **metadata_columns(metadata, mtime_ns=mtime_ns),
        probe_state=StageState.READY,
        processor_version=protocol.PROCESSOR_VERSION,
        attempts=0,
        environment_failures=0,
        safe_error_code=None,
        next_attempt_at=now,
        **_closed_lease(now),
    ) == 1


def stage_ready_update(op: str, kind: str, *, now: datetime) -> dict[str, Any]:
    """The asset columns a published thumbnail or poster sets (used inside the publish)."""
    values: dict[str, Any] = dict.fromkeys(
        _stage_columns(op, kind, all_stages=False), StageState.READY,
    )
    values.update(
        attempts=0, environment_failures=0, safe_error_code=None, lane=Lane.BACKGROUND,
        next_attempt_at=now, **_closed_lease(now),
    )
    return values


def record_unsupported(
    lease: AssetLease, op: str, kind: str, code: str, *, now: datetime,
) -> bool:
    values: dict[str, Any] = dict.fromkeys(
        _stage_columns(op, kind, all_stages=True), StageState.UNSUPPORTED,
    )
    if op == "probe":
        values["processor_version"] = protocol.PROCESSOR_VERSION
    return MediaAsset.objects.filter(fenced(lease, now=now)).update(
        **values,
        attempts=0,
        environment_failures=0,
        safe_error_code=code,
        lane=Lane.BACKGROUND,
        next_attempt_at=now,
        **_closed_lease(now),
    ) == 1


def record_failure(
    lease: AssetLease,
    op: str,
    kind: str,
    code: str,
    *,
    attempts: int,
    environment_failures: int = 0,
    now: datetime,
    uniform: Callable[[], float] = random.random,
) -> str:
    """Record one failure; returns ``failed``, ``quarantined``, ``deferred`` or ``lost``.

    ``attempts`` and ``environment_failures`` are the values the lease was claimed with.
    """
    if not is_counted(code):
        failures = min(max(environment_failures, 0) + 1, MAX_ENVIRONMENT_FAILURES)
        delay = retry_delay(
            failures, base=ENVIRONMENT_RETRY_BASE_SECONDS,
            maximum=ENVIRONMENT_RETRY_MAX_SECONDS, uniform=uniform,
        )
        updated = MediaAsset.objects.filter(fenced(lease, now=now)).update(
            environment_failures=failures, safe_error_code=code, next_attempt_at=now + delay,
            **_closed_lease(now),
        )
        return "deferred" if updated == 1 else "lost"
    failures = attempts + 1
    if failures >= QUARANTINE_AFTER:
        values: dict[str, Any] = dict.fromkeys(
            _stage_columns(op, kind, all_stages=True), StageState.QUARANTINED,
        )
        outcome = "quarantined"
        next_attempt_at = now
    else:
        values = dict.fromkeys(_stage_columns(op, kind, all_stages=False), StageState.FAILED)
        outcome = "failed"
        next_attempt_at = now + retry_delay(failures, uniform=uniform)
    updated = MediaAsset.objects.filter(fenced(lease, now=now)).update(
        **values,
        attempts=failures,
        environment_failures=0,
        safe_error_code=code,
        next_attempt_at=next_attempt_at,
        **_closed_lease(now),
    )
    return outcome if updated == 1 else "lost"


def terminal(states: Mapping[str, Any]) -> bool:
    """Whether an asset as a whole cannot be processed (blocks interactive requests too)."""
    if states.get("probe_state") in _TERMINAL:
        return True
    return StageState.QUARANTINED in (states.get("thumbnail_state"), states.get("poster_state"))
