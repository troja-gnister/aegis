"""Opening an original: validated components, no symlinks, fingerprint proved before decoding."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest
from aegis_media_processor.protocol import SourceInput
from aegis_media_processor.source import (
    InputRefused,
    SourceChanged,
    SourceUnavailable,
    descriptor_path,
    matches,
    open_source,
)

from .support import SLOT, make_roots, source_input, write_source

CONTENT = b"original bytes, never modified"


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    roots = make_roots(tmp_path)
    write_source(roots, b"album/photo.jpg", CONTENT)
    return roots


def open_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_opens_the_fingerprinted_file_read_only(roots: Path) -> None:
    descriptor = open_source(source_input(roots, b"album/photo.jpg"), str(roots))
    try:
        assert os.read(descriptor, 100) == CONTENT
        assert os.get_inheritable(descriptor) is False
        with pytest.raises(OSError):
            os.write(descriptor, b"x")
    finally:
        os.close(descriptor)


def test_a_swap_after_the_check_cannot_substitute_another_file(roots: Path) -> None:
    descriptor = open_source(source_input(roots, b"album/photo.jpg"), str(roots))
    try:
        replacement = roots / SLOT / "album" / "replacement.jpg"
        replacement.write_bytes(b"substituted")
        os.replace(replacement, roots / SLOT / "album" / "photo.jpg")
        with open(descriptor_path(descriptor), "rb") as reopened:
            assert reopened.read() == CONTENT
        assert os.pread(descriptor, 100, 0) == CONTENT
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("slot", ["", "../synthetic", "Synthetic", "a/b", "synthetic\0"])
def test_invalid_slots_are_refused_before_opening(roots: Path, slot: str) -> None:
    source = replace(source_input(roots, b"album/photo.jpg"), slot=slot)

    with pytest.raises(InputRefused):
        open_source(source, str(roots))


@pytest.mark.parametrize("path", [
    b"../synthetic/album/photo.jpg", b"album/../album/photo.jpg", b"/album/photo.jpg",
    b"album//photo.jpg", b"album/photo.jpg/", b"album/\0photo.jpg", b"./album/photo.jpg", b"",
])
def test_traversal_absolute_nul_and_empty_paths_are_refused(roots: Path, path: bytes) -> None:
    source = replace(source_input(roots, b"album/photo.jpg"), path=path)
    before = open_count()

    with pytest.raises(InputRefused):
        open_source(source, str(roots))
    assert open_count() == before


def test_a_relative_roots_directory_is_refused(roots: Path) -> None:
    with pytest.raises(InputRefused):
        open_source(source_input(roots, b"album/photo.jpg"), "roots")


def test_a_symlinked_file_is_never_followed(roots: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(CONTENT)
    (roots / SLOT / "album" / "link.jpg").symlink_to(outside)
    observed = os.stat(outside)
    source = SourceInput(SLOT, b"album/link.jpg", observed.st_dev, observed.st_ino,
                         observed.st_size, observed.st_mtime_ns)

    with pytest.raises(SourceChanged):
        open_source(source, str(roots))


def test_a_symlinked_directory_on_the_path_is_never_followed(roots: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "photo.jpg").write_bytes(CONTENT)
    (roots / SLOT / "linked").symlink_to(outside)
    observed = os.stat(outside / "photo.jpg")
    source = SourceInput(SLOT, b"linked/photo.jpg", observed.st_dev, observed.st_ino,
                         observed.st_size, observed.st_mtime_ns)

    with pytest.raises(SourceChanged):
        open_source(source, str(roots))


def test_a_symlinked_slot_is_unavailable(roots: Path, tmp_path: Path) -> None:
    (roots / "alias").symlink_to(roots / SLOT)
    source = replace(source_input(roots, b"album/photo.jpg"), slot="alias")

    with pytest.raises(SourceUnavailable):
        open_source(source, str(roots))


def test_a_missing_slot_is_unavailable_and_a_missing_file_changed(roots: Path) -> None:
    source = source_input(roots, b"album/photo.jpg")

    with pytest.raises(SourceUnavailable):
        open_source(replace(source, slot="absent"), str(roots))
    with pytest.raises(SourceChanged):
        open_source(replace(source, path=b"album/gone.jpg"), str(roots))
    with pytest.raises(SourceChanged):
        open_source(replace(source, path=b"missing/photo.jpg"), str(roots))
    with pytest.raises(SourceChanged):
        open_source(replace(source, path=b"album/photo.jpg/inner.jpg"), str(roots))


def test_a_fifo_is_refused_without_blocking(roots: Path) -> None:
    os.mkfifo(roots / SLOT / "album" / "pipe.jpg")
    observed = os.lstat(roots / SLOT / "album" / "pipe.jpg")
    source = SourceInput(SLOT, b"album/pipe.jpg", observed.st_dev, observed.st_ino, 0,
                         observed.st_mtime_ns)
    started = time.monotonic()

    with pytest.raises(SourceChanged):
        open_source(source, str(roots))
    assert time.monotonic() - started < 5


def test_a_directory_is_refused(roots: Path) -> None:
    observed = os.stat(roots / SLOT / "album")
    source = SourceInput(SLOT, b"album", observed.st_dev, observed.st_ino, observed.st_size,
                         observed.st_mtime_ns)

    with pytest.raises(SourceChanged):
        open_source(source, str(roots))


@pytest.mark.parametrize("field", ["device", "inode", "size", "mtime_ns"])
def test_any_fingerprint_difference_is_a_change(roots: Path, field: str) -> None:
    source = source_input(roots, b"album/photo.jpg")
    changed = replace(source, **{field: getattr(source, field) + 1})
    before = open_count()

    with pytest.raises(SourceChanged):
        open_source(changed, str(roots))
    assert open_count() == before


def test_a_rewritten_file_is_a_change(roots: Path) -> None:
    source = source_input(roots, b"album/photo.jpg")
    path = roots / SLOT / "album" / "photo.jpg"
    path.write_bytes(CONTENT + b" plus an edit")

    with pytest.raises(SourceChanged):
        open_source(source, str(roots))


def test_descriptor_paths_name_the_open_file_only() -> None:
    assert descriptor_path(7) == "/proc/self/fd/7"
    with pytest.raises(ValueError):
        descriptor_path(-1)


def test_an_open_descriptor_is_rechecked_against_the_fingerprint(roots: Path) -> None:
    source = source_input(roots, b"album/photo.jpg")
    descriptor = open_source(source, str(roots))
    try:
        assert matches(descriptor, source)
        assert not matches(descriptor, replace(source, inode=source.inode + 1))
        (roots / SLOT / "album" / "photo.jpg").write_bytes(CONTENT + b" edited in place")
        assert not matches(descriptor, source)
    finally:
        os.close(descriptor)
    assert not matches(descriptor, source)
