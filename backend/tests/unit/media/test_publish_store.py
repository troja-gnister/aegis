"""The derivatives directory: modes, exclusive staging, placement and refusals."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from aegis_apps.media import publish
from aegis_apps.media.dispatch import VerifiedOutput
from aegis_apps.media.publish import (
    DerivativeStore,
    OutputRejected,
    PublishFailed,
    StoreUnavailable,
    key_lock_id,
    stage_outputs,
)

from .worker_support import synthetic_mp4, synthetic_webp

KEY = "ab" + "cd" + "1" * 60
OTHER = "ab" + "cd" + "2" * 60


@pytest.fixture
def store(tmp_path: Path) -> DerivativeStore:
    root = tmp_path / "derivatives"
    root.mkdir(mode=0o770)
    return DerivativeStore(str(root))


@pytest.fixture
def umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _output(tmp_path: Path, data: bytes, *, declared: int | None = None,
            width: int = 400, height: int = 300, name: str = "thumbnail") -> VerifiedOutput:
    source = tmp_path / f"source-{len(list(tmp_path.iterdir()))}"
    source.write_bytes(data)
    descriptor = os.open(source, os.O_RDONLY)
    return VerifiedOutput(
        name=name, format="mp4" if name == "transcode" else "webp",
        bytes=len(data) if declared is None else declared, width=width, height=height,
        descriptor=descriptor,
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_staging_and_placing_use_0750_directories_and_0640_files(
    store: DerivativeStore, tmp_path: Path, umask: None,
) -> None:
    data = synthetic_webp(400, 300)
    output = _output(tmp_path, data)
    staged = store.stage(output, KEY, "webp")
    root = Path(store.root)
    assert _mode(root / "ab") == 0o750
    assert _mode(root / "ab" / "cd") == 0o750
    temporary = root / "ab" / "cd" / staged.temporary
    assert _mode(temporary) == 0o640
    store.place(staged)
    final = root / "ab" / "cd" / f"{KEY}.webp"
    assert final.read_bytes() == data
    assert _mode(final) == 0o640
    assert not temporary.exists()
    store.release(staged)
    output.close()
    assert sorted(os.listdir(root / "ab" / "cd")) == [f"{KEY}.webp"]


def test_a_setgid_derivatives_directory_gives_2750_subdirectories(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    os.chmod(store.root, 0o2770)
    if not os.stat(store.root).st_mode & stat.S_ISGID:
        pytest.skip("the file system does not keep the setgid bit for this user")
    output = _output(tmp_path, synthetic_webp(400, 300))
    staged = store.stage(output, KEY, "webp")
    store.release(staged)
    output.close()
    assert _mode(Path(store.root) / "ab") == 0o2750
    assert _mode(Path(store.root) / "ab" / "cd") == 0o2750


def test_placing_renames_over_an_existing_name_of_the_same_key(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    first = _output(tmp_path, synthetic_webp(400, 300, padding=10))
    staged = store.stage(first, KEY, "webp")
    store.place(staged)
    store.release(staged)
    replacement = synthetic_webp(400, 300, padding=40)
    second = _output(tmp_path, replacement)
    staged = store.stage(second, KEY, "webp")
    store.place(staged)
    store.release(staged)
    assert (Path(store.root) / "ab" / "cd" / f"{KEY}.webp").read_bytes() == replacement
    assert store.exists(KEY, "webp")
    assert not store.exists(OTHER, "webp")
    first.close()
    second.close()


def test_a_source_longer_than_its_verified_size_is_refused(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    data = synthetic_webp(400, 300)
    output = _output(tmp_path, data + b"\0\0", declared=len(data))
    with pytest.raises(OutputRejected):
        store.stage(output, KEY, "webp")
    output.close()
    assert os.listdir(Path(store.root) / "ab" / "cd") == []


def test_a_source_shorter_than_its_verified_size_is_refused(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    data = synthetic_webp(400, 300)
    output = _output(tmp_path, data[:-4], declared=len(data))
    with pytest.raises(OutputRejected):
        store.stage(output, KEY, "webp")
    output.close()
    assert os.listdir(Path(store.root) / "ab" / "cd") == []


def test_the_copy_is_checked_again_before_it_can_be_placed(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    # Same size as a valid file, but the bytes changed after verification.
    forged = b"\x89PNG" + synthetic_webp(400, 300)[4:]
    output = _output(tmp_path, forged)
    with pytest.raises(OutputRejected):
        store.stage(output, KEY, "webp")
    output.close()
    assert os.listdir(Path(store.root) / "ab" / "cd") == []


def test_a_format_that_does_not_match_the_extension_is_refused(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    output = _output(tmp_path, synthetic_mp4(), name="transcode", width=1920, height=1080)
    with pytest.raises(PublishFailed):
        store.stage(output, KEY, "webp")
    output.close()


def test_a_symlinked_prefix_directory_is_never_followed(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (Path(store.root) / "ab").symlink_to(elsewhere)
    output = _output(tmp_path, synthetic_webp(400, 300))
    with pytest.raises(PublishFailed):
        store.stage(output, KEY, "webp")
    output.close()
    assert os.listdir(elsewhere) == []
    assert not store.exists(KEY, "webp")
    assert not store.remove(KEY, "webp")


def test_stage_outputs_leaves_nothing_behind_when_one_fails(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    good = _output(tmp_path, synthetic_webp(400, 300))
    bad = _output(tmp_path, b"\0" * 64, name="poster", width=2048, height=1536)
    with pytest.raises(OutputRejected):
        stage_outputs(store, [(good, KEY), (bad, OTHER)])
    good.close()
    bad.close()
    assert os.listdir(Path(store.root) / "ab" / "cd") == []


def test_release_removes_an_unplaced_copy(store: DerivativeStore, tmp_path: Path) -> None:
    output = _output(tmp_path, synthetic_webp(400, 300))
    staged = store.stage(output, KEY, "webp")
    store.release(staged)
    output.close()
    assert os.listdir(Path(store.root) / "ab" / "cd") == []


def test_remove_unlinks_the_final_file_only(store: DerivativeStore, tmp_path: Path) -> None:
    output = _output(tmp_path, synthetic_webp(400, 300))
    staged = store.stage(output, KEY, "webp")
    store.place(staged)
    store.release(staged)
    output.close()
    assert store.remove(KEY, "webp")
    assert not store.exists(KEY, "webp")
    assert store.remove(KEY, "webp")
    assert store.remove("ff" * 32, "webp")


def test_check_requires_a_writable_plain_directory(tmp_path: Path) -> None:
    with pytest.raises(StoreUnavailable):
        DerivativeStore(str(tmp_path / "missing")).check()
    (tmp_path / "link").symlink_to(tmp_path)
    with pytest.raises(StoreUnavailable):
        DerivativeStore(str(tmp_path / "link")).check()
    DerivativeStore(str(tmp_path)).check()


@pytest.mark.parametrize("value", ["", "x" * 64, "AB" * 32, "ab" * 31])
def test_paths_and_locks_require_a_hex_key(store: DerivativeStore, value: str) -> None:
    with pytest.raises(ValueError):
        key_lock_id(value)
    with pytest.raises(ValueError):
        store.exists(value, "webp")


def test_lock_ids_are_signed_32_bit_values() -> None:
    assert key_lock_id("ffffffff" + "0" * 56) == -1
    assert key_lock_id("7fffffff" + "0" * 56) == 2**31 - 1
    assert key_lock_id("00000000" + "f" * 56) == 0


def test_an_unreadable_source_is_the_processors_fault(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    output = _output(tmp_path, synthetic_webp(400, 300))
    os.close(output.descriptor)
    output.descriptor = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)  # EISDIR on read
    with pytest.raises(OutputRejected):
        store.stage(output, KEY, "webp")
    output.close()
    assert os.listdir(Path(store.root) / "ab" / "cd") == []


def test_a_long_copy_reports_progress_and_can_be_abandoned(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    data = synthetic_webp(400, 300, padding=4096)
    calls: list[int] = []

    def report() -> bool:
        calls.append(1)
        return True

    with (
        patch.object(publish, "COPY_CHUNK_BYTES", 512),
        patch.object(publish, "COPY_PROGRESS_BYTES", 1024),
    ):
        output = _output(tmp_path, data)
        staged = store.stage(output, KEY, "webp", progress=report)
        store.release(staged)
        assert len(calls) >= 3
        with pytest.raises(PublishFailed):
            store.stage(output, KEY, "webp", progress=lambda: False)
        output.close()
    assert os.listdir(Path(store.root) / "ab" / "cd") == []


def test_check_reopens_a_replaced_derivatives_directory(
    store: DerivativeStore, tmp_path: Path,
) -> None:
    store.check()
    os.rename(store.root, tmp_path / "old-derivatives")
    os.mkdir(store.root, 0o770)
    store.check()
    output = _output(tmp_path, synthetic_webp(400, 300))
    staged = store.stage(output, KEY, "webp")
    store.place(staged)
    store.release(staged)
    output.close()
    assert (Path(store.root) / "ab" / "cd" / f"{KEY}.webp").is_file()
    assert not (tmp_path / "old-derivatives" / "ab").exists()
