"""Video probes, posters and transcodes with the real ffmpeg (M4, M5).

These need ffmpeg and ffprobe, so they carry the ``media_tools`` marker: deselected in ordinary
runs, run inside the aegis-media image by tests/deployment/test_media_image.py, and a hard
failure (never a skip) when selected where the tools are missing. They import nothing from a
conftest, because the in-image run loads none. Fixtures are generated here with ffmpeg's lavfi.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from aegis_media_processor.ops.video import FORMAT_WHITELIST
from aegis_media_processor.protocol import (
    Limits,
    Op,
    TranscodeParams,
    decode_result,
    encode_result,
    output_magic_matches,
)

from .support import make_job, make_roots, run_job, source_path, write_source

pytestmark = pytest.mark.media_tools

LOCATION = "+12.5824+098.1317/"
LIMITS = Limits(wall_seconds=120, cpu_seconds=120, memory_bytes=2 << 30, output_bytes=256 << 20)


@pytest.fixture(autouse=True)
def tools() -> None:
    missing = [name for name in ("ffmpeg", "ffprobe") if shutil.which(name) is None]
    if missing:
        pytest.fail(f"media_tools tests need {', '.join(missing)}: run them in the aegis-media "
                    "image (tests/deployment/test_media_image.py)", pytrace=False)


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    return make_roots(tmp_path)


def ffmpeg(*arguments: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *arguments],
        check=True, timeout=180, stdin=subprocess.DEVNULL, capture_output=True,
    )


def ffprobe_json(path: str) -> dict[str, Any]:
    completed = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
         path], check=True, timeout=60, capture_output=True,
    )
    document: dict[str, Any] = json.loads(completed.stdout)
    return document


def make_clip(
    roots: Path, relative: bytes, *, codec: str = "libx264", muxer: str = "mp4",
    size: str = "320x240", duration: str = "2", audio: bool = True, location: bool = True,
) -> str:
    path = os.fsdecode(source_path(roots, relative))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    arguments = ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate=10:duration={duration}"]
    if audio:
        arguments += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    arguments += ["-map", "0:v"]
    if audio:
        arguments += ["-map", "1:a", "-c:a", "aac"]
    arguments += ["-c:v", codec, "-pix_fmt", "yuv420p"]
    if codec == "libx265":
        arguments += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
    arguments += ["-metadata", "creation_time=2024-05-01T08:00:00Z"]
    if location:
        arguments += ["-movflags", "+use_metadata_tags",
                      "-metadata", f"com.apple.quicktime.location.ISO6709={LOCATION}"]
    ffmpeg(*arguments, "-f", muxer, path)
    return path


def test_mp4_probe_maps_codecs_duration_capture_time_and_location_presence(
    tmp_path: Path, roots: Path,
) -> None:
    make_clip(roots, b"clips/clip.mp4")
    job = make_job(roots, b"clips/clip.mp4", "probe", limits=LIMITS, job_id="a" * 32)

    result, directory = run_job(tmp_path, roots, job)

    assert result.status == "ok", result
    metadata = result.metadata
    assert metadata is not None
    assert (metadata.width, metadata.height) == (320, 240)
    assert metadata.duration_ms is not None and 1800 <= metadata.duration_ms <= 2200
    assert (metadata.video_codec, metadata.audio_codec, metadata.container) == (
        "h264", "aac", "mp4",
    )
    assert metadata.captured_at == "2024-05-01T08:00:00Z"
    assert metadata.captured_at_source == "container"
    assert metadata.orientation is None
    assert metadata.has_location is True
    encoded = encode_result(result, job)
    assert b"12.58" not in encoded and b"098.13" not in encoded
    assert b"iso6709" not in encoded.lower()
    assert os.listdir(directory) == []


def test_hevc_mov_probe(tmp_path: Path, roots: Path) -> None:
    make_clip(roots, b"clips/phone.mov", codec="libx265", muxer="mov")

    result, _ = run_job(tmp_path, roots, make_job(roots, b"clips/phone.mov", "probe",
                                                  limits=LIMITS))

    assert result.status == "ok", result
    assert result.metadata is not None
    assert (result.metadata.video_codec, result.metadata.container) == ("hevc", "mov")
    assert result.metadata.has_location is True


def test_a_rotated_video_reports_display_dimensions(tmp_path: Path, roots: Path) -> None:
    upright = make_clip(roots, b"clips/upright.mp4", location=False)
    rotated = os.fsdecode(source_path(roots, b"clips/rotated.mp4"))
    ffmpeg("-display_rotation", "90", "-i", upright, "-c", "copy", "-f", "mp4", rotated)

    result, _ = run_job(tmp_path, roots, make_job(roots, b"clips/rotated.mp4", "probe",
                                                  limits=LIMITS))

    assert result.status == "ok", result
    assert result.metadata is not None
    assert (result.metadata.width, result.metadata.height) == (240, 320)


def test_poster_frames_at_both_sizes(tmp_path: Path, roots: Path) -> None:
    make_clip(roots, b"clips/wide.mp4", size="1280x720", duration="2")

    result, directory = run_job(tmp_path, roots, make_job(roots, b"clips/wide.mp4", "poster",
                                                          limits=LIMITS))

    assert result.status == "ok", result
    assert [(o.name, o.width, o.height) for o in result.outputs] == [
        ("thumbnail", 400, 225), ("poster", 1280, 720),
    ]
    assert sorted(os.listdir(directory)) == ["poster.webp", "thumbnail.webp"]
    for output in result.outputs:
        data = (directory / output.filename).read_bytes()
        assert len(data) == output.bytes
        assert output_magic_matches("webp", data[:12])


def test_a_clip_shorter_than_the_poster_time_uses_its_first_frame(
    tmp_path: Path, roots: Path,
) -> None:
    make_clip(roots, b"clips/short.mp4", duration="0.4", audio=False, location=False)

    result, directory = run_job(tmp_path, roots, make_job(roots, b"clips/short.mp4", "poster",
                                                          limits=LIMITS))

    assert result.status == "ok", result
    assert [(o.name, o.width, o.height) for o in result.outputs] == [
        ("thumbnail", 320, 240), ("poster", 320, 240),
    ]
    assert "frame.png" not in os.listdir(directory)


def test_transcode_writes_h264_aac_faststart_mp4_without_location(
    tmp_path: Path, roots: Path,
) -> None:
    make_clip(roots, b"clips/phone.mov", codec="libx265", muxer="mov")

    result, directory = run_job(tmp_path, roots, make_job(roots, b"clips/phone.mov", "transcode",
                                                          limits=LIMITS))

    assert result.status == "ok", result
    (output,) = result.outputs
    assert (output.name, output.format, output.width, output.height) == (
        "transcode", "mp4", 320, 240,
    )
    target = directory / "transcode.mp4"
    data = target.read_bytes()
    assert len(data) == output.bytes and output_magic_matches("mp4", data[:12])
    # faststart: the movie box comes before the media data.
    assert 0 <= data.find(b"moov") < data.find(b"mdat")
    probed = ffprobe_json(str(target))
    video = [s for s in probed["streams"] if s["codec_type"] == "video"]
    audio = [s for s in probed["streams"] if s["codec_type"] == "audio"]
    assert [(s["codec_name"], s["profile"], s["pix_fmt"]) for s in video] == [
        ("h264", "High", "yuv420p"),
    ]
    assert [s["codec_name"] for s in audio] == ["aac"]
    tags = {key.lower() for key in probed["format"].get("tags", {})}
    assert not any("location" in key or "iso6709" in key for key in tags)
    assert LOCATION.encode() not in data
    assert os.listdir(directory) == ["transcode.mp4"]


def test_transcode_fits_both_orientations_within_the_long_and_short_edges(
    tmp_path: Path, roots: Path,
) -> None:
    upright = make_clip(roots, b"clips/landscape.mp4", location=False)
    ffmpeg("-display_rotation", "90", "-i", upright, "-c", "copy", "-f", "mp4",
           os.fsdecode(source_path(roots, b"clips/portrait.mp4")))
    params = TranscodeParams(long_edge=160, short_edge=144, audio_kbps=64)

    sizes = []
    for relative in (b"clips/landscape.mp4", b"clips/portrait.mp4"):
        job = make_job(roots, relative, "transcode", params=params, limits=LIMITS)
        result, _ = run_job(tmp_path, roots, job)
        assert result.status == "ok", result
        sizes.append((result.outputs[0].width, result.outputs[0].height))

    assert sizes == [(160, 120), (120, 160)]


def test_a_video_backfill_probes_and_extracts_the_poster_pair_in_one_job(
    tmp_path: Path, roots: Path,
) -> None:
    make_clip(roots, b"clips/wide.mp4", size="1280x720", duration="2")
    backfill = make_job(roots, b"clips/wide.mp4", "backfill", limits=LIMITS, job_id="b" * 32)

    result, directory = run_job(tmp_path, roots, backfill)
    probed, _ = run_job(tmp_path, roots, make_job(roots, b"clips/wide.mp4", "probe",
                                                  limits=LIMITS))
    poster, poster_directory = run_job(tmp_path, roots, make_job(roots, b"clips/wide.mp4",
                                                                 "poster", limits=LIMITS))

    assert result.status == "ok", result
    assert result.metadata is not None and result.metadata == probed.metadata
    assert (result.metadata.width, result.metadata.height) == (1280, 720)
    assert result.metadata.has_location is True
    assert [(o.name, o.width, o.height) for o in result.outputs] == [
        ("thumbnail", 400, 225), ("poster", 1280, 720),
    ]
    assert [(o.name, o.width, o.height) for o in result.outputs] == [
        (o.name, o.width, o.height) for o in poster.outputs
    ]
    assert sorted(os.listdir(directory)) == ["poster.webp", "thumbnail.webp"]
    for output in result.outputs:
        data = (directory / output.filename).read_bytes()
        assert len(data) == output.bytes and output_magic_matches("webp", data[:12])
        assert LOCATION.encode() not in data
    encoded = encode_result(result, backfill)
    assert b"12.58" not in encoded and b"098.13" not in encoded
    assert b"iso6709" not in encoded.lower()
    assert decode_result(encoded, backfill) == result
    assert sorted(os.listdir(poster_directory)) == ["poster.webp", "thumbnail.webp"]


def test_a_video_backfill_without_a_video_stream_is_unsupported_before_any_frame(
    tmp_path: Path, roots: Path,
) -> None:
    path = os.fsdecode(source_path(roots, b"clips/voice.mp4"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-f", "mp4", path)
    make_clip(roots, b"clips/short.mp4", duration="0.4", audio=False, location=False)

    voice, directory = run_job(tmp_path, roots, make_job(roots, b"clips/voice.mp4", "backfill",
                                                         limits=LIMITS))
    short, short_directory = run_job(tmp_path, roots, make_job(roots, b"clips/short.mp4",
                                                               "backfill", limits=LIMITS))

    assert (voice.status, voice.safe_error_code) == ("unsupported", "no_video_stream")
    assert (voice.metadata, voice.outputs) == (None, ())
    assert os.listdir(directory) == []
    # A clip shorter than the poster time still gets its first frame, as from a poster job.
    assert short.status == "ok", short
    assert [(o.width, o.height) for o in short.outputs] == [(320, 240), (320, 240)]
    assert "frame.png" not in os.listdir(short_directory)


def test_a_corrupt_video_fails(tmp_path: Path, roots: Path) -> None:
    write_source(roots, b"clips/corrupt.mp4", b"\x00\x00\x00\x18ftypisom" + b"garbage" * 300)

    for op in ("probe", "poster", "transcode", "backfill"):
        result, _ = run_job(tmp_path, roots, make_job(roots, b"clips/corrupt.mp4", op,
                                                      limits=LIMITS))
        assert result.status == "failed", (op, result)


def test_audio_only_files_are_unsupported(tmp_path: Path, roots: Path) -> None:
    path = os.fsdecode(source_path(roots, b"clips/voice.mp4"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "aac", "-f", "mp4", path)

    result, _ = run_job(tmp_path, roots, make_job(roots, b"clips/voice.mp4", "probe",
                                                  limits=LIMITS))

    assert (result.status, result.safe_error_code) == ("unsupported", "no_video_stream")


DASH_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static" minBufferTime="PT1S"
     mediaPresentationDuration="PT2S" profiles="urn:mpeg:dash:profile:isoff-on-demand:2011">
  <Period id="0" duration="PT2S">
    <AdaptationSet id="0" contentType="video" mimeType="video/mp4">
      <Representation id="secret" bandwidth="200000" codecs="avc1.64000d" width="320"
                      height="240">
        <BaseURL>file:{secret}</BaseURL>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>
"""


