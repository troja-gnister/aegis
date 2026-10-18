"""Private generated inputs and bounded diagnostics for disposable browser tests."""
from __future__ import annotations

import base64
import binascii
import contextlib
import functools
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from aegis_apps.common.redaction import redact
from aegisctl.container_engine import (
    compose_command,
    compose_environment,
    container_command,
    local_environment,
    selected_engine,
)
from aegisctl.container_launch import controlled_container_argv
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResource,
    ProjectResourceError,
    ProjectResourceRule,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_inventory,
    require_project_inventory,
    restart_policy_disabled,
)
from aegisctl.mounts import local_identity

REPOSITORY = Path(__file__).resolve().parents[1]
SECRET_NAMES = (
    "postgres-superuser-password", "db-migrator-password", "db-web-password",
    "db-operations-password", "db-indexer-password", "db-media-password",
    "django-secret-key", "auth-throttle-hmac-key", "e2e-alice-password",
    "e2e-bob-password", "e2e-admin-password",
)
GENERATED_NAMES = (
    "mounts.toml", "mounts.manifest.json", "compose.mounts.yaml",
    "mounts.gateway.attestation",
)
PROJECT = "aegis-phase1-e2e"
ROOT_NAMES = ("alice", "bob")
LEDGER_NAME = ".aegis-e2e-ledger.json"
SOURCE_MANIFEST = "sources.manifest.json"
# Owned synthetic sources: the two mounted roots plus one never-mounted sibling
# that only a symbolic link names. Nothing outside the private work directory.
SOURCE_DIRECTORIES = ("roots/alice", "roots/bob", "roots/alice-sibling")
SEALED_DIRECTORIES = ("roots/bob/locked",)
TIED_MTIME_NS = 1_577_836_800_000_000_000  # 2020-01-01T00:00:00Z
DIRECTORY_MTIME_NS = 1_600_000_000_000_000_000
BULK_FILES = 590
LARGE_FILE_BYTES = 1_500_000
INDEX_WAIT_SECONDS = 240
# Decodable synthetic media, generated at e2e time and never committed. The photos come from
# the locked Pillow and pillow-heif while the sources are created; the clips come from the
# aegis-media image the harness builds, after the build and before any mount or service.
# Distinctive coordinates: the browser journeys prove they never surface (spec M5).
GPS_LATITUDE = (12.0, 34.0, 56.789)
GPS_LONGITUDE = (98.0, 7.0, 54.321)
VIDEO_FIXTURES = ("clip.mp4", "clip-hevc.mov", "clip-legacy.wmv")
# Each clip's container signature: the ISO base media file type box, or the ASF header GUID.
_VIDEO_MAGIC = {
    "clip.mp4": (4, b"ftyp"),
    "clip-hevc.mov": (4, b"ftyp"),
    "clip-legacy.wmv": (0, bytes.fromhex("3026b2758e66cf11a6d900aa0062ce6c")),
}
DIGEST_FIXTURE = "roots/alice/photo.JPG"
MEDIA_IMAGE = "aegis-media"
MEDIA_FIXTURE_LABEL = "aegis.test.media-fixtures"
# Real clips are tens of KB; staying far below 1 MB keeps them out of the Phase 2A size filter.
MAX_VIDEO_BYTES = 256 << 10
MAX_VIDEO_OUTPUT_BYTES = 1 << 20
VIDEO_TIMEOUT_SECONDS = 300
MEDIA_WAIT_SECONDS = 180
# Every generated fixture (by display name) and the thumbnail states its real processing settles
# in; frontend/e2e/phase2c.spec.ts expects the same. The truncated JPEG's header decodes, so its
# probe succeeds and its thumbnail fails (and stays failed while it is retried, then quarantined).
MEDIA_SETTLED: dict[str, tuple[str, ...]] = {
    "photo.JPG": ("ready",),
    "phone.heic": ("ready",),
    "cover.png": ("ready",),
    "beach.jpg": ("ready",),
    "clip.mp4": ("ready",),
    "clip-hevc.mov": ("ready",),
    "clip-legacy.wmv": ("ready",),
    "corrupt.jpg": ("failed", "quarantined"),
}
_THUMBNAIL_STATES = ("pending", "ready", "failed", "unsupported", "quarantined")
_IMAGE_ID = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Runs in the media image with no network, no mount and a read-only root, as the fixed
# unprivileged user 10001:10001 (whatever UID the e2e build gave the image's files; it writes
# only its private 1777 tmpfs): ffmpeg's lavfi sources encode three 2 s clips into /tmp, and the
# clips leave only as one JSON document on stdout, which nothing else writes to. Single-threaded
# encoders keep the task count within the container's bound on any host. The WMV (WMV 8 and WMA
# 2 in ASF) is a legacy format no browser plays: it always plays through a transcode.
_VIDEO_SCRIPT = """\
import base64
import json
import os
import subprocess
import sys
import tempfile

PICTURE = "testsrc2=size=160x120:rate=15:duration=2"
TONE = "sine=frequency=440:sample_rate=48000:duration=2"
CLIPS = {
    "clip.mp4": [
        "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-threads", "1",
        "-c:a", "aac", "-metadata", "creation_time=2024-06-15T09:30:00Z",
        "-movflags", "+faststart", "-f", "mp4",
    ],
    "clip-hevc.mov": [
        "-c:v", "libx265", "-tag:v", "hvc1",
        "-x265-params", "log-level=error:pools=1:frame-threads=1",
        "-c:a", "aac", "-metadata", "creation_time=2023-11-20T18:00:00Z", "-f", "mov",
    ],
    "clip-legacy.wmv": ["-c:v", "wmv2", "-threads", "1", "-c:a", "wmav2", "-f", "asf"],
}
directory = tempfile.mkdtemp(dir="/tmp")
document = {}
for name, options in CLIPS.items():
    target = os.path.join(directory, name)
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-filter_threads", "1", "-f", "lavfi", "-i", PICTURE, "-f", "lavfi", "-i", TONE,
         "-map", "0:v", "-map", "1:a", "-pix_fmt", "yuv420p",
         "-b:a", "48k", "-ac", "1", "-shortest", *options, target],
        check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, timeout=120,
    )
    with open(target, "rb") as handle:
        document[name] = base64.b64encode(handle.read()).decode("ascii")
sys.stdout.write(json.dumps(document, sort_keys=True))
"""
_monotonic = time.monotonic
_sleep = time.sleep


