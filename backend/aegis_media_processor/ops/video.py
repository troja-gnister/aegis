"""ffprobe and ffmpeg: video probes, poster frames and transcodes (M4).

The decoding child runs ffmpeg and ffprobe as its own sandboxed children, with argument
arrays, the scrubbed environment, the job's limits and (inherited) the job's Landlock ruleset.
They read the original only through the descriptor the loop opened and fingerprint-checked,
passed to them unchanged, with ffmpeg's ``fd:`` protocol (``-fd <n> -i fd:``, FFmpeg 6.1 and
later). Unlike ``/proc/self/fd/<n>``, which Landlock checks against the original's path and
refuses, ``fd:`` resolves no path; unlike ``pipe:``, it is seekable for regular files, which MP4
and MOV files with their index at the end need. The descriptor is rewound before each run,
because ffmpeg duplicates it and the duplicate shares the file offset.

Every input is opened with ``-protocol_whitelist fd`` (no file, network or other protocol) and
``-format_whitelist`` naming only the demuxers of the M4 video formats, so a crafted file cannot
be probed as a DASH manifest, playlist or concat script that would read other files; none of
the whitelisted demuxers opens another file. Demuxers are chosen by content, never by extension
(to ffprobe a WMV is ``asf``, an MPG is ``mpeg`` and a 3G2 is ``mov``). The mov demuxer's
``enable_drefs`` stays at its default (off); it is not passed explicitly because an
input option the chosen demuxer does not consume is an error. Outputs drop all container
metadata (``-map_metadata -1``), so location tags never reach a derivative, and the first video
stream that is not cover art (``0:V:0``) is the one used.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from typing import Final, cast

from .. import sandbox
from ..protocol import OP_OUTPUTS, Limits, Output, PosterParams, TranscodeParams
from . import OperationError
from .image import render_frame

PROTOCOL_WHITELIST: Final = "fd"
# The mov (MP4, M4V, MOV, 3GP, 3G2, F4V), Matroska (MKV, WebM), AVI (AVI, DivX), MPEG-TS (MTS,
# M2TS), ASF (WMV, ASF), FLV, MPEG-PS (MPG, MPEG, VOB) and Ogg (OGV) demuxers. Raw elementary
# streams (``mpegvideo``) are not on it.
FORMAT_WHITELIST: Final = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts,asf,flv,mpeg,ogg"
OUTPUT_FORMAT_WHITELIST: Final = "mov,mp4,m4a,3gp,3g2,mj2"
FRAME_FILE: Final = "frame.png"
# Bounded thread counts keep thread stacks and arenas within RLIMIT_AS (see sandbox.py).
POSTER_THREADS: Final = "2"
TRANSCODE_THREADS: Final = "4"
MAX_PROBE_JSON_BYTES: Final = 1 << 20
_COMMON: Final = ("-nostdin", "-hide_banner", "-loglevel", "error")


def tool(name: str) -> str:
    path = shutil.which(name, path=sandbox.SAFE_PATH)
    if path is None:
        raise OperationError("failed", "tool_missing")
    return path


def input_options(descriptor: int) -> list[str]:
    if type(descriptor) is not int or descriptor < 3:
        raise ValueError("invalid input descriptor")
    return [
        "-protocol_whitelist", PROTOCOL_WHITELIST,
        "-format_whitelist", FORMAT_WHITELIST,
        "-fd", str(descriptor), "-i", "fd:",
    ]


def poster_filter(max_edge: int) -> str:
    """Square pixels, then fit within ``max_edge`` (never enlarged)."""
    return (
        "scale=w='iw*sar':h='ih',setsar=1,"
        f"scale=w='min({max_edge},iw)':h='min({max_edge},ih)'"
        ":force_original_aspect_ratio=decrease"
    )


def transcode_filter(params: TranscodeParams) -> str:
    """Fit within long edge by short edge in either orientation, even dimensions, no enlarging."""
    long_edge, short_edge = params.long_edge, params.short_edge
    return (
        f"scale=w='if(gte(iw,ih),min({long_edge},iw),min({short_edge},iw))'"
        f":h='if(gte(iw,ih),min({short_edge},ih),min({long_edge},ih))'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )


def poster_arguments(ffmpeg: str, descriptor: int, seek_ms: int, max_edge: int,
                     frame: str) -> list[str]:
    """One frame at ``seek_ms`` (0: the first frame) as an RGB PNG within ``max_edge``."""
    seek = ["-ss", f"{seek_ms / 1000:.3f}"] if seek_ms else []
    return [
        ffmpeg, *_COMMON, "-threads", POSTER_THREADS, *seek, *input_options(descriptor),
        "-map", "0:V:0", "-an", "-sn", "-dn", "-map_metadata", "-1",
        "-filter_threads", POSTER_THREADS, "-vf", poster_filter(max_edge),
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "image2", "-c:v", "png",
        "-threads", POSTER_THREADS, "-update", "1", "-n", "file:" + frame,
    ]


def transcode_arguments(ffmpeg: str, descriptor: int, params: TranscodeParams,
                        target: str) -> list[str]:
    """H.264 High yuv420p and AAC in a faststart MP4 with no container metadata."""
    return [
        ffmpeg, *_COMMON, "-threads", TRANSCODE_THREADS, *input_options(descriptor),
        "-map", "0:V:0", "-map", "0:a:0?", "-map_metadata", "-1", "-map_chapters", "-1",
        "-sn", "-dn", "-filter_threads", TRANSCODE_THREADS, "-vf", transcode_filter(params),
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-threads", TRANSCODE_THREADS, "-c:a", "aac", "-b:a", f"{params.audio_kbps}k",
        "-movflags", "+faststart", "-f", "mp4", "-n", "file:" + target,
    ]


def _limits(limits: Limits, deadline: float, stdout_bytes: int) -> sandbox.ChildLimits:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise OperationError("timeout", "timeout")
    return sandbox.ChildLimits(
        memory_bytes=limits.memory_bytes,
        cpu_seconds=limits.cpu_seconds,
        wall_seconds=remaining,
        output_bytes=limits.output_bytes,
        nice=0,  # the tool child already runs at the processor's lowered priority
        stdout_bytes=stdout_bytes,
    )


def run_tool(
    argv: Sequence[str],
    directory: str,
    limits: Limits,
    deadline: float,
    *,
    pass_fds: tuple[int, ...] = (),
    stdout_bytes: int = 4096,
) -> sandbox.Completed:
    """Run ffmpeg or ffprobe; deadlines and limits become protocol outcomes."""
    for descriptor in pass_fds:
        os.lseek(descriptor, 0, os.SEEK_SET)
    completed = sandbox.run(
        argv, _limits(limits, deadline, stdout_bytes), cwd=directory,
        environment=sandbox.scrubbed_environment({"TMPDIR": directory}), pass_fds=pass_fds,
    )
    code = completed.failure_code
    if code == "timeout":
        raise OperationError("timeout", "timeout")
    if code in ("cpu_limit", "output_limit"):
        raise OperationError("failed", code)
    return completed


def _refuse_constant(value: str) -> object:
    raise ValueError(value)


def _probe_json(completed: sandbox.Completed) -> dict[str, object]:
    if completed.returncode != 0 or completed.stdout_overflow:
        raise OperationError("failed", "decode_failed")
    try:
        document = json.loads(completed.stdout.decode("utf-8", "strict"),
                              parse_constant=_refuse_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise OperationError("failed", "decode_failed") from None
    if not isinstance(document, dict):
        raise OperationError("failed", "decode_failed")
    return cast(dict[str, object], document)


def ffprobe_document(
    descriptor: int, directory: str, limits: Limits, deadline: float,
) -> dict[str, object]:
    """ffprobe's format and stream JSON for the descriptor (untrusted; callers validate)."""
    argv = [
        tool("ffprobe"), "-hide_banner", "-v", "error", *input_options(descriptor),
        "-print_format", "json", "-show_format", "-show_streams",
    ]
    return _probe_json(run_tool(
        argv, directory, limits, deadline, pass_fds=(descriptor,),
        stdout_bytes=MAX_PROBE_JSON_BYTES,
    ))