def test_a_dash_manifest_cannot_pull_in_another_file(tmp_path: Path, roots: Path) -> None:
    """The demuxer whitelist is what stops a manifest disguised as an MP4 (I4).

    Debian's ffmpeg has the DASH demuxer (libxml2). It is chosen by content and opens ``file:``
    segments with media extensions, so without the restriction this ``.mp4`` would yield the
    poster or transcode of a file from another root.
    """
    upright = make_clip(roots, b"clips/source.mp4", location=False)
    secret = tmp_path / "elsewhere" / "secret.mp4"
    secret.parent.mkdir()
    ffmpeg("-i", upright, "-c", "copy", "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
           "-f", "mp4", str(secret))
    write_source(roots, b"clips/manifest.mp4", DASH_MANIFEST.format(secret=secret).encode())
    manifest = os.fsdecode(source_path(roots, b"clips/manifest.mp4"))

    # Positive control: unrestricted, ffprobe follows the manifest to the other file.
    unrestricted = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", manifest],
        capture_output=True, timeout=60, check=False,
    )
    assert unrestricted.returncode == 0, unrestricted.stderr[-2000:]
    streams = json.loads(unrestricted.stdout)["streams"]
    assert [(s["codec_type"], s["width"]) for s in streams if s["codec_type"] == "video"] == [
        ("video", 320),
    ]
    # Only the demuxer whitelist added: refused.
    whitelisted = subprocess.run(
        ["ffprobe", "-v", "error", "-format_whitelist", FORMAT_WHITELIST, "-print_format",
         "json", "-show_streams", manifest], capture_output=True, timeout=60, check=False,
    )
    assert whitelisted.returncode != 0

    for op in ("probe", "poster", "transcode", "backfill"):
        result, directory = run_job(tmp_path, roots, make_job(roots, b"clips/manifest.mp4", op,
                                                              limits=LIMITS))
        assert result.status == "failed", (op, result)
        assert os.listdir(directory) == []


