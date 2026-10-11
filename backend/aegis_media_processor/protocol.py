"""The media processor's job and result documents: the single source of truth (M2, M4, M5).

The media worker and the processor import this module, and neither trusts the other's files.
Both sides validate every document on encode and on decode: strict UTF-8 JSON with exact key
sets, exact integer types (a bool is not an integer), bounded strings and sizes, no floats, no
duplicate keys and no unknown fields. Nothing here imports Django.

Scratch layout (one volume, shared only by the worker and the processor):

- ``inbox/<id>.json``: the worker writes each job atomically (a temporary name, then a link that
  never replaces an existing job; see :func:`write_job`);
- ``work/<id>/job.json``: the processor claims a job by this rename. The file stays the
  processor loop's: the decoding child may read it but not write it. The child writes its
  outputs and temporary files only in ``work/<id>/out/`` (Landlock enforces it);
- ``rejected/``: whatever the processor could not remove from ``work/``; it retries at startup;
- ``done/<id>/``: the processor builds a fresh directory with ``result.json`` (at most
  :data:`MAX_RESULT_BYTES`) and exactly the outputs that result lists, as regular files named
  ``<name>.<format>``, and publishes it by one rename. Nothing else, and never a symlink.

The worker must still treat every published result as untrusted: decode it against its own
in-memory job (never one read back from scratch), open each output by the name
:data:`OP_OUTPUTS` derives, check the descriptor (regular file, one link, size, signature) and
copy from it rather than renaming or linking the scratch file.

Probe metadata never carries coordinates: GPS and location data are reduced to
``has_location`` inside the decoding child, no numeric or location field exists for one, and
floats are refused everywhere.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Final, Literal, cast

PROTOCOL_VERSION: Final = 1
# Part of every derivative key (M1). Bump it whenever an operation's outputs change.
PROCESSOR_VERSION: Final = "aegis-media-processor/1"

DEFAULT_SCRATCH_DIR: Final = "/srv/aegis/scratch"
DEFAULT_ROOTS_DIR: Final = "/srv/aegis/roots"
INBOX: Final = "inbox"
WORK: Final = "work"
DONE: Final = "done"
JOB_SUFFIX: Final = ".json"
JOB_FILE: Final = "job.json"
RESULT_FILE: Final = "result.json"

MAX_JOB_BYTES: Final = 16 * 1024
MAX_RESULT_BYTES: Final = 64 * 1024
MAX_PATH_BYTES: Final = 4096
MAX_COMPONENT_BYTES: Final = 255
MAX_COMPONENTS: Final = 256
# Pillow's decompression-bomb limit for every decode (M4): larger images are "unsupported".
MAX_IMAGE_PIXELS: Final = 160_000_000
MAX_METADATA_DIMENSION: Final = 1 << 20
MAX_OUTPUT_DIMENSION: Final = 16_384
MAX_DURATION_MS: Final = 100 * 24 * 3600 * 1000
MAX_CAMERA_TEXT: Final = 64
MAX_OUTPUT_BYTES: Final = 64 << 30

JOB_ID_RE: Final = re.compile(r"[0-9a-f]{32}")
# The same slot grammar as the mount manifests (aegisctl.mounts.SLOT_ID_RE).
SLOT_ID_RE: Final = re.compile(r"[a-z][a-z0-9-]{0,62}")
_CODEC_RE: Final = re.compile(r"[a-z0-9_]{1,32}")
_CAPTURED_RE: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:\d{2})?", re.ASCII,
)
_MAX_INTEGER: Final = (1 << 63) - 1

Op = Literal["probe", "thumbnail", "preview", "poster", "transcode"]
MediaKind = Literal["photo", "video"]
Lane = Literal["interactive", "background"]
Status = Literal["ok", "unsupported", "changed", "failed", "timeout"]
OutputFormat = Literal["webp", "mp4"]
CapturedSource = Literal["exif", "container"]
Container = Literal["mp4", "mov", "3gp", "matroska", "webm", "avi", "mpegts"]

OPS: Final[tuple[Op, ...]] = ("probe", "thumbnail", "preview", "poster", "transcode")
MEDIA_KINDS: Final[tuple[MediaKind, ...]] = ("photo", "video")
LANES: Final[tuple[Lane, ...]] = ("interactive", "background")
STATUSES: Final[tuple[Status, ...]] = ("ok", "unsupported", "changed", "failed", "timeout")
CAPTURED_SOURCES: Final[tuple[CapturedSource, ...]] = ("exif", "container")
CONTAINERS: Final[tuple[Container, ...]] = (
    "mp4", "mov", "3gp", "matroska", "webm", "avi", "mpegts",
)

# M4 allowlists, keyed by the lowercase extension (the catalog's type_hint).
PHOTO_EXTENSIONS: Final = frozenset(
    {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "avif", "bmp", "tif", "tiff"}
)
VIDEO_EXTENSIONS: Final = frozenset(
    {"mp4", "m4v", "mov", "webm", "mkv", "avi", "3gp", "mts", "m2ts"}
)
OP_KINDS: Final[Mapping[Op, frozenset[MediaKind]]] = MappingProxyType({
    "probe": frozenset({"photo", "video"}),
    "thumbnail": frozenset({"photo"}),
    "preview": frozenset({"photo"}),
    "poster": frozenset({"video"}),
    "transcode": frozenset({"video"}),
})

# Safe error codes, by status. A code is the only failure detail that crosses the volume.
ERROR_CODES: Final[Mapping[Status, frozenset[str]]] = MappingProxyType({
    "ok": frozenset(),
    "unsupported": frozenset({"unsupported_format", "too_large", "no_video_stream"}),
    "changed": frozenset({"source_changed"}),
    "failed": frozenset({
        "source_unavailable", "decode_failed", "tool_failed", "tool_missing", "memory_limit",
        "cpu_limit", "output_limit", "crashed", "invalid_job", "invalid_result", "interrupted",
        "spawn_failed",
    }),
    "timeout": frozenset({"timeout"}),
})

CONTENT_TYPES: Final[Mapping[OutputFormat, str]] = MappingProxyType(
    {"webp": "image/webp", "mp4": "video/mp4"}
)


@dataclass(frozen=True, slots=True)
class OutputSpec:
    """One derivative an operation writes; ``name`` is the derivative kind."""

    name: str
    format: OutputFormat

    @property
    def filename(self) -> str:
        return f"{self.name}.{self.format}"

    @property
    def content_type(self) -> str:
        return CONTENT_TYPES[self.format]


# A video's thumbnail-size poster frame is its ``thumbnail`` derivative; the preview-size
# frame is its ``poster``.
OP_OUTPUTS: Final[Mapping[Op, tuple[OutputSpec, ...]]] = MappingProxyType({
    "probe": (),
    "thumbnail": (OutputSpec("thumbnail", "webp"),),
    "preview": (OutputSpec("preview", "webp"),),
    "poster": (OutputSpec("thumbnail", "webp"), OutputSpec("poster", "webp")),
    "transcode": (OutputSpec("transcode", "mp4"),),
})
_OUTPUT_FORMATS: Final[Mapping[str, OutputFormat]] = MappingProxyType({
    spec.name: spec.format for specs in OP_OUTPUTS.values() for spec in specs
})


class ProtocolError(ValueError):
    """A document is malformed. The message never carries document content."""

    def __init__(self) -> None:
        super().__init__("media_protocol_error")


@dataclass(frozen=True, slots=True)
class SourceInput:
    """The original, by root slot and raw relative path, plus its expected fingerprint."""

    slot: str
    path: bytes
    device: int
    inode: int
    size: int
    mtime_ns: int

    @property
    def components(self) -> tuple[bytes, ...]:
        return split_relative_path(self.path)


@dataclass(frozen=True, slots=True)
class ProbeParams:
    pass


@dataclass(frozen=True, slots=True)
class ImageParams:
    """A thumbnail or a preview: fit within ``max_edge`` (never enlarged), WebP ``quality``."""

    max_edge: int
    quality: int


@dataclass(frozen=True, slots=True)
class PosterParams:
    """A frame at ``frame_ms`` (or the first frame), at the thumbnail and poster sizes."""

    thumbnail_edge: int
    thumbnail_quality: int
    poster_edge: int
    poster_quality: int
    frame_ms: int


@dataclass(frozen=True, slots=True)
class TranscodeParams:
    """H.264 High and AAC in MP4: fit within ``long_edge`` by ``short_edge`` (1080p)."""

    long_edge: int
    short_edge: int
    audio_kbps: int


type Params = ProbeParams | ImageParams | PosterParams | TranscodeParams

# The M4 defaults. The worker hashes params_document(...) into derivative keys (M1).
PROBE_PARAMS: Final = ProbeParams()
THUMBNAIL_PARAMS: Final = ImageParams(max_edge=400, quality=75)
PREVIEW_PARAMS: Final = ImageParams(max_edge=2048, quality=82)
POSTER_PARAMS: Final = PosterParams(
    thumbnail_edge=400, thumbnail_quality=75, poster_edge=2048, poster_quality=82, frame_ms=1000,
)
TRANSCODE_PARAMS: Final = TranscodeParams(long_edge=1920, short_edge=1080, audio_kbps=160)
DEFAULT_PARAMS: Final[Mapping[Op, Params]] = MappingProxyType({
    "probe": PROBE_PARAMS,
    "thumbnail": THUMBNAIL_PARAMS,
    "preview": PREVIEW_PARAMS,
    "poster": POSTER_PARAMS,
    "transcode": TRANSCODE_PARAMS,
})
_PARAMS_TYPES: Final[Mapping[Op, type]] = MappingProxyType({
    "probe": ProbeParams,
    "thumbnail": ImageParams,
    "preview": ImageParams,
    "poster": PosterParams,
    "transcode": TranscodeParams,
})


@dataclass(frozen=True, slots=True)
class Limits:
    """Per-child bounds: wall clock, CPU, address space and the size of any written file."""

    wall_seconds: int
    cpu_seconds: int
    memory_bytes: int
    output_bytes: int


LIMIT_BOUNDS: Final[Mapping[str, tuple[int, int]]] = MappingProxyType({
    "wall_seconds": (1, 86_400),
    "cpu_seconds": (1, 86_400),
    "memory_bytes": (64 << 20, 16 << 30),
    "output_bytes": (64 << 10, MAX_OUTPUT_BYTES),
})


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    op: Op
    kind: MediaKind
    lane: Lane
    input: SourceInput
    params: Params
    limits: Limits


@dataclass(frozen=True, slots=True)
class Metadata:
    """Probe metadata (M5). ``width`` and ``height`` are the displayed dimensions.

    For photos, EXIF orientations 5 to 8 swap the stored width and height; for videos, a
    rotation of 90 or 270 degrees (stream side data or the legacy ``rotate`` tag) does.
    ``orientation`` is the EXIF orientation, for information only (``None`` for videos).
    ``captured_at`` is ``YYYY-MM-DDTHH:MM:SS`` with ``Z``, a ``+HH:MM`` offset, or neither
    when the source records local time without an offset.
    """

    width: int | None
    height: int | None
    orientation: int | None
    captured_at: str | None
    captured_at_source: CapturedSource | None
    camera_make: str | None
    camera_model: str | None
    duration_ms: int | None
    video_codec: str | None
    audio_codec: str | None
    container: Container | None
    has_location: bool


@dataclass(frozen=True, slots=True)
class Output:
    name: str
    format: OutputFormat
    bytes: int
    width: int
    height: int

    @property
    def filename(self) -> str:
        return f"{self.name}.{self.format}"


@dataclass(frozen=True, slots=True)
class Result:
    id: str
    status: Status
    safe_error_code: str | None
    metadata: Metadata | None
    outputs: tuple[Output, ...]


# -- Paths and names -------------------------------------------------------------------------


def split_relative_path(raw: object) -> tuple[bytes, ...]:
    """The components of a raw relative path; refuses absolute, empty, ``.``, ``..`` and NUL."""
    if type(raw) is not bytes or not 1 <= len(raw) <= MAX_PATH_BYTES:
        raise ProtocolError
    components = tuple(raw.split(b"/"))
    if len(components) > MAX_COMPONENTS:
        raise ProtocolError
    for component in components:
        if (not component or component in (b".", b"..") or b"\0" in component
                or len(component) > MAX_COMPONENT_BYTES):
            raise ProtocolError
    return components


def media_kind_for_path(raw: bytes) -> MediaKind | None:
    """The M4 kind of a raw path's lowercase extension, or ``None``."""
    name = raw.rsplit(b"/", 1)[-1]
    if b"." not in name:
        return None
    extension = name.rsplit(b".", 1)[1]
    if not extension.isascii():
        return None
    value = extension.decode("ascii").lower()
    if value in PHOTO_EXTENSIONS:
        return "photo"
    if value in VIDEO_EXTENSIONS:
        return "video"
    return None