def checked_directory(value: str) -> Path:
    path = Path(value)
    try:
        metadata = path.lstat()
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError("invalid E2E temporary directory") from exc
    if (
        not path.is_absolute() or path.resolve(strict=True) != path
        or parent != Path("/tmp").resolve()
        or not path.name.startswith(f"{PROJECT}.")
        or len(path.name.removeprefix(f"{PROJECT}.")) != 8
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("invalid E2E temporary directory")
    return path


def _identity(path: Path) -> list[int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("synthetic E2E input is missing") from exc
    kind = stat.S_IFMT(metadata.st_mode)
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise ValueError("synthetic E2E input is hardlinked")
    links = metadata.st_nlink if stat.S_ISREG(metadata.st_mode) else 0
    return [
        metadata.st_dev, metadata.st_ino, kind,
        metadata.st_uid, metadata.st_gid, links,
    ]


def _relative(path: Path, target: Path) -> str:
    try:
        relative = target.relative_to(path).as_posix()
    except ValueError as exc:
        raise ValueError("synthetic E2E ledger path is invalid") from exc
    return "." if relative == "." else relative


def _record(ledger: dict[str, Any], path: Path, target: Path) -> None:
    key = _relative(path, target)
    entries = ledger["entries"]
    if key in entries:
        raise ValueError("synthetic E2E creation ledger is invalid")
    parent_key = _relative(path, target.parent) if target != path else None
    if parent_key is not None and parent_key not in entries:
        raise ValueError("synthetic E2E creation parent was not recorded")
    entries[key] = _identity(target)


def _write_ledger(
    path: Path, ledger: dict[str, Any], *, descriptor: int | None = None,
) -> None:
    raw = (json.dumps(ledger, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    close = descriptor is None
    if descriptor is None:
        descriptor = os.open(path / LEDGER_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        if close:
            os.close(descriptor)


def _load_ledger(path: Path) -> dict[str, Any]:
    checked_directory(str(path))
    try:
        ledger = json.loads((path / LEDGER_NAME).read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("synthetic E2E creation ledger is invalid") from exc
    if (
        not isinstance(ledger, dict) or ledger.get("version") != 1
        or not isinstance(ledger.get("entries"), dict)
        or not isinstance(ledger.get("ancestors"), dict)
    ):
        raise ValueError("synthetic E2E creation ledger is invalid")
    return ledger


def _current_entries(path: Path) -> dict[str, list[int]]:
    current: dict[str, list[int]] = {}
    pending = [path]
    while pending:
        target = pending.pop()
        current[_relative(path, target)] = _identity(target)
        if stat.S_IFMT(target.lstat().st_mode) != stat.S_IFDIR:
            continue
        if _relative(path, target) in SEALED_DIRECTORIES:
            # Created empty and then made unreadable; the source snapshot proves
            # its mode and timestamp, and cleanup's rmdir refuses any content.
            continue
        try:
            children = list(os.scandir(target))
        except OSError as exc:
            raise ValueError("synthetic E2E inventory cannot be read") from exc
        pending.extend(Path(child.path) for child in children)
    return current


def _validate_ledger(
    path: Path, ledger: dict[str, Any], *, exact: bool = True,
) -> None:
    for name, expected in ledger["ancestors"].items():
        ancestor = Path(name)
        try:
            metadata = ancestor.lstat()
        except OSError as exc:
            raise ValueError("synthetic E2E ancestor was replaced") from exc
        if not stat.S_ISDIR(metadata.st_mode) or [metadata.st_dev, metadata.st_ino] != expected:
            raise ValueError("synthetic E2E ancestor was replaced")
    current = _current_entries(path)
    expected = ledger["entries"]
    changed = any(
        current.get(key) != identity for key, identity in expected.items()
    )
    if (exact and current != expected) or changed:
        raise ValueError("synthetic E2E inventory changed or contains unknown inputs")


def _utc_ns(*fields: int) -> int:
    from datetime import UTC, datetime

    return int(datetime(*fields, tzinfo=UTC).timestamp()) * 1_000_000_000


def _fixture_directory(ledger: dict[str, Any], path: Path, target: Path) -> None:
    target.mkdir(mode=0o755)
    # mkdir's mode is masked by the process umask (test-e2e.sh sets 077),
    # but the gateway (uid 101, cap_drop: ALL) must be able to read and
    # traverse these synthetic originals, so fix the mode explicitly.
    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(descriptor, 0o755)
    finally:
        os.close(descriptor)
    _record(ledger, path, target)


def _fixed_directory_mtime(target: Path) -> None:
    os.utime(target, ns=(DIRECTORY_MTIME_NS, DIRECTORY_MTIME_NS), follow_symlinks=False)


def _fixture_file(
    ledger: dict[str, Any], path: Path, target: Path, content: bytes,
    mtime_ns: int = TIED_MTIME_NS,
) -> None:
    descriptor = os.open(
        target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
    )
    try:
        os.fchmod(descriptor, 0o644)
        view = memoryview(content)
        while view:
            view = view[os.write(descriptor, view):]
        os.utime(descriptor, ns=(mtime_ns, mtime_ns))
    finally:
        os.close(descriptor)
    _record(ledger, path, target)


def _exif(*, captured: str, orientation: int | None = None, gps: bool = False) -> bytes:
    from PIL import ExifTags, Image

    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "Synthetic Camera Co."
    exif[ExifTags.Base.Model] = "Model 7"
    if orientation is not None:
        exif[ExifTags.Base.Orientation] = orientation
    # A wall-clock time without an offset: shown exactly as recorded.
    exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] = captured
    if gps:
        location = exif.get_ifd(ExifTags.IFD.GPSInfo)
        location[ExifTags.GPS.GPSLatitudeRef] = "N"
        location[ExifTags.GPS.GPSLatitude] = GPS_LATITUDE
        location[ExifTags.GPS.GPSLongitudeRef] = "E"
        location[ExifTags.GPS.GPSLongitude] = GPS_LONGITUDE
    return exif.tobytes()


@functools.cache
def _photo_fixtures() -> dict[str, bytes]:
    """Small decodable synthetic photos, keyed by their path below Alice's root.

    Encoded once per process: Pillow's JPEG and PNG output is reproducible, but an x265 HEIF
    encoding need not be bit-identical across encoder runs, and repeated preparations in one
    process (the harness tests) must produce identical manifests.
    """
    import io
    import random

    import pillow_heif
    from PIL import Image

    def two_tone(size: tuple[int, int], left: tuple[int, int, int],
                 right: tuple[int, int, int]) -> Image.Image:
        image = Image.new("RGB", size, right)
        image.paste(left, (0, 0, size[0] // 2, size[1]))
        return image

    def encoded(image: Image.Image, kind: str, **options: Any) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, format=kind, **options)
        return buffer.getvalue()

    pillow_heif.register_heif_opener()
    noise = Image.frombytes("RGB", (64, 64), random.Random(4096).randbytes(64 * 64 * 3))
    complete = encoded(noise, "JPEG", quality=90)
    return {
        # Stored landscape, displayed portrait: EXIF orientation 6, a capture time and GPS.
        "photo.JPG": encoded(
            two_tone((160, 120), (230, 20, 20), (20, 20, 230)), "JPEG", quality=85,
            exif=_exif(captured="2024:05:01 10:20:30", orientation=6, gps=True),
        ),
        "phone.heic": encoded(
            two_tone((128, 96), (20, 160, 60), (240, 200, 40)), "HEIF", quality=50,
            exif=_exif(captured="2023:12:24 18:30:00"),
        ),
        # The header decodes, the scan data stops halfway: its thumbnail fails.
        "corrupt.jpg": complete[: len(complete) // 2],
        # No EXIF at all: sorted and labeled by its modification time.
        "albums/cover.png": encoded(two_tone((96, 64), (250, 250, 250), (40, 40, 40)), "PNG"),
        "albums/2024/summer/beach.jpg": encoded(
            two_tone((128, 96), (250, 220, 150), (40, 140, 220)), "JPEG", quality=85,
            exif=_exif(captured="2024:07:14 12:00:00"),
        ),
    }


def _create_sources(ledger: dict[str, Any], path: Path) -> None:
    """Deterministic owned originals: >600 entries, nesting, ties, links and type hints.

    The photos are real, decodable images; the clips are added after the image build
    (:func:`media_fixtures`), before any mount preflight or service start.
    """
    alice, bob = path / "roots/alice", path / "roots/bob"
    photos = _photo_fixtures()
    sibling = path / "roots/alice-sibling"
    _fixture_directory(ledger, path, sibling)
    _fixture_file(ledger, path, sibling / "sibling-secret.txt", b"Never followed\n")
    for owner, root in (("alice", alice), ("bob", bob)):
        _fixture_file(
            ledger, path, root / f"{owner}-only.txt",
            f"Synthetic {owner} fixture\n".encode("ascii"),
        )
    for number in range(BULK_FILES):
        _fixture_file(
            ledger, path, alice / f"bulk-{number:04}.txt",
            f"bulk {number}\n".encode("ascii") * (1 + number % 7),
        )
    for name, content in (
        ("100%_done.txt", b"Literal percent prefix\n"),
        ("1000-other.txt", b"Wildcard decoy\n"),
        ("Tie.txt", b"Upper-case tie\n"),
        ("tie.txt", b"Lower-case tie\n"),
        ("README", b"No extension\n"),
        ("data.unknown", b"Literal unknown extension\n"),
        ("photo.JPG", photos["photo.JPG"]),
        ("phone.heic", photos["phone.heic"]),
        ("corrupt.jpg", photos["corrupt.jpg"]),
        ("notes.md", b"# Synthetic notes\n"),
        ("résumé.pdf", b"Synthetic document bytes\n"),
    ):
        _fixture_file(ledger, path, alice / name, content)
    pattern = bytes(range(256))
    _fixture_file(
        ledger, path, alice / "large.bin",
        (pattern * (LARGE_FILE_BYTES // len(pattern) + 1))[:LARGE_FILE_BYTES],
    )
    _fixture_file(ledger, path, alice / "old-report.txt", b"Old report\n",
                  _utc_ns(2001, 2, 3, 12, 0))
    # New York local 2026-03-07 23:30 EST and 2026-03-08 23:30 EDT.
    _fixture_file(ledger, path, alice / "dst-edge.txt", b"Before local day\n",
                  _utc_ns(2026, 3, 8, 4, 30))
    _fixture_file(ledger, path, alice / "dst-day.txt", b"Inside local day\n",
                  _utc_ns(2026, 3, 9, 3, 30))
    for directory in ("albums", "albums/2024", "albums/2024/summer", "empty-folder"):
        _fixture_directory(ledger, path, alice / directory)
    for name in ("albums/cover.png", "albums/2024/summer/beach.jpg"):
        _fixture_file(ledger, path, alice / name, photos[name])
    _fixture_file(ledger, path, alice / "albums/2024/summer/deep.txt", b"Nested fixture\n")
    link = alice / "link-outside"
    link.symlink_to("../alice-sibling/sibling-secret.txt")
    _record(ledger, path, link)
    locked = bob / "locked"
    _fixture_directory(ledger, path, locked)
    directories = sorted(
        (name for name, identity in ledger["entries"].items()
         if name.startswith(SOURCE_DIRECTORIES) and identity[2] == stat.S_IFDIR),
        key=lambda name: name.count("/"), reverse=True,
    )
    for name in directories:
        _fixed_directory_mtime(path / name)
    # Owner-unreadable, so the indexer records a real permission failure.
    descriptor = os.open(locked, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchmod(descriptor, 0)
    finally:
        os.close(descriptor)


def _snapshot_directory(descriptor: int, prefix: str, entries: dict[str, Any]) -> None:
    for name in sorted(os.listdir(descriptor)):
        key = f"{prefix}/{name}"
        info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            entries[key] = {"kind": "symlink", "target": os.readlink(name, dir_fd=descriptor)}
        elif stat.S_ISREG(info.st_mode):
            handle = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            try:
                opened = os.fstat(handle)
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise ValueError("synthetic E2E source fixture changed")
                digest = hashlib.sha256()
                while chunk := os.read(handle, 1 << 16):
                    digest.update(chunk)
            finally:
                os.close(handle)
            entries[key] = {
                "kind": "file", "mode": mode, "size": info.st_size,
                "mtime_ns": info.st_mtime_ns, "sha256": digest.hexdigest(),
            }
        elif stat.S_ISDIR(info.st_mode):
            listed = key not in SEALED_DIRECTORIES
            entries[key] = {
                "kind": "directory", "mode": mode, "mtime_ns": info.st_mtime_ns,
                "listed": listed,
            }
            if listed:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=descriptor,
                )
                try:
                    opened = os.fstat(child)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError("synthetic E2E source fixture changed")
                    _snapshot_directory(child, key, entries)
                finally:
                    os.close(child)
        else:
            entries[key] = {"kind": "special", "mode": mode}


def source_snapshot(path: Path) -> dict[str, Any]:
    """Descriptor-relative, no-follow snapshot of every owned source entry."""
    entries: dict[str, Any] = {}
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        base = os.open(path, flags)
        try:
            roots = os.open("roots", flags, dir_fd=base)
        finally:
            os.close(base)
        try:
            for directory in SOURCE_DIRECTORIES:
                name = directory.removeprefix("roots/")
                info = os.stat(name, dir_fd=roots, follow_symlinks=False)
                if not stat.S_ISDIR(info.st_mode):
                    raise ValueError("synthetic E2E source fixture changed")
                entries[directory] = {
                    "kind": "directory", "mode": stat.S_IMODE(info.st_mode),
                    "mtime_ns": info.st_mtime_ns, "listed": True,
                }
                source = os.open(name, flags, dir_fd=roots)
                try:
                    opened = os.fstat(source)
                    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError("synthetic E2E source fixture changed")
                    _snapshot_directory(source, directory, entries)
                finally:
                    os.close(source)
        finally:
            os.close(roots)
    except OSError as exc:
        raise ValueError("synthetic E2E source fixture changed") from exc
    return entries


def _source_manifest_bytes(path: Path) -> bytes:
    return json.dumps(
        {"version": 1, "entries": source_snapshot(path)},
        sort_keys=True, separators=(",", ":"),
    ).encode("ascii") + b"\n"


def _write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        view = view[os.write(descriptor, view):]
    os.fsync(descriptor)


def _write_source_manifest(ledger: dict[str, Any], path: Path) -> None:
    target = path / SOURCE_MANIFEST
    raw = _source_manifest_bytes(path)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, raw)
    finally:
        os.close(descriptor)
    _record(ledger, path, target)


def _rewrite_source_manifest(ledger: dict[str, Any], path: Path) -> None:
    """Re-record the manifest in place (the recorded file) once the last source exists."""
    target = path / SOURCE_MANIFEST
    raw = _source_manifest_bytes(path)
    descriptor = os.open(target, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        opened = os.fstat(descriptor)
        recorded = ledger["entries"].get(SOURCE_MANIFEST)
        if not isinstance(recorded, list) or [opened.st_dev, opened.st_ino] != recorded[:2]:
            raise ValueError("synthetic E2E source manifest is invalid")
        os.ftruncate(descriptor, 0)
        _write_all(descriptor, raw)
    finally:
        os.close(descriptor)


def _load_source_manifest(path: Path, ledger: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
    target = path / SOURCE_MANIFEST
    try:
        if ledger["entries"].get(SOURCE_MANIFEST) != _identity(target):
            raise ValueError("synthetic E2E source manifest is invalid")
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            raw = os.read(descriptor, 1 << 22)
        finally:
            os.close(descriptor)
        manifest = json.loads(raw)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("synthetic E2E source manifest is invalid") from exc
    if (
        not isinstance(manifest, dict) or manifest.get("version") != 1
        or not isinstance(manifest.get("entries"), dict) or len(raw) >= 1 << 22
    ):
        raise ValueError("synthetic E2E source manifest is invalid")
    return manifest["entries"], raw


def verify_sources(path: Path) -> dict[str, Any]:
    """Compare the pre-mount manifest with a fresh snapshot; never repairs or deletes."""
    ledger = _load_ledger(path)
    recorded, raw = _load_source_manifest(path, ledger)
    if source_snapshot(path) != recorded:
        raise ValueError("synthetic E2E source fixture changed")
    _validate_ledger(path, ledger)
    kinds = [value.get("kind") for value in recorded.values()]
    return {
        "entries": len(recorded),
        "files": kinds.count("file"),
        "directories": kinds.count("directory"),
        "symlinks": kinds.count("symlink"),
        "sealed": sum(1 for value in recorded.values() if value.get("listed") is False),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    }


def fixture_digest(path: Path) -> str:
    """The recorded SHA-256 of the photo whose download the browser journeys compare."""
    recorded, _ = _load_source_manifest(path, _load_ledger(path))
    entry = recorded.get(DIGEST_FIXTURE)
    digest = entry.get("sha256") if isinstance(entry, dict) else None
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError("synthetic E2E source manifest is invalid")
    return digest


def prepare(path: Path) -> None:
    path = checked_directory(str(path))
    if any(path.iterdir()):
        raise ValueError("synthetic E2E directory must be empty")
    root_metadata = path.lstat()
    tmp_metadata = path.parent.lstat()
    ledger = {
        "version": 1,
        "ancestors": {
            str(path.parent): [tmp_metadata.st_dev, tmp_metadata.st_ino],
            str(path): [root_metadata.st_dev, root_metadata.st_ino],
        },
        "entries": {".": _identity(path)},
        "resources": [],
    }
    ledger_path = path / LEDGER_NAME
    descriptor = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _record(ledger, path, ledger_path)
        secret_dir = path / "secrets"
        secret_dir.mkdir(mode=0o700)
        _record(ledger, path, secret_dir)
        for name in SECRET_NAMES:
            target = secret_dir / name
            with target.open("x", encoding="ascii") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(secrets.token_hex(32) + "\n")
            _record(ledger, path, target)
        root_dir = path / "roots"
        root_dir.mkdir(mode=0o700)
        _record(ledger, path, root_dir)
        lines = ["version = 1"]
        for name in ROOT_NAMES:
            source = root_dir / name
            _fixture_directory(ledger, path, source)
            lines.extend([
                "", "[[slots]]", f'slot_id = "e2e-{name}"',
                f"source = {json.dumps(str(source))}",
                f'container_path = "/srv/aegis/roots/e2e-{name}"',
                'mode = "read_only"',
                f"expected_identity = {json.dumps(local_identity(source))}",
            ])
        _create_sources(ledger, path)
        # Recorded before any mount preflight, render or container start.
        _write_source_manifest(ledger, path)
        config = path / "mounts.toml"
        with config.open("x", encoding="ascii") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write("\n".join(lines) + "\n")
        _record(ledger, path, config)
        _write_ledger(path, ledger, descriptor=descriptor)
    finally:
        os.close(descriptor)


def record_generated(path: Path) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger, exact=False)
    for name in GENERATED_NAMES[1:]:
        if name not in ledger["entries"]:
            _record(ledger, path, path / name)
    _write_ledger(path, ledger)
    _validate_ledger(path, ledger)


def _stored_resources(ledger: dict[str, Any]) -> ProjectInventory:
    try:
        resources = tuple(ProjectResource(**entry) for entry in ledger["resources"])
    except (KeyError, TypeError) as exc:
        raise ValueError("synthetic E2E resource ledger is invalid") from exc
    return ProjectInventory(PROJECT, tuple(sorted(resources)))


def resources_check(path: Path) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    require_project_inventory(_stored_resources(ledger))


def _resource_rules(operation: str, token: str) -> tuple[ProjectResourceRule, ...]:
    provenance = ("aegis.test.resource-token", token)
    if operation == "run-web":
        return (ProjectResourceRule("container", (
            ("com.docker.compose.service", "web"),
            ("com.docker.compose.oneoff", "True"),
            provenance,
        )),)
    if operation == "media-fixtures":
        # The one-off clip generator (media_fixtures), recorded only if --rm left it behind.
        return (ProjectResourceRule("container", (
            (MEDIA_FIXTURE_LABEL, "True"),
            provenance,
        )),)
    if operation != "up":
        raise ValueError("unknown E2E resource transition")
    services = (
        "postgres", "migrate", "web", "operations", "indexer", "media", "media-processor",
        "gateway",
    )
    networks = ("backend", "edge", "tls-hop")
    volumes = (
        "indexer-coordination", "postgres-data", "staging", "derivatives",
        "model-cache", "quarantine", "frontier-outbox", "media-scratch",
    )
    return (
        *(ProjectResourceRule("container", (
            ("com.docker.compose.service", service),
            ("com.docker.compose.oneoff", "False"),
            provenance,
        )) for service in services),
        *(ProjectResourceRule("network", (
            ("com.docker.compose.network", network),
            provenance,
        )) for network in networks),
        *(ProjectResourceRule("volume", (
            ("com.docker.compose.volume", volume),
            provenance,
        )) for volume in volumes),
    )


def resources_record(path: Path, operation: str) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    inventory = admit_project_transition(
        _stored_resources(ledger), capture_project_inventory(PROJECT),
        _resource_rules(operation, path.name),
    )
    ledger["resources"] = [
        {
            "kind": resource.kind, "handle": resource.handle,
            "immutable_id": resource.immutable_id, "fingerprint": resource.fingerprint,
        }
        for resource in inventory.resources
    ]
    _write_ledger(path, ledger)
    _validate_ledger(path, ledger)


def resources_cleanup(path: Path) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    cleanup_project_inventory(_stored_resources(ledger))
    ledger["resources"] = []
    _write_ledger(path, ledger)


def _media_image_id() -> str:
    """The exact local image the harness just built, so the run never resolves a tag later."""
    try:
        inspected = subprocess.run(
            container_command("image", "inspect", "--format", "{{.Id}}", MEDIA_IMAGE),
            check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=30, env=local_environment(os.environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("synthetic E2E media image is unavailable") from exc
    identifier = inspected.stdout.strip()
    if inspected.returncode or _IMAGE_ID.fullmatch(identifier) is None:
        raise ValueError("synthetic E2E media image is unavailable")
    return identifier


def _video_fixture_command(path: Path, image: str) -> list[str]:
    token = path.name
    command: list[str] = controlled_container_argv(
        container_command(
            # No --rm: an engine-side automatic removal could race the exact-identity record.
            # The container is recorded once it has stopped (or been abandoned) and removed by
            # its immutable ID (media_fixtures).
            "run", "--pull", "never",
            "--name", f"{PROJECT}-media-fixtures-{token.removeprefix(f'{PROJECT}.')}",
            # Project and provenance labels: resources-record admits it by these.
            "--label", f"com.docker.compose.project={PROJECT}",
            "--label", f"aegis.test.resource-token={token}",
            "--label", f"{MEDIA_FIXTURE_LABEL}=True",
            "--network", "none", "--read-only", "--user", "10001:10001",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "1g", "--cpus", "1", "--pids-limit", "256",
            "--tmpfs", "/tmp:size=64m,mode=1777,noexec,nosuid,nodev",
            "--entrypoint", "python",
            image, "-I", "-c", _VIDEO_SCRIPT,
        ),
        environment=os.environ, working_directory=REPOSITORY,
    )
    return command


def _decoded_videos(raw: bytes) -> dict[str, bytes]:
    if not raw or len(raw) > MAX_VIDEO_OUTPUT_BYTES:
        raise ValueError("synthetic E2E media fixture output is invalid")
    try:
        document = json.loads(raw)
        if not isinstance(document, dict) or sorted(document) != sorted(VIDEO_FIXTURES):
            raise ValueError
        videos = {
            name: base64.b64decode(document[name], validate=True) for name in VIDEO_FIXTURES
        }
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("synthetic E2E media fixture output is invalid") from exc
    for name, content in videos.items():
        offset, signature = _VIDEO_MAGIC[name]
        if (
            not 1024 <= len(content) <= MAX_VIDEO_BYTES
            or content[offset:offset + len(signature)] != signature
        ):
            raise ValueError("synthetic E2E media fixture output is invalid")
    return videos


def media_fixtures(path: Path) -> None:
    """Add the decodable clips, generated by the just-built media image, to Alice's root.

    One container with no network, no mount, a read-only root and bounded memory, tasks and CPU
    encodes them and prints them on stdout; only this process writes the source tree. Whatever
    happens (exit, failure, timeout of the client), the container is then recorded and removed
    by its immutable ID, so nothing can auto-remove it between the record and the removal. It
    runs after the build and before anything reads the sources (mount preflight, labels,
    services), so the source manifest is re-recorded here, once, in place.
    """
    ledger = _load_ledger(path)
    verify_sources(path)
    alice = path / "roots/alice"
    if (
        ledger["resources"]
        or any(name in ledger["entries"] for name in GENERATED_NAMES[1:])
        or any(_relative(path, alice / name) in ledger["entries"] for name in VIDEO_FIXTURES)
    ):
        raise ValueError("synthetic E2E media fixtures are out of order")
    image = _media_image_id()
    try:
        generated = subprocess.run(
            _video_fixture_command(path, image), check=False, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=VIDEO_TIMEOUT_SECONDS,
            env=local_environment(os.environ),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("synthetic E2E media fixture generation failed") from exc
    finally:
        resources_record(path, "media-fixtures")
        # Exact removal of what was recorded (a still-running container is forced to stop);
        # cleanup_project_inventory then proves the project has no resource left.
        resources_cleanup(path)
    if generated.returncode:
        raise ValueError("synthetic E2E media fixture generation failed")
    videos = _decoded_videos(generated.stdout)
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    for name in VIDEO_FIXTURES:
        _fixture_file(ledger, path, alice / name, videos[name])
    _fixed_directory_mtime(alice)
    _write_ledger(path, ledger)
    _rewrite_source_manifest(ledger, path)
    _validate_ledger(path, ledger)
    verify_sources(path)


def _targets(path: Path, ledger: dict[str, Any], names: list[str]) -> list[Path]:
    targets = [path / name for name in names]
    for target in targets:
        if ledger["entries"].get(_relative(path, target)) != _identity(target):
            raise ValueError("synthetic E2E input was replaced")
    return targets


def _run_preparation(path: Path, ledger: dict[str, Any], command: list[str]) -> None:
    _validate_ledger(path, ledger)
    try:
        result = subprocess.run(
            command, check=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("synthetic E2E runtime preparation failed") from exc
    _validate_ledger(path, ledger)
    if result.returncode:
        raise ValueError("synthetic E2E runtime preparation failed")


def prepare_sources(path: Path) -> None:
    if selected_engine() == "docker":
        return
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    verify_sources(path)
    mounted = tuple(f"roots/{name}" for name in ROOT_NAMES)
    names = sorted(
        name for name in ledger["entries"]
        if name in mounted or name.startswith(tuple(f"{root}/" for root in mounted))
    )
    _run_preparation(path, ledger, [
        "chcon", "--no-dereference", "--type", "container_file_t", "--",
        *(str(target) for target in _targets(path, ledger, names)),
    ])


def prepare_runtime(path: Path) -> None:
    if selected_engine() == "docker":
        return
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    secret_names = [f"secrets/{name}" for name in SECRET_NAMES]
    secrets_to_own = _targets(path, ledger, secret_names)
    for secret in secrets_to_own:
        if secret.lstat().st_mode & 0o777 != 0o600:
            raise ValueError("synthetic E2E secret has unsafe mode")
    manifest = _targets(path, ledger, ["mounts.manifest.json"])[0]
    attestation = _targets(path, ledger, ["mounts.gateway.attestation"])[0]
    uid = os.environ.get("AEGIS_UID", "")
    gid = os.environ.get("AEGIS_GID", "")
    if not uid.isdecimal() or not gid.isdecimal():
        raise ValueError("AEGIS_UID and AEGIS_GID must be decimal synthetic identities")
    label_targets = [*secrets_to_own, manifest, attestation]
    _run_preparation(path, ledger, [
        "chcon", "--no-dereference", "--type", "container_file_t", "--",
        *(str(target) for target in label_targets),
    ])
    ownership_targets = [*secrets_to_own, manifest]
    _validate_ledger(path, ledger)
    try:
        mapped = subprocess.run(
            container_command(
                "unshare", "chown", "--no-dereference", f"{uid}:{gid}", "--",
                *map(str, ownership_targets),
            ),
            check=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
        verified = subprocess.run(
            container_command(
                "unshare", "stat", "--format=%u:%g", "--",
                *map(str, ownership_targets),
            ),
            check=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("synthetic E2E runtime preparation failed") from exc
    if (
        mapped.returncode or verified.returncode
        or verified.stdout.splitlines() != [f"{uid}:{gid}"] * len(ownership_targets)
    ):
        raise ValueError("synthetic E2E ownership mapping failed")
    current = _current_entries(path)
    if current.keys() != ledger["entries"].keys():
        raise ValueError("synthetic E2E ownership inventory changed")
    target_names = {_relative(path, target) for target in ownership_targets}
    for name, previous in ledger["entries"].items():
        observed = current.get(name)
        if observed is None or (
            observed != previous and (
                name not in target_names
                or observed[:3] != previous[:3]
                or observed[5] != previous[5]
            )
        ):
            raise ValueError("synthetic E2E ownership inventory changed")
    for name in target_names:
        ledger["entries"][name] = current[name]
    _write_ledger(path, ledger)
    _validate_ledger(path, ledger)


def check_compose(config: dict[str, Any]) -> None:
    if config["name"] != PROJECT:
        raise ValueError("unexpected E2E project")
    for volume in config["volumes"].values():
        if volume.get("external") or not volume["name"].startswith(f"{PROJECT}_"):
            raise ValueError("E2E volumes must be disposable and project-scoped")
    services = config["services"]
    # Tests run only when invoked: a host boot or engine restart never starts one.
    if not all(restart_policy_disabled(service) for service in services.values()):
        raise ValueError("E2E services must not have a restart policy")
    if not config["networks"]["backend"]["internal"]:
        raise ValueError("E2E backend requires an internal network")
    for role in ("web", "migrate", "operations", "indexer", "media", "media-processor",
                 "gateway"):
        service = services.get(role)
        if service is None:
            raise ValueError("E2E services are incomplete")
        if not service["read_only"] or service["cap_drop"] != ["ALL"]:
            raise ValueError("E2E runtime isolation is missing")
        if role == "media-processor":
            # The media decoder has no network at all and no credential (spec M2).
            if service.get("network_mode") != "none" or service.get("networks"):
                raise ValueError("E2E media processor must have no network")
            if service.get("secrets") or service.get("environment"):
                raise ValueError("E2E media processor must hold no secret or setting")
        elif role != "gateway" and set(service["networks"]) != {"backend"}:
            raise ValueError("E2E backend must have no internet egress")
        roots = [
            volume for volume in service.get("volumes", [])
            if volume["target"].startswith("/srv/aegis/roots/")
        ]
        if role in ("web", "migrate"):
            if roots:
                raise ValueError("web/migrator cannot mount originals")
        elif len(roots) != 2 or any(v.get("read_only") is not True for v in roots):
            raise ValueError("every original mount must be read-only")
        for volume in service.get("volumes", []):
            if volume["type"] == "bind" and Path(volume["source"]).resolve() == REPOSITORY:
                raise ValueError("runtime cannot mount the repository")
    for role in ("postgres", "gateway"):
        if any(p.get("host_ip") != "127.0.0.1" for p in services[role]["ports"]):
            raise ValueError("E2E ports must bind loopback")


def sanitize_line(line: str) -> str:
    safe: dict[str, Any] = {"message": "Service event (details omitted)"}
    if len(line) <= 65_536:
        try:
            data = redact(json.loads(line))
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            if data.get("level") in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                safe["level"] = data["level"]
            if type(data.get("status")) is int and 100 <= data["status"] <= 599:
                safe["status"] = data["status"]
    return json.dumps(safe, separators=(",", ":"))


def cleanup(path: Path) -> None:
    # Sources must be provably unchanged before any recorded entry is removed;
    # a mismatch or unknown entry retains the complete fixture for diagnosis.
    verify_sources(path)
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    # Sealed directories cannot be listed, so remove them first: unknown content
    # makes rmdir refuse before any other recorded entry has been deleted.
    sealed = [name for name in SEALED_DIRECTORIES if name in ledger["entries"]]
    for name in sealed:
        try:
            (path / name).rmdir()
        except OSError as exc:
            raise ValueError(
                "synthetic E2E inventory changed or contains unknown inputs",
            ) from exc
    entries = [
        path / name for name in ledger["entries"]
        if name not in (".", LEDGER_NAME, *sealed)
    ]
    entries.sort(key=lambda target: len(target.parts), reverse=True)
    for target in entries:
        if stat.S_IFMT(target.lstat().st_mode) == stat.S_IFDIR:
            target.rmdir()
        else:
            target.unlink()
    (path / LEDGER_NAME).unlink()
    path.rmdir()


_INDEX_SESSIONS: dict[str, urllib.request.OpenerDirector] = {}


def _api(
    opener: urllib.request.OpenerDirector, base: str, method: str, route: str,
    body: object = None, token: str | None = None,
) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["X-CSRFToken"] = token
    request = urllib.request.Request(base + route, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=10) as response:
            status, raw = response.status, response.read(1 << 20)
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read(1 << 16)
    except (OSError, ValueError):
        raise ValueError("synthetic E2E index status request failed") from None
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def _csrf(opener: urllib.request.OpenerDirector, base: str) -> str:
    status, payload = _api(opener, base, "GET", "/api/v1/auth/csrf")
    token = payload.get("csrfToken") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(token, str):
        raise ValueError("synthetic E2E index status request failed")
    return token


def _session(base: str, username: str, password: str) -> urllib.request.OpenerDirector:
    """One reused, signed-in session per synthetic user (closed by _close_index_sessions)."""
    opener = _INDEX_SESSIONS.get(username)
    if opener is None:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )
        status, _ = _api(opener, base, "POST", "/api/v1/auth/login",
                         {"username": username, "password": password}, _csrf(opener, base))
        if status != 200:
            raise ValueError("synthetic E2E index status request failed")
        _INDEX_SESSIONS[username] = opener
    return opener


def _root_ids(opener: urllib.request.OpenerDirector, base: str) -> list[str]:
    status, payload = _api(opener, base, "GET", "/api/v1/roots")
    roots = payload.get("roots") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(roots, list):
        raise ValueError("synthetic E2E index status request failed")
    identifiers = []
    for root in roots:
        identifier = root.get("id") if isinstance(root, dict) else None
        if not isinstance(identifier, str) or len(identifier) != 36 or not all(
            character in "0123456789abcdef-" for character in identifier
        ):
            raise ValueError("synthetic E2E index status request failed")
        identifiers.append(identifier)
    return identifiers


def _index_states(base: str, username: str, password: str) -> list[dict[str, Any]]:
    """Every authorized root's real status; one reused session per synthetic user."""
    opener = _session(base, username, password)
    statuses = []
    for identifier in _root_ids(opener, base):
        status, value = _api(opener, base, "GET", f"/api/v1/roots/{identifier}/index-status")
        if status != 200 or not isinstance(value, dict):
            raise ValueError("synthetic E2E index status request failed")
        statuses.append(value)
    return statuses


def _settled(status: dict[str, Any], directories: int) -> bool:
    """Every owned directory reached a terminal state: complete, or a real permission failure."""
    try:
        completed = int(status.get("completedDirectories", ""))
        degraded = int(status.get("degradedDirectories", ""))
    except ValueError:
        return False
    if status.get("state") == "ready":
        return status.get("lastCompletedAt") is not None and (completed, degraded) == (
            directories, 0,
        )
    return status.get("state") == "degraded" and degraded > 0 and (
        completed + degraded == directories
    )


def _state_summary(statuses: list[dict[str, Any]]) -> str:
    known = ("not_indexed", "queued", "scanning", "ready", "degraded", "unavailable")
    return ",".join(
        f"{state if state in known else 'unknown'}:{completed}/{degraded}"
        for state, completed, degraded in (
            (status.get("state"), status.get("completedDirectories"),
             status.get("degradedDirectories")) for status in statuses
        )
        if str(completed).isdecimal() and str(degraded).isdecimal()
    )[:200]


def _close_index_sessions(base: str) -> None:
    while _INDEX_SESSIONS:
        _, opener = _INDEX_SESSIONS.popitem()
        with contextlib.suppress(ValueError):
            _api(opener, base, "POST", "/api/v1/auth/logout", token=_csrf(opener, base))


def _public_base() -> str:
    base = os.environ.get("AEGIS_PUBLIC_URL", "")
    if not base.startswith("http://127.0.0.1:") or not base.removeprefix(
        "http://127.0.0.1:",
    ).isdecimal():
        raise ValueError("synthetic E2E index status request failed")
    return base


def index_wait(path: Path) -> None:
    """Bounded wait for the real indexer, observed only through the status API."""
    path = checked_directory(str(path))
    recorded, _ = _load_source_manifest(path, _load_ledger(path))
    base = _public_base()
    deadline = _monotonic() + INDEX_WAIT_SECONDS
    try:
        for username, variable in (("alice", "E2E_ALICE_PASSWORD"), ("bob", "E2E_BOB_PASSWORD")):
            password = os.environ.get(variable, "")
            if not password:
                raise ValueError("synthetic E2E index status request failed")
            source = f"roots/{username}"
            directories = sum(
                1 for name, value in recorded.items()
                if value.get("kind") == "directory"
                and (name == source or name.startswith(f"{source}/"))
            )
            while True:
                statuses = _index_states(base, username, password)
                if len(statuses) == 1 and _settled(statuses[0], directories):
                    break
                if _monotonic() >= deadline:
                    # Fixed state vocabulary and counters only, never names or values.
                    print(f"AEGIS_E2E index-wait user={username} "
                          f"states={_state_summary(statuses)}", file=sys.stderr, flush=True)
                    raise ValueError("synthetic E2E index did not settle")
                _sleep(2)
    finally:
        _close_index_sessions(base)


def _media_states(base: str, password: str) -> dict[str, str]:
    """Thumbnail state per display name on Alice's one root, from her real timeline."""
    opener = _session(base, "alice", password)
    identifiers = _root_ids(opener, base)
    if len(identifiers) != 1:
        raise ValueError("synthetic E2E media status request failed")
    status, page = _api(
        opener, base, "GET", f"/api/v1/roots/{identifiers[0]}/timeline?limit=120",
    )
    items = page.get("items") if isinstance(page, dict) else None
    if status != 200 or not isinstance(items, list):
        raise ValueError("synthetic E2E media status request failed")
    states: dict[str, str] = {}
    for item in items:
        name = item.get("displayName") if isinstance(item, dict) else None
        media = item.get("media") if isinstance(item, dict) else None
        state = media.get("thumbnail") if isinstance(media, dict) else None
        if isinstance(name, str) and state in _THUMBNAIL_STATES:
            states[name] = state
    return states


def _media_summary(states: dict[str, str]) -> str:
    """Counts per fixed state vocabulary over the expected fixtures; never names."""
    observed = [states.get(name, "missing") for name in MEDIA_SETTLED]
    return ",".join(
        f"{state}:{observed.count(state)}"
        for state in (*_THUMBNAIL_STATES, "missing") if state in observed
    )


def media_wait(path: Path) -> None:
    """Bounded wait for the media backfill of every generated fixture, through the real API."""
    checked_directory(str(path))
    base = _public_base()
    password = os.environ.get("E2E_ALICE_PASSWORD", "")
    if not password:
        raise ValueError("synthetic E2E media status request failed")
    deadline = _monotonic() + MEDIA_WAIT_SECONDS
    try:
        while True:
            states = _media_states(base, password)
            if all(states.get(name) in settled for name, settled in MEDIA_SETTLED.items()):
                return
            if _monotonic() >= deadline:
                print(f"AEGIS_E2E media-wait states={_media_summary(states)}",
                      file=sys.stderr, flush=True)
                raise ValueError("synthetic E2E media did not settle")
            _sleep(2)
    finally:
        _close_index_sessions(base)


def controlled_compose(arguments: list[str]) -> int:
    environment = compose_environment(os.environ)
    command = controlled_container_argv(
        compose_command(*arguments, environment=environment), environment=environment,
        canonical_base=REPOSITORY / "compose.yaml", working_directory=REPOSITORY,
    )
    return subprocess.run(command, cwd=REPOSITORY, env=environment, check=False).returncode


DIRECTORY_ACTIONS = {
    "prepare": prepare,
    "media-fixtures": media_fixtures,
    "record-generated": record_generated,
    "prepare-sources": prepare_sources,
    "prepare-runtime": prepare_runtime,
    "resources-check": resources_check,
    "resources-cleanup": resources_cleanup,
    "index-wait": index_wait,
    "media-wait": media_wait,
    "cleanup": cleanup,
}
ACTIONS = (
    "controlled-compose", "check-compose", "sanitize-logs", "resources-record",
    "sources-verify", "fixture-digest", *DIRECTORY_ACTIONS,
)
# The exact constant refusals raised by this harness and aegisctl.container_resources.
# Any other message, even of these types, is never published.
_PUBLIC_REFUSALS: dict[type[BaseException], frozenset[str]] = {
    ValueError: frozenset((
        "AEGIS_UID and AEGIS_GID must be decimal synthetic identities",
        "E2E backend must have no internet egress",
        "E2E backend requires an internal network",
        "E2E media processor must have no network",
        "E2E media processor must hold no secret or setting",
        "E2E ports must bind loopback",
        "E2E runtime isolation is missing",
        "E2E services are incomplete",
        "E2E services must not have a restart policy",
        "E2E volumes must be disposable and project-scoped",
        "every original mount must be read-only",
        "invalid E2E temporary directory",
        "runtime cannot mount the repository",
        "synthetic E2E ancestor was replaced",
        "synthetic E2E creation ledger is invalid",
        "synthetic E2E creation parent was not recorded",
        "synthetic E2E directory must be empty",
        "synthetic E2E input is hardlinked",
        "synthetic E2E input is missing",
        "synthetic E2E input was replaced",
        "synthetic E2E inventory cannot be read",
        "synthetic E2E inventory changed or contains unknown inputs",
        "synthetic E2E ledger path is invalid",
        "synthetic E2E ownership inventory changed",
        "synthetic E2E ownership mapping failed",
        "synthetic E2E resource ledger is invalid",
        "synthetic E2E index did not settle",
        "synthetic E2E index status request failed",
        "synthetic E2E media did not settle",
        "synthetic E2E media fixture generation failed",
        "synthetic E2E media fixture output is invalid",
        "synthetic E2E media fixtures are out of order",
        "synthetic E2E media image is unavailable",
        "synthetic E2E media status request failed",
        "synthetic E2E runtime preparation failed",
        "synthetic E2E secret has unsafe mode",
        "synthetic E2E source fixture changed",
        "synthetic E2E source manifest is invalid",
        "unexpected E2E project",
        "unknown E2E resource transition",
        "unknown E2E support action",
        "web/migrator cannot mount originals",
    )),
    ProjectResourceError: frozenset((
        "disposable project already has resources",
        "exact project resource cleanup failed",
        "exact project resource cleanup was incomplete",
        "project cleanup resource kind is invalid",
        "project network inspection failed",
        "project resource fingerprint is invalid",
        "project resource identity is invalid",
        "project resource inspection failed",
        "project resource inventory is ambiguous",
        "project resource ownership changed",
        "project resource query failed",
        "project resource removal transition is invalid",
        "project resource transition changed project",
        "project resource transition is ambiguous",
        "project resources changed or contain unknown resources",
        "recorded project resource identity changed",
        "recorded project resource was removed or replaced",
        "required project resource was not removed",
        "unexpected project resource transition",
    )),
}


def _workflow_escape(value: str, *, property_value: bool = False) -> str:
    escaped = value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    if property_value:
        escaped = escaped.replace(":", "%3A").replace(",", "%2C")
    return escaped


def refusal_annotation(action: str, error: BaseException) -> str:
    """One bounded GitHub error annotation: the action and a fixed refusal only."""
    title = f"e2e-support {action if action in ACTIONS else 'unknown-action'}"
    message = str(error)
    detail = type(error).__name__
    if message in _PUBLIC_REFUSALS.get(type(error), frozenset()):
        detail = f"{detail}: {message}"
    else:
        detail = f"{detail} (details omitted)"
    return (
        f"::error title={_workflow_escape(title, property_value=True)}::"
        f"{_workflow_escape(detail)}"
    )


def main(arguments: list[str]) -> int:
    action = arguments[0] if arguments else ""
    try:
        if action == "controlled-compose":
            return controlled_compose(arguments[1:])
        if action == "check-compose":
            check_compose(json.load(sys.stdin))
        elif action == "sanitize-logs":
            for _ in range(4000):
                line = sys.stdin.readline(65_538)
                if not line:
                    break
                print(sanitize_line(line))
        elif action == "resources-record":
            resources_record(checked_directory(arguments[1]), arguments[2])
        elif action == "sources-verify":
            summary = verify_sources(checked_directory(arguments[1]))
            # A bounded report on stdout, outside the fixture that cleanup removes.
            print("AEGIS_E2E sources verified " + " ".join(
                f"{key}={summary[key]}" for key in (
                    "entries", "files", "directories", "symlinks", "sealed", "manifest_sha256",
                )
            ), flush=True)
        elif action == "fixture-digest":
            # Not secret: the browser runner compares a downloaded original with it.
            print(fixture_digest(checked_directory(arguments[1])), flush=True)
        elif action in DIRECTORY_ACTIONS:
            DIRECTORY_ACTIONS[action](checked_directory(arguments[1]))
        else:
            raise ValueError("unknown E2E support action")
    except Exception as error:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            # The harness captures fixture-digest's stdout; its refusal must stay visible.
            stream = sys.stderr if action == "fixture-digest" else sys.stdout
            print(refusal_annotation(action, error), file=stream, flush=True)
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
