"""Deterministic, streamed synthetic catalog metadata for the 2A.1 benchmark.

This is metadata-only fixture data for a freshly created benchmark database. It is
not a filesystem fixture and is never a backfill of operator catalog data.
Every identity derives from a fixed UUID namespace and numbered seed keys, so a
shape always produces identical records without materializing them.
"""
from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cached_property
from typing import Any

from aegis_apps.catalog.names import source_name

NAMESPACE = uuid.UUID("6f4c52f6-7f39-5d5e-9a41-2a1d0b6c2a15")
MAX_ENTRIES = 20_000_000
MAX_WIDE_FOLDER = 1_000_000
FOLDER_CAPACITY = 1000
TOP_LEVEL_FOLDERS = 64
STALE_CHILDREN = 24
ROOT_SHARES = (70, 20, 10)
MTIME_BASE_NS = 1_600_000_000_000_000_000
DAY_NS = 86_400 * 1_000_000_000
DEVICE = 2049
ADMIN_USERNAME = "bench-admin"
# Labeled stale-availability fixtures (Task 6 ancestry): an unknown parent marker, a
# mismatched marker and an inaccessible directory. Their descendants are expected to
# be effectively unavailable; every other entry carries its current parent's revision.
STALE_FIXTURES = ("stale-unknown-marker", "stale-mismatched-marker", "inaccessible-directory")
COLUMNS = (
    "id", "root_id", "source_parent_id", "logical_parent_id", "raw_name", "display_name",
    "name_key", "logical_name", "kind", "type_hint", "source_state", "size", "mtime_ns",
    "ctime_ns", "device", "inode", "source_revision", "source_parent_revision",
    "catalog_version", "children_version", "observation_epoch", "seen_generation",
    "seen_attempt", "observed_at",
)


@dataclass(frozen=True, slots=True)
class RootSpec:
    key: str
    slot_id: str
    display_name: str
    direct_users: tuple[int, ...]
    group_users: tuple[int, ...]
    group_name: str | None


# Ten users; each root mixes direct and group grants. The administrator has none.
ROOTS = (
    RootSpec("main", "bench-main", "Benchmark main", (1, 2, 3, 4, 5),
             (6, 7, 8, 9, 10), "bench-main-readers"),
    RootSpec("second", "bench-second", "Benchmark second", (6, 7, 8, 9, 10), (), None),
    RootSpec("third", "bench-third", "Benchmark third", (), (1, 2, 3, 4, 5),
             "bench-third-readers"),
)


def username(number: int) -> str:
    if type(number) is not int or not 1 <= number <= 99:
        raise ValueError("invalid benchmark user number")
    return f"bench-user-{number:02d}"


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("invalid benchmark dataset shape")
    return value


@dataclass(frozen=True, slots=True)
class DatasetShape:
    entries: int
    wide_folder: int
    seed: int

    def __post_init__(self) -> None:
        _integer(self.entries, 1, MAX_ENTRIES)
        _integer(self.wide_folder, 1, MAX_WIDE_FOLDER)
        _integer(self.seed, 0, 2**63 - 1)
        _Layout(self.entries, self.wide_folder, self.seed).budgets  # noqa: B018


@dataclass(frozen=True, slots=True)
class DirectoryTarget:
    id: uuid.UUID
    root: str
    label: str
    children: int