def job_filename(job_id: str) -> str:
    if type(job_id) is not str or JOB_ID_RE.fullmatch(job_id) is None:
        raise ProtocolError
    return job_id + JOB_SUFFIX


def job_id_from_filename(name: str) -> str | None:
    """The job id of an inbox file name, or ``None`` for anything else (temporary files)."""
    if not name.endswith(JOB_SUFFIX):
        return None
    candidate = name[: -len(JOB_SUFFIX)]
    return candidate if JOB_ID_RE.fullmatch(candidate) else None


def output_magic_matches(output_format: OutputFormat, head: bytes) -> bool:
    """Whether a file's first 12 bytes carry its format's signature (RIFF/WEBP, MP4 ``ftyp``)."""
    if len(head) < 12:
        return False
    if output_format == "webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if output_format == "mp4":
        return head[4:8] == b"ftyp"
    return False


def params_document(params: Params) -> dict[str, int]:
    """The canonical params object, as encoded in jobs and hashed into derivative keys."""
    if type(params) is ProbeParams:
        return {}
    if type(params) is ImageParams:
        return {"max_edge": params.max_edge, "quality": params.quality}
    if type(params) is PosterParams:
        return {
            "thumbnail_edge": params.thumbnail_edge,
            "thumbnail_quality": params.thumbnail_quality,
            "poster_edge": params.poster_edge,
            "poster_quality": params.poster_quality,
            "frame_ms": params.frame_ms,
        }
    if type(params) is TranscodeParams:
        return {
            "long_edge": params.long_edge,
            "short_edge": params.short_edge,
            "audio_kbps": params.audio_kbps,
        }
    raise ProtocolError


