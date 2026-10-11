"""``canPlayType`` arguments for originals (spec M7, API contract ``playbackType``).

The probe stores ffprobe codec names (``h264``, ``hevc``, ``vp9``, ``av1``, ``aac``,
``opus``) and a container family; it stores no profile, level, bit depth or sample-entry
tag. A codec string is derived only where it never claims less than the stream may need:

- H.264 claims High profile, with a level chosen from the display size;
- HEVC claims Main 10, with a level from the display size, as ``hvc1`` for QuickTime
  files (Apple devices write ``hvc1`` sample entries) and as ``hev1`` otherwise, so a
  browser that only plays ``hvc1`` asks for a transcode instead of failing;
- AV1 claims Main profile, 10-bit, with a level from the display size;
- VP8 and VP9 use their plain WebM names, and are not claimed in other containers;
- audio is ``mp4a.40.2`` (AAC) or ``opus`` (both containers), or ``vorbis`` (WebM).

A stored value that already is an RFC 6381 string of the right family is used as is. When
anything cannot be derived, the result is ``None`` and the client requests a transcode.
The MIME type is the one the original is actually served with, so a value is produced only
for extensions on the inline allowlist.
"""

from __future__ import annotations

import re
from typing import Final

from .formats import INLINE_ORIGINAL_CONTENT_TYPES

# Display-size thresholds in luma samples: up to 1080p, up to 4K (DCI), and above.
_HD_SAMPLES: Final = 2_228_224
_UHD_SAMPLES: Final = 8_912_896
_MAX_SAMPLES: Final = 35_651_584

_AVC_RE: Final = re.compile(r"avc[13]\.[0-9A-Fa-f]{6}")
_HEVC_RE: Final = re.compile(r"(?:hvc1|hev1)\.[0-9A-Za-z.]{1,40}")
_AV1_RE: Final = re.compile(r"av01\.[0-9]\.[0-9]{2}[MH]\.[0-9]{2}(?:\.[0-9.]{1,24})?")
_VP9_RE: Final = re.compile(r"vp09\.[0-9]{2}\.[0-9]{2}\.[0-9]{2}(?:\.[0-9.]{1,24})?")
_MP4A_RE: Final = re.compile(r"mp4a\.40\.(?:2|5|29)")

_ISO_BMFF_CONTAINERS: Final = frozenset({"mp4", "mov"})
_ISO_BMFF_EXTENSIONS: Final = frozenset({"mp4", "m4v", "mov"})


def _samples(width: object, height: object) -> int | None:
    if type(width) is not int or type(height) is not int or width < 1 or height < 1:
        return None
    samples = width * height
    return samples if samples <= _MAX_SAMPLES else None


def _avc(width: object, height: object) -> str | None:
    samples = _samples(width, height)
    if samples is None:
        return None
    # High profile (0x64), no constraint flags; level 4.2, 5.2 or 6.2.
    level = 0x2A if samples <= _HD_SAMPLES else 0x34 if samples <= _UHD_SAMPLES else 0x3E
    return f"avc1.6400{level:02X}"


def _hevc(width: object, height: object, *, quicktime: bool) -> str | None:
    samples = _samples(width, height)
    if samples is None:
        return None
    # Main 10 (profile 2, compatibility 4), Main tier; level 4.1, 5.1 or 6.1 (x30).
    level = 123 if samples <= _HD_SAMPLES else 153 if samples <= _UHD_SAMPLES else 183
    return f"{'hvc1' if quicktime else 'hev1'}.2.4.L{level}.B0"


def _av1(width: object, height: object) -> str | None:
    samples = _samples(width, height)
    if samples is None:
        return None
    # Main profile, Main tier, 10-bit; seq_level_idx 4.1 (9), 5.1 (13) or 6.1 (17).
    level = 9 if samples <= _HD_SAMPLES else 13 if samples <= _UHD_SAMPLES else 17
    return f"av01.0.{level:02d}M.10"


def _iso_video(codec: str, width: object, height: object, *, quicktime: bool) -> str | None:
    if _AVC_RE.fullmatch(codec) or _HEVC_RE.fullmatch(codec) or _AV1_RE.fullmatch(codec):
        return codec
    if codec == "h264":
        return _avc(width, height)
    if codec == "hevc":
        return _hevc(width, height, quicktime=quicktime)
    if codec == "av1":
        return _av1(width, height)
    return None


def _webm_video(codec: str, width: object, height: object) -> str | None:
    if codec in ("vp8", "vp9"):
        return codec
    if _VP9_RE.fullmatch(codec) or _AV1_RE.fullmatch(codec):
        return codec
    if codec == "av1":
        return _av1(width, height)
    return None


def _iso_audio(codec: str) -> str | None:
    if codec == "aac":
        return "mp4a.40.2"
    if _MP4A_RE.fullmatch(codec):
        return codec
    return "opus" if codec == "opus" else None


def _webm_audio(codec: str) -> str | None:
    return codec if codec in ("opus", "vorbis") else None


def playback_type(
    *,
    type_hint: object,
    container: object,
    video_codec: object,
    audio_codec: object,
    width: object,
    height: object,
) -> str | None:
    """The ``canPlayType`` argument for an original video, or ``None`` to transcode."""
    if not isinstance(type_hint, str) or not isinstance(container, str):
        return None
    mime = INLINE_ORIGINAL_CONTENT_TYPES.get(type_hint)
    if mime is None or not mime.startswith("video/") or not isinstance(video_codec, str):
        return None
    if audio_codec is not None and not isinstance(audio_codec, str):
        return None
    if type_hint in _ISO_BMFF_EXTENSIONS and container in _ISO_BMFF_CONTAINERS:
        video = _iso_video(video_codec, width, height, quicktime=container == "mov")
        audio = None if audio_codec is None else _iso_audio(audio_codec)
    elif type_hint == "webm" and container == "webm":
        video = _webm_video(video_codec, width, height)
        audio = None if audio_codec is None else _webm_audio(audio_codec)
    else:
        return None
    if video is None or (audio_codec is not None and audio is None):
        return None
    codecs = video if audio is None else f"{video}, {audio}"
    return f'{mime}; codecs="{codecs}"'
