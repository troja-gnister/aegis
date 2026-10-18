from __future__ import annotations

import pytest
from aegis_apps.media.formats import INLINE_ORIGINAL_CONTENT_TYPES, VIDEO_EXTENSIONS
from aegis_apps.media.playback import playback_type


def _playback(**overrides: object) -> str | None:
    values: dict[str, object] = {
        "type_hint": "mp4",
        "container": "mp4",
        "video_codec": "h264",
        "audio_codec": "aac",
        "width": 1920,
        "height": 1080,
    }
    values.update(overrides)
    return playback_type(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, 'video/mp4; codecs="avc1.64002A, mp4a.40.2"'),
        ({"audio_codec": None}, 'video/mp4; codecs="avc1.64002A"'),
        ({"width": 3840, "height": 2160}, 'video/mp4; codecs="avc1.640034, mp4a.40.2"'),
        ({"width": 7680, "height": 4320}, 'video/mp4; codecs="avc1.64003E, mp4a.40.2"'),
        ({"type_hint": "m4v"}, 'video/mp4; codecs="avc1.64002A, mp4a.40.2"'),
        ({"audio_codec": "opus"}, 'video/mp4; codecs="avc1.64002A, opus"'),
        # Apple devices write hvc1 sample entries in QuickTime files.
        (
            {"type_hint": "mov", "container": "mov", "video_codec": "hevc"},
            'video/quicktime; codecs="hvc1.2.4.L123.B0, mp4a.40.2"',
        ),
        (
            {"type_hint": "mov", "container": "mov", "video_codec": "hevc",
             "width": 2160, "height": 3840},
            'video/quicktime; codecs="hvc1.2.4.L153.B0, mp4a.40.2"',
        ),
        # Elsewhere the sample entry is unknown; hev1 never makes an hvc1-only browser
        # attempt (and fail) playback.
        ({"video_codec": "hevc"}, 'video/mp4; codecs="hev1.2.4.L123.B0, mp4a.40.2"'),
        ({"video_codec": "av1"}, 'video/mp4; codecs="av01.0.09M.10, mp4a.40.2"'),
        (
            {"type_hint": "webm", "container": "webm", "video_codec": "vp9",
             "audio_codec": "opus"},
            'video/webm; codecs="vp9, opus"',
        ),
        (
            {"type_hint": "webm", "container": "webm", "video_codec": "vp8",
             "audio_codec": "vorbis"},
            'video/webm; codecs="vp8, vorbis"',
        ),
        (
            {"type_hint": "webm", "container": "webm", "video_codec": "av1",
             "audio_codec": None, "width": 3840, "height": 2160},
            'video/webm; codecs="av01.0.13M.10"',
        ),
        # Stored RFC 6381 strings are used as they are.
        ({"video_codec": "avc1.4D401F"}, 'video/mp4; codecs="avc1.4D401F, mp4a.40.2"'),
        ({"audio_codec": "mp4a.40.5"}, 'video/mp4; codecs="avc1.64002A, mp4a.40.5"'),
    ],
)
def test_playback_type_is_derived_conservatively(
    overrides: dict[str, object], expected: str,
) -> None:
    assert _playback(**overrides) == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"video_codec": "mpeg4"},
        {"video_codec": "vp9"},
        {"video_codec": None},
        {"audio_codec": "mp3"},
        {"audio_codec": "pcm_s16le"},
        {"width": None},
        {"height": 0},
        {"width": 100_000, "height": 100_000},
        {"container": "matroska"},
        {"container": "webm"},
        {"container": None},
        {"type_hint": "mkv", "container": "matroska"},
        {"type_hint": "avi", "container": "avi"},
        {"type_hint": "3gp", "container": "3gp"},
        {"type_hint": "3g2", "container": "3gp", "video_codec": "h263"},
        {"type_hint": "f4v", "container": "mp4"},
        {"type_hint": "wmv", "container": "asf", "video_codec": "wmv2", "audio_codec": "wmav2"},
        {"type_hint": "flv", "container": "flv", "video_codec": "flv1", "audio_codec": "mp3"},
        {"type_hint": "mpg", "container": "mpeg", "video_codec": "mpeg1video"},
        {"type_hint": "ogv", "container": "ogg", "video_codec": "theora"},
        {"type_hint": "jpg"},
        {"type_hint": None},
        {"type_hint": "webm", "container": "webm", "video_codec": "h264"},
        {"type_hint": "webm", "container": "webm", "video_codec": "vp9", "audio_codec": "aac"},
    ],
)
def test_underivable_playback_is_null_so_the_client_transcodes(
    overrides: dict[str, object],
) -> None:
    assert _playback(**overrides) is None


@pytest.mark.parametrize(
    "type_hint", sorted(VIDEO_EXTENSIONS - INLINE_ORIGINAL_CONTENT_TYPES.keys()),
)
@pytest.mark.parametrize("container", ["mp4", "mov"])
def test_videos_off_the_inline_allowlist_always_play_through_a_transcode(
    type_hint: str, container: str,
) -> None:
    """Even an H.264 and AAC stream: the original is never served inline (WMV, FLV, MPG...)."""
    assert _playback(type_hint=type_hint, container=container) is None