def transcode_wall_seconds(duration_ms: int | None, cap_seconds: int) -> int:
    """A transcode's wall budget: proportional to the probed duration, capped.

    Three seconds per second of media plus a minute, at least two minutes; an unknown
    duration gets the cap.
    """
    cap = _integer(cap_seconds, *LIMIT_BOUNDS["wall_seconds"])
    if duration_ms is None:
        return cap
    duration = _integer(duration_ms, 0, MAX_DURATION_MS)
    return min(cap, max(120, math.ceil(duration * 3 / 1000) + 60))


def output_edge_bounds(job: Job) -> Mapping[str, tuple[int, int]]:
    """Per output name, the (long edge, short edge) bounds its dimensions must respect."""
    params = job.params
    if type(params) is ImageParams:
        return {OP_OUTPUTS[job.op][0].name: (params.max_edge, params.max_edge)}
    if type(params) is PosterParams:
        return {
            "thumbnail": (params.thumbnail_edge, params.thumbnail_edge),
            "poster": (params.poster_edge, params.poster_edge),
        }
    if type(params) is TranscodeParams:
        return {"transcode": (params.long_edge, params.short_edge)}
    return {}


# -- Field validation --------------------------------------------------------------------------


def _integer(value: object, minimum: int = -(1 << 63), maximum: int = _MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError
    return value


def _optional_integer(value: object, minimum: int, maximum: int) -> int | None:
    return None if value is None else _integer(value, minimum, maximum)


def _choice[T: str](value: object, choices: tuple[T, ...]) -> T:
    for choice in choices:
        if type(value) is str and value == choice:
            return choice
    raise ProtocolError


def _optional_choice[T: str](value: object, choices: tuple[T, ...]) -> T | None:
    return None if value is None else _choice(value, choices)


def _job_id(value: object) -> str:
    if type(value) is not str or JOB_ID_RE.fullmatch(value) is None:
        raise ProtocolError
    return value


def _camera_text(value: object) -> str | None:
    if value is None:
        return None
    if (type(value) is not str or not 1 <= len(value) <= MAX_CAMERA_TEXT
            or not value.isprintable() or value != value.strip()):
        raise ProtocolError
    return value


def _codec(value: object) -> str | None:
    if value is None:
        return None
    if type(value) is not str or _CODEC_RE.fullmatch(value) is None:
        raise ProtocolError
    return value


def captured_at_is_valid(value: object) -> bool:
    """Whether a value is a well-formed ``captured_at`` (see :class:`Metadata`)."""
    if type(value) is not str:
        return False
    match = _CAPTURED_RE.fullmatch(value)
    if match is None:
        return False
    year, month, day, hour, minute, second = (int(match[index]) for index in range(1, 7))
    try:
        datetime(year, month, day, hour, minute, second)
    except ValueError:
        return False
    offset = match[7]
    if offset and offset != "Z":
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        if minutes >= 60 or timedelta(hours=hours, minutes=minutes) > timedelta(hours=18):
            return False
    return 1800 <= year <= 2200


def _check_source(source: object) -> SourceInput:
    if type(source) is not SourceInput:
        raise ProtocolError
    if type(source.slot) is not str or SLOT_ID_RE.fullmatch(source.slot) is None:
        raise ProtocolError
    split_relative_path(source.path)
    _integer(source.device, 0, (1 << 64) - 1)
    _integer(source.inode, 1, (1 << 64) - 1)
    _integer(source.size, 0)
    _integer(source.mtime_ns)
    return source


def _check_params(op: Op, params: object) -> Params:
    if type(params) is not _PARAMS_TYPES[op]:
        raise ProtocolError
    if type(params) is ImageParams:
        _integer(params.max_edge, 16, 4096)
        _integer(params.quality, 1, 100)
    elif type(params) is PosterParams:
        _integer(params.thumbnail_edge, 16, 4096)
        _integer(params.thumbnail_quality, 1, 100)
        _integer(params.poster_edge, 16, 4096)
        _integer(params.poster_quality, 1, 100)
        _integer(params.frame_ms, 0, 600_000)
    elif type(params) is TranscodeParams:
        _integer(params.long_edge, 144, 3840)
        _integer(params.short_edge, 144, 2160)
        if params.short_edge > params.long_edge:
            raise ProtocolError
        _integer(params.audio_kbps, 32, 320)
    return cast(Params, params)


def _check_limits(limits: object) -> Limits:
    if type(limits) is not Limits:
        raise ProtocolError
    for name, (minimum, maximum) in LIMIT_BOUNDS.items():
        _integer(getattr(limits, name), minimum, maximum)
    return limits


def check_job(job: object) -> Job:
    """Validate a job object field by field; returns it unchanged or raises."""
    if type(job) is not Job:
        raise ProtocolError
    _job_id(job.id)
    op = _choice(job.op, OPS)
    kind = _choice(job.kind, MEDIA_KINDS)
    _choice(job.lane, LANES)
    source = _check_source(job.input)
    if kind not in OP_KINDS[op] or media_kind_for_path(source.path) != kind:
        raise ProtocolError
    _check_params(op, job.params)
    _check_limits(job.limits)
    return job


def check_metadata(metadata: object, kind: MediaKind | None = None) -> Metadata:
    if type(metadata) is not Metadata:
        raise ProtocolError
    _optional_integer(metadata.width, 1, MAX_METADATA_DIMENSION)
    _optional_integer(metadata.height, 1, MAX_METADATA_DIMENSION)
    if (metadata.width is None) != (metadata.height is None):
        raise ProtocolError
    _optional_integer(metadata.orientation, 1, 8)
    if metadata.captured_at is not None and not captured_at_is_valid(metadata.captured_at):
        raise ProtocolError
    source = _optional_choice(metadata.captured_at_source, CAPTURED_SOURCES)
    if (metadata.captured_at is None) != (source is None):
        raise ProtocolError
    _camera_text(metadata.camera_make)
    _camera_text(metadata.camera_model)
    _optional_integer(metadata.duration_ms, 0, MAX_DURATION_MS)
    _codec(metadata.video_codec)
    _codec(metadata.audio_codec)
    _optional_choice(metadata.container, CONTAINERS)
    if type(metadata.has_location) is not bool:
        raise ProtocolError
    if kind == "photo" and (
        source == "container" or metadata.duration_ms is not None
        or metadata.video_codec is not None or metadata.audio_codec is not None
        or metadata.container is not None
    ):
        raise ProtocolError
    if kind == "video" and (metadata.orientation is not None or source == "exif"):
        raise ProtocolError
    return metadata


def _check_output(output: object) -> Output:
    if type(output) is not Output:
        raise ProtocolError
    name = output.name
    if type(name) is not str or name not in _OUTPUT_FORMATS:
        raise ProtocolError
    if output.format != _OUTPUT_FORMATS[name]:
        raise ProtocolError
    _integer(output.bytes, 1, MAX_OUTPUT_BYTES)
    _integer(output.width, 1, MAX_OUTPUT_DIMENSION)
    _integer(output.height, 1, MAX_OUTPUT_DIMENSION)
    return output


def check_result(result: object, job: Job | None = None) -> Result:
    """Validate a result; with its job, also its id, outputs, sizes and dimensions."""
    if type(result) is not Result:
        raise ProtocolError
    _job_id(result.id)
    status = _choice(result.status, STATUSES)
    if type(result.outputs) is not tuple or len(result.outputs) > 2:
        raise ProtocolError
    outputs = tuple(_check_output(output) for output in result.outputs)
    if len({output.name for output in outputs}) != len(outputs):
        raise ProtocolError
    if status == "ok":
        if result.safe_error_code is not None:
            raise ProtocolError
    else:
        code = result.safe_error_code
        if type(code) is not str or code not in ERROR_CODES[status]:
            raise ProtocolError
        if result.metadata is not None or outputs:
            raise ProtocolError
    if result.metadata is not None:
        check_metadata(result.metadata, None if job is None else job.kind)
    if job is None:
        return result
    check_job(job)
    if result.id != job.id:
        raise ProtocolError
    if status != "ok":
        return result
    if (result.metadata is None) != (job.op != "probe"):
        raise ProtocolError
    if tuple(output.name for output in outputs) != tuple(
        spec.name for spec in OP_OUTPUTS[job.op]
    ):
        raise ProtocolError
    bounds = output_edge_bounds(job)
    for output in outputs:
        long_edge, short_edge = bounds[output.name]
        if output.bytes > job.limits.output_bytes:
            raise ProtocolError
        if max(output.width, output.height) > long_edge or min(
            output.width, output.height
        ) > short_edge:
            raise ProtocolError
    return result


# -- Encoding ----------------------------------------------------------------------------------


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, separators=(",", ":"), allow_nan=False, sort_keys=True,
    ).encode("ascii")