def test_concat_scripts_are_refused(tmp_path: Path, roots: Path) -> None:
    """Covered twice: the concat demuxer's default ``safe=1`` and the demuxer whitelist."""
    secret = tmp_path / "secret.mp4"
    shutil.copyfile(make_clip(roots, b"clips/source.mp4", location=False), secret)
    write_source(roots, b"clips/concat.mp4", f"ffconcat version 1.0\nfile {secret}\n".encode())

    for op in ("probe", "transcode"):
        result, directory = run_job(tmp_path, roots, make_job(roots, b"clips/concat.mp4", op,
                                                              limits=LIMITS))
        assert result.status == "failed", (op, result)
        assert os.listdir(directory) == []


def test_confined_children_decode_video_through_the_fd_protocol(
    tmp_path: Path, roots: Path,
) -> None:
    """The real loop: Landlock'd children, ffmpeg reading the inherited descriptor (fd:)."""
    from aegis_media_processor.loop import Processor
    from aegis_media_processor.protocol import MAX_RESULT_BYTES, read_regular_file, write_job

    make_clip(roots, b"clips/clip.mp4")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    operations: tuple[Op, ...] = ("probe", "poster", "transcode", "backfill")
    jobs = [make_job(roots, b"clips/clip.mp4", op, limits=LIMITS) for op in operations]

    with Processor(str(scratch), str(roots), 2) as processor:
        processor.prepare()
        for job in jobs:
            write_job(str(scratch / "inbox"), job)
        processor.run(until_idle=True)

    for job in jobs:
        data = read_regular_file(str(scratch / "done" / job.id / "result.json"), MAX_RESULT_BYTES)
        result = decode_result(data, job)
        assert result.status == "ok", (job.op, result)
        assert sorted(os.listdir(scratch / "done" / job.id)) == sorted(
            ["result.json", *(output.filename for output in result.outputs)]
        )
    assert os.listdir(scratch / "work") == []