class _Layout:
    """Pure index arithmetic: global index -> parent/root/local position."""

    def __init__(self, entries: int, wide: int, seed: int) -> None:
        self.entries, self.wide, self.seed = entries, wide, seed
        self.wide_dir = len(ROOTS)
        self.stale_dirs = tuple(range(self.wide_dir + 1, self.wide_dir + 1 + len(STALE_FIXTURES)))
        self.stale_base = self.stale_dirs[-1] + 1
        self.wide_base = self.stale_base + len(STALE_FIXTURES) * STALE_CHILDREN
        self.tree_base = self.wide_base + wide

    @cached_property
    def budgets(self) -> tuple[tuple[int, int, int], ...]:
        """Per root: (first index, directory count, file count)."""
        remaining = self.entries - self.tree_base
        if remaining < 2 * len(ROOTS) * 10:
            raise ValueError("invalid benchmark dataset shape")
        shares = [remaining * share // 100 for share in ROOT_SHARES]
        shares[0] += remaining - sum(shares)
        result = []
        start = self.tree_base
        for budget in shares:
            directories = max(1, -(-budget // (FOLDER_CAPACITY + 1)))
            result.append((start, directories, budget - directories))
            start += budget
        return tuple(result)

    def identity(self, index: int) -> uuid.UUID:
        return uuid.uuid5(NAMESPACE, f"aegis-2a1-catalog:{self.seed}:{index}")

    def revision(self, index: int) -> int:
        return 1 + (index * 7 + self.seed) % 5

    def tree_parent(self, root: int, directory: int) -> int:
        start, _directories, _files = self.budgets[root]
        top = min(self.budgets[root][1], TOP_LEVEL_FOLDERS)
        return root if directory < top else start + (directory - top) % top

    def tree_files(self, root: int, directory: int) -> tuple[int, int]:
        start, directories, files = self.budgets[root]
        first = start + directories
        return (first + directory * files // directories,
                first + (directory + 1) * files // directories)


def _file_name(local: int) -> bytes:
    bucket = local % 100
    number = f"{local:07d}"
    if bucket < 35:
        return f"IMG_{number}.jpg".encode()
    if bucket < 45:
        return f"document-{number}.pdf".encode()
    if bucket < 55:
        return f"notes-{number}.txt".encode()
    if bucket < 60:
        return f"clip-{number}.mp4".encode()
    if bucket < 64:
        return f"Café Ünïcode {number}.png".encode()
    if bucket == 64:
        return f"Café decomposed {number}.png".encode()
    if bucket < 68:
        return f"日本語ファイル-{number}.md".encode()
    if bucket < 70:
        return b"raw-\xff\xfe-" + number.encode() + b".bin"
    if bucket < 72:
        prefix = f"long-{number}-".encode()
        return prefix + b"x" * (255 - len(prefix) - 4) + b".dat"
    if bucket == 72:
        return f"Tie-{number}.csv".encode()
    if bucket == 73:
        # Case-adjacent twin of the previous entry: identical order key, distinct bytes.
        return f"tie-{local - 1:07d}.csv".encode()
    if bucket < 78:
        return f"README-{number}".encode()
    if bucket == 78:
        return f"data-{number}.unknown".encode()
    if bucket == 79:
        return (f"rare-{number}.xyz" if local % 1000 == 79 else f"photo-{number}.heic").encode()
    if bucket < 85:
        return f"100%_done-{number}.txt".encode()
    if bucket < 90:
        return f"song-{number}.mp3".encode()
    if bucket < 95:
        return f"archive-{number}.zip".encode()
    return f"IMG_{number}.jpeg".encode()


def _leaf(layout: _Layout, index: int, local: int, *, wide: bool) -> dict[str, Any]:
    kind = "file"
    raw = _file_name(local)
    if wide and local % 50 == 49:
        kind, raw = "directory", f"folder-{local:07d}".encode()
    elif local % 997 == 500:
        kind, raw = "symlink", f"link-{local:07d}".encode()
    elif wide and local % 1499 == 700:
        kind, raw = "special", f"fifo-{local:07d}".encode()
    state = "present"
    if kind != "directory":
        if local % 101 == 7:
            state = "missing"
        elif local % 211 == 11:
            state = "inaccessible"
    size: int | None
    if kind == "file":
        size = None if local % 53 == 0 else (
            2**40 + local if local % 1000 == 998 else (local % 17) * 4096
        )
    else:
        size = 24 if kind == "symlink" else None
    mtime = None if local % 59 == 0 else MTIME_BASE_NS + (local % 500) * DAY_NS
    if kind == "directory":
        mtime = MTIME_BASE_NS
    return {"raw": raw, "kind": kind, "state": state, "size": size, "mtime": mtime}


def _record(layout: _Layout, index: int, root: int, parent: int | None, raw: bytes,
            kind: str, state: str = "present", size: int | None = None,
            mtime: int | None = MTIME_BASE_NS, marker: str = "current") -> dict[str, object]:
    identity = layout.identity(index)
    parent_id = None if parent is None else layout.identity(parent)
    if parent is None:
        display, key, hint = "", b"", None
        parent_revision = None
    else:
        name = source_name(raw)
        display, key, hint = name.display, name.order_key, name.type_hint
        parent_revision = {
            "current": layout.revision(parent), "unknown": None,
            "mismatched": layout.revision(parent) + 100,
        }[marker]
    return {
        "id": identity, "root": ROOTS[root].key, "source_parent_id": parent_id,
        "logical_parent_id": parent_id, "raw_name": raw, "display_name": display,
        "name_key": key, "logical_name": None, "kind": kind, "type_hint": hint,
        "source_state": state, "size": size, "mtime_ns": mtime, "ctime_ns": mtime,
        "device": DEVICE, "inode": 10_000_000 + index,
        "source_revision": layout.revision(index), "source_parent_revision": parent_revision,
        "catalog_version": 1 + index % 4,
        "children_version": 1 + index % 3 if kind == "directory" else 0,
        "observation_epoch": 1, "seen_generation": 1, "seen_attempt": 1, "observed_at": None,
    }


def catalog_records(shape: DatasetShape) -> Iterator[dict[str, object]]:
    """Yield every record exactly once, parents before children, in O(1) memory."""
    layout = _Layout(shape.entries, shape.wide_folder, shape.seed)
    for root in range(len(ROOTS)):
        yield _record(layout, root, root, None, b"", "directory")
    yield _record(layout, layout.wide_dir, 0, 0, b"wide-folder", "directory")
    markers = ("unknown", "mismatched", "current")
    for number, index in enumerate(layout.stale_dirs):
        yield _record(layout, index, 0, 0, STALE_FIXTURES[number].encode(), "directory",
                      state="inaccessible" if number == 2 else "present",
                      marker=markers[number])
    for offset in range(len(STALE_FIXTURES) * STALE_CHILDREN):
        index = layout.stale_base + offset
        parent = layout.stale_dirs[offset // STALE_CHILDREN]
        leaf = _leaf(layout, index, offset % STALE_CHILDREN + 100, wide=False)
        yield _record(layout, index, 0, parent, f"held-{offset:04d}.txt".encode(), "file",
                      size=leaf["size"], mtime=leaf["mtime"])
    for local in range(shape.wide_folder):
        index = layout.wide_base + local
        leaf = _leaf(layout, index, local, wide=True)
        yield _record(layout, index, 0, layout.wide_dir, leaf["raw"], leaf["kind"],
                      leaf["state"], leaf["size"], leaf["mtime"])
    for root, (start, directories, _files) in enumerate(layout.budgets):
        top = min(directories, TOP_LEVEL_FOLDERS)
        for directory in range(directories):
            raw = (f"Album {directory:04d}" if directory < top else f"Set {directory:05d}")
            yield _record(layout, start + directory, root, layout.tree_parent(root, directory),
                          raw.encode(), "directory")
        for directory in range(directories):
            first, end = layout.tree_files(root, directory)
            for index in range(first, end):
                leaf = _leaf(layout, index, index - first, wide=False)
                yield _record(layout, index, root, start + directory, leaf["raw"],
                              leaf["kind"], leaf["state"], leaf["size"], leaf["mtime"])


def directory_targets(shape: DatasetShape) -> tuple[DirectoryTarget, ...]:
    """Browsable directories and exact direct-child counts, computed without generation."""
    layout = _Layout(shape.entries, shape.wide_folder, shape.seed)
    targets = []
    top_counts = []
    for root, (start, directories, _files) in enumerate(layout.budgets):
        top = min(directories, TOP_LEVEL_FOLDERS)
        nested = Counter(layout.tree_parent(root, directory) for directory in range(top,
                                                                                    directories))
        anchor_children = top + (1 + len(STALE_FIXTURES) if root == 0 else 0)
        top_counts.append(anchor_children)
        targets.append(DirectoryTarget(layout.identity(root), ROOTS[root].key, "anchor",
                                       anchor_children))
        for directory in range(directories):
            first, end = layout.tree_files(root, directory)
            targets.append(DirectoryTarget(
                layout.identity(start + directory), ROOTS[root].key, "tree",
                end - first + nested.get(start + directory, 0),
            ))
    targets.append(DirectoryTarget(layout.identity(layout.wide_dir), "main", "wide",
                                   shape.wide_folder))
    targets.extend(DirectoryTarget(layout.identity(index), "main", "stale", STALE_CHILDREN)
                   for index in layout.stale_dirs)
    return tuple(targets)


def dataset_counts(shape: DatasetShape) -> dict[str, Any]:
    """Exact reported counts, streamed once over the generator."""
    kinds: Counter[str] = Counter()
    states: Counter[str] = Counter()
    roots: Counter[str] = Counter()
    total = 0
    for record in catalog_records(shape):
        total += 1
        kinds[str(record["kind"])] += 1
        states[str(record["source_state"])] += 1
        roots[str(record["root"])] += 1
    return {
        "entries": total, "kinds": dict(sorted(kinds.items())),
        "states": dict(sorted(states.items())), "roots": dict(sorted(roots.items())),
        "wideFolderChildren": shape.wide_folder,
    }


def sort_key(record: dict[str, object], sort: str) -> tuple[object, ...]:
    """The browse query's ascending keyset tuple (direction applies past the first ranks)."""
    kind_rank = 0 if record["kind"] == "directory" else 1
    if sort == "name":
        return (kind_rank, record["name_key"], record["id"])
    field = "mtime_ns" if sort == "modified" else "size"
    value = record[field]
    return (kind_rank, int(value is None), 0 if value is None else value,
            record["name_key"], record["id"])


def expected_children(shape: DatasetShape, parent: uuid.UUID, *, sort: str = "name",
                      order: str = "asc") -> list[dict[str, object]]:
    """Default (non-missing) membership and order of one directory, for small shapes."""
    if sort not in ("name", "modified", "size") or order not in ("asc", "desc"):
        raise ValueError("invalid benchmark sort")
    rows = [record for record in catalog_records(shape)
            if record["logical_parent_id"] == parent and record["source_state"] != "missing"]
    fixed = 1 if sort == "name" else 2

    def ordered(record: dict[str, object]) -> tuple[object, ...]:
        key = sort_key(record, sort)
        if order == "asc":
            return key
        return (*key[:fixed], *(_Descending(value) for value in key[fixed:]))

    return sorted(rows, key=ordered)


@dataclass(frozen=True, slots=True)
class _Descending:
    value: Any

    def __lt__(self, other: _Descending) -> bool:
        return bool(other.value < self.value)