def encode_job(job: Job) -> bytes:
    check_job(job)
    source = job.input
    raw = _json_bytes({
        "version": PROTOCOL_VERSION,
        "id": job.id,
        "op": job.op,
        "kind": job.kind,
        "lane": job.lane,
        "input": {
            "slot": source.slot,
            "path": base64.b64encode(source.path).decode("ascii"),
            "device": source.device,
            "inode": source.inode,
            "size": source.size,
            "mtime_ns": source.mtime_ns,
        },
        "params": params_document(job.params),
        "limits": {name: getattr(job.limits, name) for name in LIMIT_BOUNDS},
    })
    if len(raw) > MAX_JOB_BYTES:
        raise ProtocolError
    return raw


def _metadata_document(metadata: Metadata) -> dict[str, object]:
    return {
        "width": metadata.width,
        "height": metadata.height,
        "orientation": metadata.orientation,
        "captured_at": metadata.captured_at,
        "captured_at_source": metadata.captured_at_source,
        "camera_make": metadata.camera_make,
        "camera_model": metadata.camera_model,
        "duration_ms": metadata.duration_ms,
        "video_codec": metadata.video_codec,
        "audio_codec": metadata.audio_codec,
        "container": metadata.container,
        "has_location": metadata.has_location,
    }


def encode_result(result: Result, job: Job | None = None) -> bytes:
    check_result(result, job)
    raw = _json_bytes({
        "version": PROTOCOL_VERSION,
        "id": result.id,
        "status": result.status,
        "safe_error_code": result.safe_error_code,
        "metadata": None if result.metadata is None else _metadata_document(result.metadata),
        "outputs": [
            {"name": output.name, "format": output.format, "bytes": output.bytes,
             "width": output.width, "height": output.height}
            for output in result.outputs
        ],
    })
    if len(raw) > MAX_RESULT_BYTES:
        raise ProtocolError
    return raw