# -- Legacy formats ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LegacyClip:
    """A clip as an old phone, camera or encoder wrote it, and what its probe must report."""

    options: tuple[str, ...]  # the encoders and the muxer
    codecs: tuple[str, str, str]  # video codec, audio codec, container
    size: tuple[int, int] = (320, 240)
    captured: str | None = None


CAPTURED = "2024-05-01T08:00:00Z"
# Debian's ffmpeg has no WMV3 (WMV 9), VC-1 or AMR encoder, so those decoders get no fixture.
# MPEG-1 and MPEG-2 need a standard frame rate, hence 25 fps for every clip.
LEGACY_CLIPS: dict[bytes, LegacyClip] = {
    b"legacy/phone.3g2": LegacyClip(
        ("-c:v", "h263", "-c:a", "aac", "-ar", "16000", "-ac", "1", "-b:a", "32k",
         "-metadata", f"creation_time={CAPTURED}", "-f", "3g2"),
        ("h263", "aac", "3gp"), size=(176, 144), captured=CAPTURED,
    ),
    b"legacy/phone-mpeg4.3g2": LegacyClip(
        ("-c:v", "mpeg4", "-c:a", "aac", "-f", "3g2"), ("mpeg4", "aac", "3gp"),
    ),
    b"legacy/old.wmv": LegacyClip(
        ("-c:v", "wmv2", "-c:a", "wmav2", "-f", "asf"), ("wmv2", "wmav2", "asf"),
    ),
    b"legacy/stream.asf": LegacyClip(
        ("-c:v", "wmv1", "-c:a", "wmav1", "-f", "asf"), ("wmv1", "wmav1", "asf"),
    ),
    b"legacy/flash.flv": LegacyClip(
        ("-c:v", "flv", "-c:a", "libmp3lame", "-f", "flv"), ("flv1", "mp3", "flv"),
    ),
    b"legacy/flash-h264.flv": LegacyClip(
        ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "flv"),
        ("h264", "aac", "flv"),
    ),
    b"legacy/flash.f4v": LegacyClip(
        ("-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-metadata", f"creation_time={CAPTURED}", "-f", "f4v"),
        ("h264", "aac", "mp4"), captured=CAPTURED,
    ),
    b"legacy/home.mpg": LegacyClip(
        ("-c:v", "mpeg1video", "-c:a", "mp2", "-f", "mpeg"), ("mpeg1video", "mp2", "mpeg"),
    ),
    b"legacy/home.mpeg": LegacyClip(
        ("-c:v", "mpeg2video", "-c:a", "mp2", "-f", "mpeg"), ("mpeg2video", "mp2", "mpeg"),
    ),
    b"legacy/VTS_01_1.VOB": LegacyClip(
        ("-c:v", "mpeg2video", "-c:a", "ac3", "-f", "vob"), ("mpeg2video", "ac3", "mpeg"),
    ),
    b"legacy/open.ogv": LegacyClip(
        ("-c:v", "libtheora", "-c:a", "libvorbis", "-f", "ogg"), ("theora", "vorbis", "ogg"),
    ),
    b"legacy/movie.divx": LegacyClip(
        ("-c:v", "mpeg4", "-vtag", "DX50", "-c:a", "libmp3lame", "-f", "avi"),
        ("mpeg4", "mp3", "avi"),
    ),
}