def _regular_size(path: str) -> int:
    try:
        observed = os.lstat(path)
    except FileNotFoundError:
        return 0
    return observed.st_size if stat.S_ISREG(observed.st_mode) else 0


def _remove(path: str) -> None:
    with suppress(FileNotFoundError):
        os.unlink(path)


def make_poster(
    descriptor: int, directory: str, params: PosterParams, limits: Limits, deadline: float,
) -> tuple[Output, ...]:
    """A frame at ``frame_ms`` (the first frame if there is none), at both poster sizes."""
    frame = os.path.join(directory, FRAME_FILE)
    largest = max(params.thumbnail_edge, params.poster_edge)
    ffmpeg = tool("ffmpeg")
    try:
        for seek_ms in dict.fromkeys((params.frame_ms, 0)):
            _remove(frame)
            argv = poster_arguments(ffmpeg, descriptor, seek_ms, largest, frame)
            completed = run_tool(argv, directory, limits, deadline, pass_fds=(descriptor,))
            if completed.returncode == 0 and _regular_size(frame) > 0:
                break
        else:
            raise OperationError("failed", "decode_failed")
        thumbnail, poster = OP_OUTPUTS["poster"]
        return render_frame(frame, directory, (
            (thumbnail, params.thumbnail_edge, params.thumbnail_quality),
            (poster, params.poster_edge, params.poster_quality),
        ))
    finally:
        _remove(frame)


def _first_video_size(document: Mapping[str, object]) -> tuple[int, int, str] | None:
    streams = document.get("streams")
    for stream in streams if isinstance(streams, list) else []:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        width, height, codec = stream.get("width"), stream.get("height"), stream.get("codec_name")
        if type(width) is int and type(height) is int and isinstance(codec, str):
            return width, height, codec
    return None


def transcode(
    descriptor: int, directory: str, params: TranscodeParams, limits: Limits, deadline: float,
) -> Output:
    """H.264 High (yuv420p, at most 1080p) and AAC in an MP4 with faststart and no metadata."""
    (spec,) = OP_OUTPUTS["transcode"]
    target = os.path.join(directory, spec.filename)
    argv = transcode_arguments(tool("ffmpeg"), descriptor, params, target)
    completed = run_tool(argv, directory, limits, deadline, pass_fds=(descriptor,))
    size = _regular_size(target)
    if completed.returncode != 0 or size == 0:
        raise OperationError("failed", "tool_failed")
    verification = run_tool([
        tool("ffprobe"), "-hide_banner", "-v", "error",
        "-protocol_whitelist", "file", "-format_whitelist", OUTPUT_FORMAT_WHITELIST,
        "-i", "file:" + target, "-print_format", "json", "-show_streams",
    ], directory, limits, deadline, stdout_bytes=MAX_PROBE_JSON_BYTES)
    observed = _first_video_size(_probe_json(verification))
    if observed is None or observed[2] != "h264":
        raise OperationError("failed", "tool_failed")
    width, height, _ = observed
    return Output(name=spec.name, format=spec.format, bytes=size, width=width, height=height)
