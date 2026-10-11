"""Builders for the media worker integration tests: a synthetic library and a fake processor.

The fake processor never decodes anything: it reads job files from ``inbox/`` and publishes
results, with header-only WebP and MP4 outputs, into ``done/`` exactly as the real loop does.
Names in the synthetic catalog are random, so a test can check that none of them ever
reaches a heartbeat.
"""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.media.config import MediaPolicy
from aegis_apps.media.dispatch import Scratch
from aegis_apps.media.keys import fingerprint
from aegis_apps.media.publish import DerivativeStore
from aegis_apps.media.worker import MediaWorker
from aegis_apps.operations.management.commands.run_role import WorkerIdentity
from aegis_apps.operations.models import Operation
from aegis_apps.operations.services import create_operation
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from aegis_media_processor import protocol

WORKER_ID = "20000000-0000-4000-8000-000000000002"
SCHEMA_ID = "sha256:" + "c" * 64
MTIME_NS = 1_714_559_400_000_000_000
PREVIEW = int(Permission.BROWSE | Permission.PREVIEW)

PHOTO_METADATA = protocol.Metadata(
    width=4032, height=3024, orientation=6, captured_at="2024-05-01T10:30:00+02:00",
    captured_at_source="exif", camera_make="Synthetic", camera_model="Model One",
    duration_ms=None, video_codec=None, audio_codec=None, container=None, has_location=True,
)
VIDEO_METADATA = protocol.Metadata(
    width=1920, height=1080, orientation=None, captured_at="2024-05-01T08:30:00Z",
    captured_at_source="container", camera_make=None, camera_model=None, duration_ms=12_345,
    video_codec="h264", audio_codec="aac", container="mp4", has_location=False,
)


def synthetic_webp(width: int, height: int, *, padding: int = 64) -> bytes:
    vp8x = (
        b"VP8X" + (10).to_bytes(4, "little") + b"\0\0\0\0"
        + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    )
    padding += padding % 2
    body = b"WEBP" + vp8x + b"PAD " + padding.to_bytes(4, "little") + b"\0" * padding
    return b"RIFF" + len(body).to_bytes(4, "little") + body


def synthetic_mp4(*, padding: int = 256) -> bytes:
    ftyp = (24).to_bytes(4, "big") + b"ftypisom" + b"\0\0\x02\0" + b"isomiso2"
    return ftyp + (8 + padding).to_bytes(4, "big") + b"free" + b"\0" * padding


def default_outputs(job: protocol.Job) -> list[tuple[str, bytes, int, int]]:
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
    done: Path,
    job: protocol.Job,
    *,
    status: protocol.Status = "ok",
    code: str | None = None,
    metadata: protocol.Metadata | None = None,
    outputs: Sequence[tuple[str, bytes, int, int]] | None = None,
    raw: bytes | None = None,
) -> Path:
    directory = done / job.id
    directory.mkdir(mode=0o770)
    if status == "ok" and job.op == "probe" and metadata is None:
        metadata = PHOTO_METADATA if job.kind == "photo" else VIDEO_METADATA
    files = (default_outputs(job) if outputs is None else list(outputs)) if status == "ok" else []
    declared: list[protocol.Output] = []
    for name, data, width, height in files:
        spec = next(s for s in protocol.OP_OUTPUTS[job.op] if s.name == name)
        (directory / spec.filename).write_bytes(data)
        declared.append(protocol.Output(name, spec.format, len(data), width, height))
    if raw is None:
        raw = protocol.encode_result(protocol.Result(
            job.id, status, code, metadata if status == "ok" else None, tuple(declared),
        ), job)
    (directory / protocol.RESULT_FILE).write_bytes(raw)
    return directory


type Behaviour = Callable[[protocol.Job], Mapping[str, Any]]


@dataclass(slots=True)
class FakeProcessor:
    """Claims every queued job (oldest name first) and publishes a result for it."""

    scratch: Path
    behaviour: Behaviour = field(default=lambda job: {})
    served: list[protocol.Job] = field(default_factory=list)

    def serve(self, limit: int = 64) -> list[protocol.Job]:
        inbox = self.scratch / protocol.INBOX
        done = self.scratch / protocol.DONE
        served: list[protocol.Job] = []
        for name in sorted(os.listdir(inbox))[:limit]:
            if protocol.job_id_from_filename(name) is None:
                continue
            job = protocol.decode_job((inbox / name).read_bytes())
            (inbox / name).unlink()
            publish_result(done, job, **self.behaviour(job))
            served.append(job)
        self.served.extend(served)
        return served

    def claim_only(self) -> list[protocol.Job]:
        """Take every queued job and never answer (a hung or dead processor)."""
        inbox = self.scratch / protocol.INBOX
        taken = []
        for name in sorted(os.listdir(inbox)):
            if protocol.job_id_from_filename(name) is not None:
                taken.append(protocol.decode_job((inbox / name).read_bytes()))
                (inbox / name).unlink()
        return taken