def make_legacy_clip(roots: Path, relative: bytes) -> str:
    clip = LEGACY_CLIPS[relative]
    path = os.fsdecode(source_path(roots, relative))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    width, height = clip.size
    ffmpeg("-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=25:duration=2",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
           "-map", "0:v", "-map", "1:a", *clip.options, path)
    return path


def expect_h264_aac_mp4(path: Path, size: tuple[int, int]) -> None:
    data = path.read_bytes()
    assert output_magic_matches("mp4", data[:12])
    assert 0 <= data.find(b"moov") < data.find(b"mdat")
    probed = ffprobe_json(str(path))
    assert [
        (s["codec_name"], s["profile"], s["pix_fmt"], s["width"], s["height"])
        for s in probed["streams"] if s["codec_type"] == "video"
    ] == [("h264", "High", "yuv420p", *size)]
    assert [s["codec_name"] for s in probed["streams"] if s["codec_type"] == "audio"] == ["aac"]


@pytest.mark.parametrize("relative", list(LEGACY_CLIPS), ids=lambda path: path.decode())
def test_legacy_formats_are_probed_get_posters_and_transcode(
    tmp_path: Path, roots: Path, relative: bytes,
) -> None:
    """3G2, WMV, ASF, FLV, F4V, MPG, MPEG, VOB, OGV and DivX, through the real tools."""
    clip = LEGACY_CLIPS[relative]
    make_legacy_clip(roots, relative)

    backfill, directory = run_job(tmp_path, roots, make_job(roots, relative, "backfill",
                                                            limits=LIMITS))
    transcode, transcoded = run_job(tmp_path, roots, make_job(roots, relative, "transcode",
                                                              limits=LIMITS))

    assert backfill.status == "ok", backfill
    metadata = backfill.metadata
    assert metadata is not None
    assert (metadata.video_codec, metadata.audio_codec, metadata.container) == clip.codecs
    assert (metadata.width, metadata.height) == clip.size
    assert metadata.duration_ms is not None and 1800 <= metadata.duration_ms <= 2300
    assert metadata.captured_at == clip.captured
    assert metadata.has_location is False
    assert [(o.name, o.width, o.height) for o in backfill.outputs] == [
        ("thumbnail", *clip.size), ("poster", *clip.size),
    ]
    for output in backfill.outputs:
        data = (directory / output.filename).read_bytes()
        assert len(data) == output.bytes and output_magic_matches("webp", data[:12])
    assert transcode.status == "ok", transcode
    (output,) = transcode.outputs
    assert (output.name, output.format, output.width, output.height) == (
        "transcode", "mp4", *clip.size,
    )
    assert (transcoded / "transcode.mp4").stat().st_size == output.bytes
    expect_h264_aac_mp4(transcoded / "transcode.mp4", clip.size)
    assert os.listdir(transcoded) == ["transcode.mp4"]


