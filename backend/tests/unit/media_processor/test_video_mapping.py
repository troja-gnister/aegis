"""ffprobe JSON to probe metadata, and the ffmpeg arguments, without running ffmpeg (M4, M5)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from aegis_media_processor.inventory import FFMPEG_DECODERS, ffmpeg_codecs
from aegis_media_processor.ops import OperationError
from aegis_media_processor.ops.probe import container_captured_at, map_ffprobe
from aegis_media_processor.ops.video import (
    FORMAT_WHITELIST,
    input_options,
    poster_arguments,
    poster_filter,
    transcode_arguments,
    transcode_filter,
)
from aegis_media_processor.protocol import TRANSCODE_PARAMS, Result, check_metadata, encode_result
from aegis_media_processor.sandbox import scrubbed_environment

LOCATION = "+12.5824+098.1317/"


def ffprobe(**changes: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920,
             "height": 1080, "duration": "12.345000", "disposition": {"attached_pic": 0},
             "tags": {"creation_time": "2023-01-01T00:00:00.000000Z", "handler_name": "Video"},
             "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]},
            {"index": 1, "codec_type": "audio", "codec_name": "aac",
             "tags": {"handler_name": "Sound"}},
        ],
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.345000",
            "tags": {
                "major_brand": "isom", "creation_time": "2024-05-01T08:00:00.000000Z",
                "com.apple.quicktime.location.ISO6709": LOCATION,
                "com.apple.quicktime.make": "Synthetic", "com.apple.quicktime.model": "Phone 9",
            },
        },
    }
    document.update(changes)
    return document


def test_rotation_swaps_to_display_dimensions_and_location_is_presence_only() -> None:
    metadata = map_ffprobe(ffprobe())

    assert (metadata.width, metadata.height) == (1080, 1920)
    assert metadata.orientation is None
    assert metadata.duration_ms == 12_345
    assert (metadata.video_codec, metadata.audio_codec, metadata.container) == (
        "h264", "aac", "mp4",
    )
    assert metadata.captured_at == "2024-05-01T08:00:00Z"
    assert metadata.captured_at_source == "container"
    assert (metadata.camera_make, metadata.camera_model) == ("Synthetic", "Phone 9")
    assert metadata.has_location is True
    assert check_metadata(metadata, "video") == metadata
    encoded = encode_result(Result("c" * 32, "ok", None, metadata, ()))
    assert b"12.58" not in encoded and b"098.13" not in encoded and b"ISO6709" not in encoded


@pytest.mark.parametrize("rotation,displayed", [
    (0, (1920, 1080)), (90, (1080, 1920)), (-90, (1080, 1920)), (180, (1920, 1080)),
    (270, (1080, 1920)), (-180.0, (1920, 1080)),
])
def test_display_matrix_rotations(rotation: float, displayed: tuple[int, int]) -> None:
    document = ffprobe()
    document["streams"][0]["side_data_list"] = [{"rotation": rotation}]

    metadata = map_ffprobe(document)

    assert (metadata.width, metadata.height) == displayed


def test_the_legacy_rotate_tag_also_rotates() -> None:
    document = ffprobe()
    del document["streams"][0]["side_data_list"]
    document["streams"][0]["tags"]["rotate"] = "90"

    assert (map_ffprobe(document).width, map_ffprobe(document).height) == (1080, 1920)


def test_the_apple_creation_date_keeps_its_local_offset() -> None:
    document = ffprobe()
    document["format"]["tags"]["com.apple.quicktime.creationdate"] = "2024-05-01T10:00:00+0200"

    assert map_ffprobe(document).captured_at == "2024-05-01T10:00:00+02:00"


@pytest.mark.parametrize("value,expected", [
    ("2024-05-01T08:00:00.000000Z", "2024-05-01T08:00:00Z"),
    ("2024-05-01 08:00:00", "2024-05-01T08:00:00"),
    ("2024-05-01T10:00:00-0730", "2024-05-01T10:00:00-07:30"),
    ("1970-01-01T00:00:00.000000Z", None),
    ("1904-01-01T00:00:00Z", None),
    ("2024-02-30T00:00:00Z", None),
    ("yesterday", None),
    (17, None),
])
def test_container_creation_times(value: object, expected: str | None) -> None:
    assert container_captured_at(value) == expected


def test_cover_art_is_not_the_video_stream_and_audio_only_is_unsupported() -> None:
    cover = {"codec_type": "video", "codec_name": "mjpeg", "width": 600, "height": 600,
             "disposition": {"attached_pic": 1}}
    audio = {"codec_type": "audio", "codec_name": "aac"}

    with pytest.raises(OperationError) as refused:
        map_ffprobe(ffprobe(streams=[cover, audio]))
    assert (refused.value.status, refused.value.code) == ("unsupported", "no_video_stream")
    with pytest.raises(OperationError):
        map_ffprobe(ffprobe(streams=[]))
    document = ffprobe()
    document["streams"].insert(0, cover)
    assert map_ffprobe(document).video_codec == "h264"


@pytest.mark.parametrize("format_name,brand,video,audio,expected", [
    ("mov,mp4,m4a,3gp,3g2,mj2", "qt  ", "hevc", "aac", "mov"),
    ("mov,mp4,m4a,3gp,3g2,mj2", None, "h264", "aac", "mov"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "3gp4", "h264", "aac", "3gp"),
    ("mov,mp4,m4a,3gp,3g2,mj2", "mp42", "h264", None, "mp4"),
    ("matroska,webm", None, "vp9", "opus", "webm"),
    ("matroska,webm", None, "av1", None, "webm"),
    ("matroska,webm", None, "h264", "aac", "matroska"),
    ("avi", None, "mpeg4", "mp3", "avi"),
    ("mpegts", None, "h264", "ac3", "mpegts"),
    ("flv", None, "h264", "aac", None),
])
def test_container_names(
    format_name: str, brand: str | None, video: str, audio: str | None, expected: str | None,
) -> None:
    streams: list[dict[str, Any]] = [{"codec_type": "video", "codec_name": video,
                                      "width": 640, "height": 480}]
    if audio is not None:
        streams.append({"codec_type": "audio", "codec_name": audio})
    tags = {} if brand is None else {"major_brand": brand}

    metadata = map_ffprobe({"streams": streams, "format": {"format_name": format_name,
                                                            "tags": tags}})

    assert metadata.container == expected
    assert metadata.has_location is False
    assert metadata.captured_at is None


def test_hostile_ffprobe_values_become_unknown_fields() -> None:
    document = ffprobe()
    stream = document["streams"][0]
    stream.update({"width": "1920", "height": 1 << 30, "codec_name": "H.264 <script>"})
    document["streams"][1]["codec_name"] = "a" * 40
    document["format"].update({"duration": "NaN", "format_name": ["mov"], "tags": "nope"})

    metadata = map_ffprobe(document)

    assert (metadata.width, metadata.height) == (None, None)
    assert metadata.video_codec is None and metadata.audio_codec is None
    assert metadata.duration_ms == 12_345  # from the stream instead
    assert metadata.container is None
    assert metadata.camera_make is None
    assert check_metadata(metadata, "video") == metadata


def test_location_keys_anywhere_count_and_others_do_not() -> None:
    plain = copy.deepcopy(ffprobe())
    del plain["format"]["tags"]["com.apple.quicktime.location.ISO6709"]
    assert map_ffprobe(plain).has_location is False

    android = copy.deepcopy(plain)
    android["format"]["tags"]["location-eng"] = LOCATION
    assert map_ffprobe(android).has_location is True

    in_stream = copy.deepcopy(plain)
    in_stream["streams"][0]["tags"]["location"] = LOCATION
    assert map_ffprobe(in_stream).has_location is True


def test_tools_read_only_the_passed_descriptor_through_whitelisted_demuxers() -> None:
    options = input_options(5)

    # fd: resolves no path, so Landlock never sees the original's path; it is seekable.
    assert options == [
        "-protocol_whitelist", "fd", "-format_whitelist", FORMAT_WHITELIST,
        "-fd", "5", "-i", "fd:",
    ]
    for invalid in (-1, 0, 2):
        with pytest.raises(ValueError):
            input_options(invalid)
    assert set(FORMAT_WHITELIST.split(",")) == {
        "mov", "mp4", "m4a", "3gp", "3g2", "mj2", "matroska", "webm", "avi", "mpegts",
    }


def test_filters_fit_within_their_boxes_without_enlarging() -> None:
    assert poster_filter(2048) == (
        "scale=w='iw*sar':h='ih',setsar=1,"
        "scale=w='min(2048,iw)':h='min(2048,ih)':force_original_aspect_ratio=decrease"
    )
    assert transcode_filter(TRANSCODE_PARAMS) == (
        "scale=w='if(gte(iw,ih),min(1920,iw),min(1080,iw))'"
        ":h='if(gte(iw,ih),min(1080,ih),min(1920,ih))'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def _option_values(arguments: list[str], option: str) -> list[str]:
    return [arguments[index + 1] for index, value in enumerate(arguments) if value == option]


def test_poster_arguments_bound_threads_and_drop_metadata() -> None:
    arguments = poster_arguments("/usr/bin/ffmpeg", 5, 1000, 2048, "/scratch/work/j/frame.png")

    assert arguments[:5] == ["/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"]
    assert _option_values(arguments, "-threads") == ["2", "2"]
    assert _option_values(arguments, "-filter_threads") == ["2"]
    assert _option_values(arguments, "-ss") == ["1.000"]
    assert arguments.index("-ss") < arguments.index("-i")
    assert _option_values(arguments, "-i") == ["fd:"]
    assert _option_values(arguments, "-fd") == ["5"]
    assert _option_values(arguments, "-protocol_whitelist") == ["fd"]
    assert _option_values(arguments, "-map") == ["0:V:0"]
    assert _option_values(arguments, "-map_metadata") == ["-1"]
    assert arguments[-2:] == ["-n", "file:/scratch/work/j/frame.png"]
    assert "-ss" not in poster_arguments("ffmpeg", 5, 0, 400, "/f.png")


def test_transcode_arguments_bound_threads_and_drop_metadata() -> None:
    arguments = transcode_arguments("/usr/bin/ffmpeg", 7, TRANSCODE_PARAMS, "/w/transcode.mp4")

    assert _option_values(arguments, "-threads") == ["4", "4"]
    assert _option_values(arguments, "-filter_threads") == ["4"]
    assert _option_values(arguments, "-i") == ["fd:"]
    assert _option_values(arguments, "-fd") == ["7"]
    assert _option_values(arguments, "-map") == ["0:V:0", "0:a:0?"]
    assert _option_values(arguments, "-map_metadata") == ["-1"]
    assert _option_values(arguments, "-map_chapters") == ["-1"]
    assert _option_values(arguments, "-c:v") == ["libx264"]
    assert _option_values(arguments, "-profile:v") == ["high"]
    assert _option_values(arguments, "-pix_fmt") == ["yuv420p"]
    assert _option_values(arguments, "-b:a") == ["160k"]
    assert _option_values(arguments, "-movflags") == ["+faststart"]
    assert arguments[-2:] == ["-n", "file:/w/transcode.mp4"]


def test_every_child_environment_limits_malloc_arenas() -> None:
    assert scrubbed_environment()["MALLOC_ARENA_MAX"] == "2"
    assert scrubbed_environment({"TMPDIR": "/w"})["MALLOC_ARENA_MAX"] == "2"


def test_inventory_reads_codec_listings() -> None:
    listing = (
        "Decoders:\n V..... = Video\n A..... = Audio\n ------\n"
        " VFS..D h264                 H.264 / AVC / MPEG-4 AVC\n"
        " VFS..D hevc                 HEVC (High Efficiency Video Coding)\n"
        " A....D aac                  AAC (Advanced Audio Coding)\n"
    )

    assert ffmpeg_codecs(listing, FFMPEG_DECODERS) == ["h264", "hevc", "aac"]