# -- Decoding ----------------------------------------------------------------------------------

_METADATA_FIELDS: Final = frozenset({
    "width", "height", "orientation", "captured_at", "captured_at_source", "camera_make",
    "camera_model", "duration_ms", "video_codec", "audio_codec", "container", "has_location",
})


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError
        result[key] = value
    return result


def _parsed_int(raw: str) -> int:
    if len(raw) > 21:
        raise ProtocolError
    return int(raw)


def _refuse(raw: str) -> object:
    raise ProtocolError


def _load(raw: object, limit: int) -> dict[str, object]:
    if type(raw) is not bytes or not 2 <= len(raw) <= limit:
        raise ProtocolError
    try:
        value = json.loads(
            raw.decode("utf-8", "strict"), object_pairs_hook=_object, parse_int=_parsed_int,
            parse_float=_refuse, parse_constant=_refuse,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ProtocolError from None
    if not isinstance(value, dict):
        raise ProtocolError
    return cast(dict[str, object], value)


def _fields(value: object, fields: frozenset[str] | set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ProtocolError
    return cast(dict[str, object], value)


def _decode_path(value: object) -> bytes:
    if type(value) is not str or not 4 <= len(value) <= 4 * math.ceil(MAX_PATH_BYTES / 3) \
            or not value.isascii():
        raise ProtocolError
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise ProtocolError from None
    if base64.b64encode(decoded).decode("ascii") != value:
        raise ProtocolError
    split_relative_path(decoded)
    return decoded


def _decode_params(op: Op, value: object) -> Params:
    expected = _PARAMS_TYPES[op]
    if expected is ProbeParams:
        _fields(value, set())
        return ProbeParams()
    if expected is ImageParams:
        fields = _fields(value, {"max_edge", "quality"})
        return ImageParams(cast(int, fields["max_edge"]), cast(int, fields["quality"]))
    if expected is PosterParams:
        fields = _fields(value, {
            "thumbnail_edge", "thumbnail_quality", "poster_edge", "poster_quality", "frame_ms",
        })
        return PosterParams(
            cast(int, fields["thumbnail_edge"]), cast(int, fields["thumbnail_quality"]),
            cast(int, fields["poster_edge"]), cast(int, fields["poster_quality"]),
            cast(int, fields["frame_ms"]),
        )
    fields = _fields(value, {"long_edge", "short_edge", "audio_kbps"})
    return TranscodeParams(
        cast(int, fields["long_edge"]), cast(int, fields["short_edge"]),
        cast(int, fields["audio_kbps"]),
    )


def decode_job(raw: bytes) -> Job:
    document = _fields(_load(raw, MAX_JOB_BYTES), {
        "version", "id", "op", "kind", "lane", "input", "params", "limits",
    })
    if _integer(document["version"]) != PROTOCOL_VERSION:
        raise ProtocolError
    op = _choice(document["op"], OPS)
    source = _fields(document["input"], {"slot", "path", "device", "inode", "size", "mtime_ns"})
    limits = _fields(document["limits"], set(LIMIT_BOUNDS))
    job = Job(
        id=cast(str, document["id"]),
        op=op,
        kind=cast(MediaKind, document["kind"]),
        lane=cast(Lane, document["lane"]),
        input=SourceInput(
            slot=cast(str, source["slot"]),
            path=_decode_path(source["path"]),
            device=cast(int, source["device"]),
            inode=cast(int, source["inode"]),
            size=cast(int, source["size"]),
            mtime_ns=cast(int, source["mtime_ns"]),
        ),
        params=_decode_params(op, document["params"]),
        limits=Limits(**{name: cast(int, limits[name]) for name in LIMIT_BOUNDS}),
    )
    return check_job(job)


def _decode_metadata(value: object) -> Metadata | None:
    if value is None:
        return None
    fields = _fields(value, _METADATA_FIELDS)
    return Metadata(
        width=cast("int | None", fields["width"]),
        height=cast("int | None", fields["height"]),
        orientation=cast("int | None", fields["orientation"]),
        captured_at=cast("str | None", fields["captured_at"]),
        captured_at_source=cast("CapturedSource | None", fields["captured_at_source"]),
        camera_make=cast("str | None", fields["camera_make"]),
        camera_model=cast("str | None", fields["camera_model"]),
        duration_ms=cast("int | None", fields["duration_ms"]),
        video_codec=cast("str | None", fields["video_codec"]),
        audio_codec=cast("str | None", fields["audio_codec"]),
        container=cast("Container | None", fields["container"]),
        has_location=cast(bool, fields["has_location"]),
    )


def _decode_output(value: object) -> Output:
    fields = _fields(value, {"name", "format", "bytes", "width", "height"})
    return Output(
        name=cast(str, fields["name"]),
        format=cast(OutputFormat, fields["format"]),
        bytes=cast(int, fields["bytes"]),
        width=cast(int, fields["width"]),
        height=cast(int, fields["height"]),
    )


def decode_result(raw: bytes, job: Job | None = None) -> Result:
    """Decode and validate a result; pass its job to bind id, outputs and bounds."""
    document = _fields(_load(raw, MAX_RESULT_BYTES), {
        "version", "id", "status", "safe_error_code", "metadata", "outputs",
    })
    if _integer(document["version"]) != PROTOCOL_VERSION:
        raise ProtocolError
    outputs = document["outputs"]
    if not isinstance(outputs, list) or len(outputs) > 2:
        raise ProtocolError
    result = Result(
        id=cast(str, document["id"]),
        status=cast(Status, document["status"]),
        safe_error_code=cast("str | None", document["safe_error_code"]),
        metadata=_decode_metadata(document["metadata"]),
        outputs=tuple(_decode_output(output) for output in outputs),
    )
    return check_result(result, job)


# -- Files -------------------------------------------------------------------------------------

_READ_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOCTTY
_DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_CREATE_FLAGS: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NOCTTY
)


def read_regular_file(path: str, limit: int, *, dir_fd: int | None = None) -> bytes:
    """Read at most ``limit`` bytes of a regular file, never following a final symlink.

    Raises :class:`ProtocolError` for anything that is not a regular file or is too large,
    and ``OSError`` when the file cannot be opened.
    """
    descriptor = os.open(path, _READ_FLAGS, dir_fd=dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ProtocolError
        os.set_blocking(descriptor, True)
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(65_536, limit + 1 - len(data)))
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise ProtocolError
        return bytes(data)
    finally:
        os.close(descriptor)


def _write_temporary(directory_fd: int, name: str, data: bytes, mode: int) -> str:
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, _CREATE_FLAGS, mode, dir_fd=directory_fd)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(descriptor, view):]
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        os.unlink(temporary, dir_fd=directory_fd)
        raise
    os.close(descriptor)
    return temporary


def write_file_atomically(directory_fd: int, name: str, data: bytes, mode: int = 0o660) -> None:
    """Write ``name`` in a directory by temporary file and rename (replacing any old file)."""
    temporary = _write_temporary(directory_fd, name, data, mode)
    try:
        os.rename(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    except BaseException:
        os.unlink(temporary, dir_fd=directory_fd)
        raise


def write_job(inbox: str, job: Job) -> None:
    """Enqueue a job: written whole under a temporary name, then linked without replacing.

    Raises ``FileExistsError`` when a job with the same id is already queued.
    """
    data = encode_job(job)
    directory = os.open(inbox, _DIRECTORY_FLAGS)
    try:
        temporary = _write_temporary(directory, job_filename(job.id), data, 0o660)
        try:
            os.link(temporary, job_filename(job.id), src_dir_fd=directory,
                    dst_dir_fd=directory, follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=directory)
    finally:
        os.close(directory)