def test_confined_children_decode_every_legacy_format(tmp_path: Path, roots: Path) -> None:
    """The real loop (Landlock, seccomp, ``fd:``) backfills and transcodes each legacy clip."""
    from aegis_media_processor.loop import Processor
    from aegis_media_processor.protocol import MAX_RESULT_BYTES, read_regular_file, write_job

    for relative in LEGACY_CLIPS:
        make_legacy_clip(roots, relative)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    operations: tuple[Op, ...] = ("backfill", "transcode")
    jobs = [
        make_job(roots, relative, op, limits=LIMITS)
        for relative in LEGACY_CLIPS for op in operations
    ]

    with Processor(str(scratch), str(roots), 2) as processor:
        processor.prepare()
        for job in jobs:
            write_job(str(scratch / "inbox"), job)
        processor.run(until_idle=True)

    for job in jobs:
        clip = LEGACY_CLIPS[job.input.path]
        directory = scratch / "done" / job.id
        result = decode_result(read_regular_file(str(directory / "result.json"),
                                                 MAX_RESULT_BYTES), job)
        assert result.status == "ok", (job.input.path, job.op, result)
        if job.op == "backfill":
            assert result.metadata is not None
            assert (result.metadata.video_codec, result.metadata.audio_codec,
                    result.metadata.container) == clip.codecs
            assert [o.name for o in result.outputs] == ["thumbnail", "poster"]
        else:
            assert [(o.name, o.width, o.height) for o in result.outputs] == [
                ("transcode", *clip.size),
            ]
            expect_h264_aac_mp4(directory / "transcode.mp4", clip.size)
    assert os.listdir(scratch / "work") == []


def test_a_raw_mpeg_elementary_stream_is_not_demuxed(tmp_path: Path, roots: Path) -> None:
    """``mpegvideo`` is not whitelisted: a raw stream named .mpg fails like an undecodable file.

    MPG files are MPEG-PS (or MPEG-1 system) streams; raw MPEG video uses .m1v and .m2v.
    """
    path = os.fsdecode(source_path(roots, b"legacy/raw.mpg"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=1",
           "-c:v", "mpeg1video", "-f", "mpeg1video", path)

    cases: tuple[tuple[Op, str], ...] = (
        ("probe", "decode_failed"), ("backfill", "decode_failed"), ("transcode", "tool_failed"),
    )
    for op, code in cases:
        result, directory = run_job(tmp_path, roots, make_job(roots, b"legacy/raw.mpg", op,
                                                              limits=LIMITS))
        assert (result.status, result.safe_error_code) == ("failed", code), (op, result)
        assert os.listdir(directory) == []
