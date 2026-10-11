"""Synthetic processor jobs, results and output files for the media worker tests.

Nothing here decodes media: the WebP and MP4 files are only headers plus padding, which is
all the worker ever reads.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Sequence
from pathlib import Path

from aegis_media_processor import protocol

PHOTO_METADATA = protocol.Metadata(
    width=4032, height=3024, orientation=1, captured_at="2024-05-01T10:30:00+02:00",
    captured_at_source="exif", camera_make="Synthetic", camera_model="Model One",
    duration_ms=None, video_codec=None, audio_codec=None, container=None, has_location=True,
)
VIDEO_METADATA = protocol.Metadata(
    width=1920, height=1080, orientation=None, captured_at="2024-05-01T08:30:00Z",
    captured_at_source="container", camera_make=None, camera_model=None, duration_ms=12_345,
    video_codec="h264", audio_codec="aac", container="mp4", has_location=False,
)


def synthetic_webp(width: int, height: int, *, padding: int = 64) -> bytes:
    """A RIFF/WEBP file whose VP8X chunk declares ``width`` x ``height``."""
    vp8x = (
        b"VP8X" + (10).to_bytes(4, "little") + b"\0\0\0\0"
        + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )
    padding += padding % 2
    body = b"WEBP" + vp8x + b"PAD " + padding.to_bytes(4, "little") + b"\0" * padding
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def synthetic_vp8(width: int, height: int) -> bytes:
    """A simple-format (lossy ``VP8 ``) WebP header plus padding."""
    frame = b"\x10\x02\x00" + b"\x9d\x01\x2a" + width.to_bytes(2, "little") + (
        height.to_bytes(2, "little")
    ) + b"\0" * 32
    chunk = b"VP8 " + len(frame).to_bytes(4, "little") + frame
    body = b"WEBP" + chunk
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def synthetic_vp8l(width: int, height: int) -> bytes:
    """A lossless (``VP8L``) WebP header plus padding."""
    bits = (width - 1) | ((height - 1) << 14)
    payload = b"\x2f" + bits.to_bytes(4, "little") + b"\0" * 31
    chunk = b"VP8L" + len(payload).to_bytes(4, "little") + payload
    body = b"WEBP" + chunk
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def synthetic_mp4(*, padding: int = 256) -> bytes:
    ftyp = (24).to_bytes(4, "big") + b"ftypisom" + b"\0\0\x02\0" + b"isomiso2"
    return ftyp + (8 + padding).to_bytes(4, "big") + b"free" + b"\0" * padding


def make_job(
    op: protocol.Op = "thumbnail",
    *,
    kind: protocol.MediaKind | None = None,
    path: bytes | None = None,
    output_bytes: int = 8 << 20,
    lane: protocol.Lane = "background",
) -> protocol.Job:
    media_kind = kind or ("photo" if op in ("thumbnail", "preview") else "video")
    if path is None:
        path = b"album/photo.jpg" if media_kind == "photo" else b"album/clip.mp4"
    return protocol.check_job(protocol.Job(
        id=secrets.token_hex(16), op=op, kind=media_kind, lane=lane,
        input=protocol.SourceInput("synthetic", path, 7, 1000, 4096, 1_700_000_000_000_000_000),
        params=protocol.DEFAULT_PARAMS[op],
        limits=protocol.Limits(
            wall_seconds=60, cpu_seconds=120, memory_bytes=1 << 30, output_bytes=output_bytes,
        ),
    ))


def default_outputs(job: protocol.Job) -> list[tuple[str, bytes, int, int]]:
    """``(name, data, width, height)`` for every output the job's operation writes."""
    outputs: list[tuple[str, bytes, int, int]] = []
    for spec in protocol.OP_OUTPUTS[job.op]:
        if spec.format == "mp4":
            outputs.append((spec.name, synthetic_mp4(), 1920, 1080))
        elif spec.name == "thumbnail":
            outputs.append((spec.name, synthetic_webp(400, 300), 400, 300))
        else:
            outputs.append((spec.name, synthetic_webp(2048, 1536), 2048, 1536))
    return outputs


def publish_result(
    done: Path | str,
    job: protocol.Job,
    *,
    status: protocol.Status = "ok",
    code: str | None = None,
    metadata: protocol.Metadata | None = None,
    outputs: Sequence[tuple[str, bytes, int, int]] | None = None,
    raw: bytes | None = None,
) -> Path:
    """Write ``done/<id>/`` as the processor would (or with a forged ``raw`` result)."""
    directory = Path(done) / job.id
    directory.mkdir(mode=0o770)
    if status == "ok" and job.op == "probe" and metadata is None:
        metadata = PHOTO_METADATA if job.kind == "photo" else VIDEO_METADATA
    files = (list(default_outputs(job)) if outputs is None else list(outputs)) if (
        status == "ok"
    ) else []
    declared: list[protocol.Output] = []
    for name, data, width, height in files:
        spec = next(s for s in protocol.OP_OUTPUTS[job.op] if s.name == name)
        (directory / spec.filename).write_bytes(data)
        declared.append(protocol.Output(name, spec.format, len(data), width, height))
    if raw is None:
        raw = protocol.encode_result(
            protocol.Result(job.id, status, code, metadata if status == "ok" else None,
                            tuple(declared)),
            job,
        )
    (directory / protocol.RESULT_FILE).write_bytes(raw)
    return directory


def scratch_tree(root: Path) -> Path:
    for name in (protocol.INBOX, protocol.DONE):
        (root / name).mkdir(mode=0o770, exist_ok=True)
    return root


def queued_jobs(scratch: Path) -> list[protocol.Job]:
    inbox = scratch / protocol.INBOX
    jobs = []
    for name in sorted(os.listdir(inbox)):
        if protocol.job_id_from_filename(name) is not None:
            jobs.append(protocol.decode_job((inbox / name).read_bytes()))
    return jobs