@dataclass(slots=True)
class ManualClock:
    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(slots=True)
class Library:
    """A synthetic root in the configured manifest, with a directory of media files."""

    root: Root
    anchor: CatalogEntry
    album: CatalogEntry
    factory: Callable[..., CatalogEntry]
    inodes: int = 1000

    def photo(self, raw: bytes | None = None, **kwargs: Any) -> CatalogEntry:
        return self.file(raw or f"{secrets.token_hex(6)}.jpg".encode(), **kwargs)

    def video(self, raw: bytes | None = None, **kwargs: Any) -> CatalogEntry:
        return self.file(raw or f"{secrets.token_hex(6)}.mp4".encode(), **kwargs)

    def file(self, raw: bytes, **kwargs: Any) -> CatalogEntry:
        self.inodes += 1
        values: dict[str, Any] = {
            "size": 4096, "mtime_ns": MTIME_NS, "ctime_ns": MTIME_NS, "device": 7,
            "inode": self.inodes,
        }
        values.update(kwargs)
        parent = values.pop("parent", self.album)
        entry = self.factory(root=self.root, raw=raw, parent=parent, **values)
        return entry

    def fingerprint(self, entry: CatalogEntry) -> str:
        entry.refresh_from_db()
        assert entry.device is not None and entry.inode is not None
        assert entry.size is not None and entry.mtime_ns is not None
        return fingerprint(
            entry.root_id, entry.id, entry.device, entry.inode, entry.size, entry.mtime_ns,
        )

    def requester(self, permissions: int = PREVIEW) -> User:
        user = User.objects.create_user(username=f"media-{uuid.uuid4().hex}")
        RootGrant.objects.create(root=self.root, user=user, permissions=permissions)
        user.refresh_from_db()
        return user

    def request(self, user: User, entry: CatalogEntry, kind: str = "preview") -> Operation:
        from aegis_apps.media.models import MediaAsset

        asset = MediaAsset.objects.get(pk=entry.pk)
        return create_operation(
            actor=user,
            request_id=f"media_{secrets.token_hex(16)}",
            kind=f"media.{kind}",
            intent={"roots": [{"id": str(self.root.pk), "permissions": PREVIEW}]},
            target_role="media",
            payload={
                "v": 1, "rootId": str(self.root.pk), "entryId": str(entry.pk),
                "fingerprint": asset.fingerprint,
            },
        )


def make_library(browse: Any) -> Library:
    """A library over ``browse_fixture`` (its root is in the configured mount manifest)."""
    album = browse.entry(b"album", kind="directory")
    return Library(root=browse.root, anchor=browse.anchor, album=album, factory=browse.factory)


def worker_identity() -> WorkerIdentity:
    digest = os.environ.get("AEGIS_MOUNT_MANIFEST_SHA256") or "unconfigured:v1"
    return WorkerIdentity(
        role="media", worker_id=WORKER_ID, release_id="release-media",
        schema_identity=SCHEMA_ID, manifest_identity=digest,
    )


def scratch_and_store(tmp_path: Path) -> tuple[Path, Path]:
    scratch = tmp_path / "scratch"
    derivatives = tmp_path / "derivatives"
    scratch.mkdir(mode=0o770)
    derivatives.mkdir(mode=0o770)
    return scratch, derivatives


def make_worker(
    tmp_path: Path,
    *,
    clock: ManualClock | None = None,
    stop: Callable[[], bool] = lambda: False,
    environ: Mapping[str, str] | None = None,
) -> tuple[MediaWorker, FakeProcessor, ManualClock]:
    scratch, derivatives = scratch_and_store(tmp_path)
    selected = clock or ManualClock()
    worker = MediaWorker(
        worker_identity(),
        stop_requested=stop,
        policy=MediaPolicy.from_environ(dict(environ or {})),
        scratch=Scratch(str(scratch)),
        store=DerivativeStore(str(derivatives)),
        sleep=lambda seconds: None,
        monotonic=selected,
        uniform=lambda: 0.5,
    )
    return worker, FakeProcessor(scratch), selected


def run(
    worker: MediaWorker, processor: FakeProcessor, clock: ManualClock, ticks: int = 1,
) -> None:
    """Tick, let the processor answer, tick again to collect; ``ticks`` times."""
    for _ in range(ticks):
        worker.tick()
        processor.serve()
        clock.advance(1.0)
        worker.tick()
        clock.advance(1.0)


def drive(
    worker: MediaWorker,
    processor: FakeProcessor,
    clock: ManualClock,
    until: Callable[[], bool],
    *,
    limit: int = 30,
) -> None:
    """Tick and serve until ``until()`` holds (then one more tick), at most ``limit`` times."""
    for _ in range(limit):
        worker.tick()
        processor.serve()
        clock.advance(1.0)
        if until():
            worker.tick()
            return
    raise AssertionError("the worker did not reach the expected state")
