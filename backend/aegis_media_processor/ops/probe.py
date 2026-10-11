"""Probe metadata (M5): dimensions as displayed, capture time, camera, codecs, location presence.

Photos are read with Pillow (header and EXIF only; no pixels are decoded). Videos are read with
``ffprobe -v error -print_format json -show_format -show_streams`` on the opened descriptor,
and its JSON is treated as untrusted. Location data is reduced to ``has_location`` here, in the
tool child: GPS tags and location keys are tested for presence and their values are never read
into the result.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Final, cast

from PIL import ExifTags

from ..protocol import (
    MAX_DURATION_MS,
    MAX_METADATA_DIMENSION,
    Container,
    Limits,
    Metadata,
    captured_at_is_valid,
)
from . import OperationError
from .image import open_image
from .video import ffprobe_document

_MAX_CAMERA_TEXT: Final = 64
_EXIF_DATETIME: Final = re.compile(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", re.ASCII)
_EXIF_OFFSET: Final = re.compile(r"[+-]\d{2}:\d{2}", re.ASCII)
_CONTAINER_DATETIME: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?"
    r"(Z|[+-]\d{2}:?\d{2})?", re.ASCII,
)
_DURATION: Final = re.compile(r"\d{1,12}(?:\.\d{1,9})?", re.ASCII)
_CODEC: Final = re.compile(r"[a-z0-9_]{1,32}", re.ASCII)
# QuickTime and Unix epoch zero: containers write these when the clock was never set.
_UNSET_TIMES: Final = frozenset({"1904-01-01T00:00:00", "1970-01-01T00:00:00"})
_GPS_POSITION_TAGS: Final = frozenset({1, 2, 3, 4})  # latitude and longitude, and their refs
_WEBM_VIDEO: Final = frozenset({"vp8", "vp9", "av1"})
_WEBM_AUDIO: Final = frozenset({"opus", "vorbis"})


def camera_text(value: object) -> str | None:
    """Printable, whitespace-collapsed text of at most 64 characters, or ``None``."""
    if isinstance(value, bytes):
        value = value.decode("latin-1")
    if not isinstance(value, str):
        return None
    printable = "".join(character for character in value if character.isprintable())
    text = " ".join(printable.split())[:_MAX_CAMERA_TEXT].strip()
    return text or None


def exif_captured_at(value: object, offset: object) -> str | None:
    """EXIF ``DateTimeOriginal`` (with ``OffsetTimeOriginal`` when valid) as ``captured_at``."""
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    if not isinstance(value, str):
        return None
    match = _EXIF_DATETIME.match(value.strip("\0 "))
    if match is None:
        return None
    local = "{}-{}-{}T{}:{}:{}".format(*match.groups())
    if isinstance(offset, bytes):
        offset = offset.decode("ascii", "replace")
    if isinstance(offset, str) and _EXIF_OFFSET.fullmatch(offset.strip("\0 ")):
        with_offset = local + offset.strip("\0 ")
        if captured_at_is_valid(with_offset):
            return with_offset
    return local if captured_at_is_valid(local) else None


def container_captured_at(value: object) -> str | None:
    """A container creation time (ISO 8601) as ``captured_at``; unset clocks are ``None``."""
    if not isinstance(value, str):
        return None
    match = _CONTAINER_DATETIME.fullmatch(value.strip())
    if match is None:
        return None
    local = "{}-{}-{}T{}:{}:{}".format(*match.groups()[:6])
    if local in _UNSET_TIMES:
        return None
    offset = match[7] or ""
    if offset and offset != "Z" and ":" not in offset:
        offset = f"{offset[:3]}:{offset[3:]}"
    text = local + offset
    return text if captured_at_is_valid(text) else None


def _exif_orientation(value: object) -> int | None:
    return value if type(value) is int and 1 <= value <= 8 else None


def probe_photo(descriptor: int) -> Metadata:
    with open_image(descriptor) as image:
        width, height = image.size
        try:
            exif = image.getexif()
        except Exception:
            exif = None
        transform = None if exif is None else _exif_orientation(exif.get(ExifTags.Base.Orientation))
        # pillow-heif applies HEIF rotation itself, resets EXIF to 1 and keeps the original here.
        informational = transform
        if image.format == "HEIF":
            informational = _exif_orientation(image.info.get("original_orientation")) or transform
        if transform in (5, 6, 7, 8):
            width, height = height, width
        captured = make = model = None
        has_location = False
        if exif is not None:
            make = camera_text(exif.get(ExifTags.Base.Make))
            model = camera_text(exif.get(ExifTags.Base.Model))
            try:
                details = exif.get_ifd(ExifTags.IFD.Exif)
                captured = exif_captured_at(
                    details.get(ExifTags.Base.DateTimeOriginal),
                    details.get(ExifTags.Base.OffsetTimeOriginal),
                )
            except Exception:
                captured = None
            try:
                gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
                has_location = any(tag in gps for tag in _GPS_POSITION_TAGS)
            except Exception:
                has_location = ExifTags.IFD.GPSInfo in exif
        return Metadata(
            width=width, height=height, orientation=informational, captured_at=captured,
            captured_at_source="exif" if captured else None, camera_make=make,
            camera_model=model, duration_ms=None, video_codec=None, audio_codec=None,
            container=None, has_location=has_location,
        )


# -- Videos ------------------------------------------------------------------------------------


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _tags(value: object) -> dict[str, str]:
    """String tags with lowercase keys; anything else is dropped."""
    return {
        key.lower(): item for key, item in _mapping(value).items()
        if isinstance(key, str) and isinstance(item, str)
    }


def _is_location_key(key: str) -> bool:
    return "location" in key or "iso6709" in key or key.startswith("gps")


def _codec(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    return lowered if _CODEC.fullmatch(lowered) else None


def _dimension(value: object) -> int | None:
    return value if type(value) is int and 1 <= value <= MAX_METADATA_DIMENSION else None


def _rotation(stream: Mapping[str, object]) -> int:
    """Quarter turns from the display matrix side data, else the legacy ``rotate`` tag."""
    candidates: list[object] = []
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        candidates.extend(_mapping(item).get("rotation") for item in side_data)
    candidates.append(_tags(stream.get("tags")).get("rotate"))
    for candidate in candidates:
        if isinstance(candidate, bool):
            continue
        if isinstance(candidate, str):
            try:
                candidate = float(candidate)
            except ValueError:
                continue
        if isinstance(candidate, int | float) and abs(candidate) <= 3600:
            return round(candidate / 90) % 4
    return 0


def _duration_ms(value: object) -> int | None:
    if not isinstance(value, str) or _DURATION.fullmatch(value) is None:
        return None
    try:
        milliseconds = int((Decimal(value) * 1000).to_integral_value())
    except InvalidOperation:
        return None
    return milliseconds if 0 <= milliseconds <= MAX_DURATION_MS else None


def _container(
    format_name: object, tags: Mapping[str, str], video: str | None, audio: str | None,
) -> Container | None:
    if not isinstance(format_name, str):
        return None
    family = format_name.split(",", 1)[0]
    if family == "mov":
        brand = tags.get("major_brand", "").strip().lower()
        if not brand or brand.startswith("qt"):
            return "mov"
        return "3gp" if brand.startswith("3g") else "mp4"
    if family == "matroska":
        webm = video in _WEBM_VIDEO and (audio is None or audio in _WEBM_AUDIO)
        return "webm" if webm else "matroska"
    if family == "avi":
        return "avi"
    if family == "mpegts":
        return "mpegts"
    return None


def map_ffprobe(document: Mapping[str, object]) -> Metadata:
    """Probe metadata from ffprobe's JSON; refuses a file without a video stream."""
    streams = document.get("streams")
    stream_list = [_mapping(item) for item in streams] if isinstance(streams, list) else []
    video_stream = next((
        stream for stream in stream_list
        if stream.get("codec_type") == "video"
        and _mapping(stream.get("disposition")).get("attached_pic") != 1
    ), None)
    if video_stream is None:
        raise OperationError("unsupported", "no_video_stream")
    audio_stream = next(
        (stream for stream in stream_list if stream.get("codec_type") == "audio"), None,
    )
    container_format = _mapping(document.get("format"))
    format_tags = _tags(container_format.get("tags"))
    video_codec = _codec(video_stream.get("codec_name"))
    audio_codec = None if audio_stream is None else _codec(audio_stream.get("codec_name"))
    width, height = _dimension(video_stream.get("width")), _dimension(video_stream.get("height"))
    if width is None or height is None:
        width = height = None
    elif _rotation(video_stream) % 2:
        width, height = height, width
    duration = _duration_ms(container_format.get("duration"))
    if duration is None:
        duration = _duration_ms(video_stream.get("duration"))
    stream_tags = _tags(video_stream.get("tags"))
    captured = None
    for value in (
        format_tags.get("com.apple.quicktime.creationdate"),
        format_tags.get("creation_time"),
        stream_tags.get("creation_time"),
    ):
        captured = container_captured_at(value)
        if captured is not None:
            break
    make = next((camera_text(format_tags[key]) for key in (
        "com.apple.quicktime.make", "com.android.manufacturer", "make",
    ) if key in format_tags), None)
    model = next((camera_text(format_tags[key]) for key in (
        "com.apple.quicktime.model", "com.android.model", "model",
    ) if key in format_tags), None)
    has_location = any(
        _is_location_key(key)
        for tags in (format_tags, *(_tags(stream.get("tags")) for stream in stream_list))
        for key in tags
    )
    return Metadata(
        width=width, height=height, orientation=None, captured_at=captured,
        captured_at_source="container" if captured else None, camera_make=make,
        camera_model=model, duration_ms=duration, video_codec=video_codec,
        audio_codec=audio_codec,
        container=_container(container_format.get("format_name"), format_tags, video_codec,
                             audio_codec),
        has_location=has_location,
    )


def probe_video(descriptor: int, directory: str, limits: Limits, deadline: float) -> Metadata:
    return map_ffprobe(ffprobe_document(descriptor, directory, limits, deadline))
