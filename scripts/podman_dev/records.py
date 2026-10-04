"""Exact identities recorded for one helper session, persisted as private JSON."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

RECORD_NAME = "session.json"
TEMPORARY_NAME = f".{RECORD_NAME}.tmp"


class SessionError(RuntimeError):
    """A session cannot start, verify or stop safely; diagnostics are preserved."""


@dataclass(frozen=True)
class PathIdentity:
    device: int
    inode: int

    @classmethod
    def of(cls, path: Path) -> PathIdentity:
        metadata = path.lstat()
        return cls(metadata.st_dev, metadata.st_ino)


@dataclass
class ApiRecord:
    pid: int
    start_time: int
    command: list[str]
    socket: str
    directory_identity: PathIdentity
    socket_identity: PathIdentity | None = None


@dataclass(frozen=True)
class WebkitRecord:
    container_id: str
    image_id: str
    port: int
    token: str

    @property
    def endpoint(self) -> str:
        return f"ws://127.0.0.1:{self.port}/{self.token}"


@dataclass
class SessionRecord:
    state_identity: PathIdentity
    api: ApiRecord | None = None
    webkit: WebkitRecord | None = None


def save_record(state: Path, record: SessionRecord) -> None:
    temporary = state / TEMPORARY_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(temporary, flags, 0o600), "w", encoding="utf-8") as handle:
        json.dump(asdict(record), handle, indent=2, sort_keys=True)
    os.replace(temporary, state / RECORD_NAME)


def load_record(state: Path) -> SessionRecord:
    raw = json.loads((state / RECORD_NAME).read_text(encoding="utf-8"))
    api = raw["api"]
    webkit = raw["webkit"]
    return SessionRecord(
        state_identity=PathIdentity(**raw["state_identity"]),
        api=None if api is None else ApiRecord(
            pid=api["pid"],
            start_time=api["start_time"],
            command=list(api["command"]),
            socket=api["socket"],
            directory_identity=PathIdentity(**api["directory_identity"]),
            socket_identity=(
                None if api["socket_identity"] is None
                else PathIdentity(**api["socket_identity"])
            ),
        ),
        webkit=None if webkit is None else WebkitRecord(**webkit),
    )
