"""Physical source observations, independent of logical organization."""

from dataclasses import dataclass
from enum import StrEnum


class EntryKind(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"
    SPECIAL = "special"


class SourceState(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    INACCESSIBLE = "inaccessible"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class SourceName:
    raw: bytes
    display: str
    order_key: bytes
    type_hint: str | None


@dataclass(frozen=True, slots=True)
class Observation:
    name: SourceName
    kind: EntryKind
    state: SourceState
    size: int | None
    mtime_ns: int | None
    ctime_ns: int | None
    device: int | None
    inode: int | None


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int
