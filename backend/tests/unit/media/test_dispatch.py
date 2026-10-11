"""The worker treats everything in ``done/`` as hostile; every rejection path is covered."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from aegis_apps.media.dispatch import (
    ResultRejected,
    Scratch,
    ScratchUnavailable,
    check_header,
    webp_dimensions,
)
from aegis_media_processor import protocol

from .worker_support import (
    make_job,
    publish_result,
    queued_jobs,
    scratch_tree,
    synthetic_mp4,
    synthetic_vp8,
    synthetic_vp8l,
    synthetic_webp,
)


@pytest.fixture
def scratch(tmp_path: Path) -> Iterator[Scratch]:
    scratch_tree(tmp_path)
    opened = Scratch(str(tmp_path))
    opened.prepare()
    yield opened
    opened.close()


def _done(scratch: Scratch) -> Path:
    return Path(scratch.done)


def _forged(job: protocol.Job, **overrides: object) -> bytes:
    document: dict[str, object] = {
        "version": 1, "id": job.id, "status": "ok", "safe_error_code": None,
        "metadata": None,
        "outputs": [
            {"name": "thumbnail", "format": "webp", "bytes": 100, "width": 400, "height": 300},
        ],
    }
    document.update(overrides)
    return json.dumps(document).encode()


def _reason(scratch: Scratch, job: protocol.Job) -> str:
    with pytest.raises(ResultRejected) as caught:
        scratch.collect(job)
    return caught.value.reason


# -- accepted results ---------------------------------------------------------------------


@pytest.mark.parametrize("op", ["thumbnail", "preview", "poster", "transcode"])
def test_a_plain_matching_result_is_accepted_with_open_descriptors(
    scratch: Scratch, op: protocol.Op,
) -> None:
    job = make_job(op, output_bytes=1 << 30)
    publish_result(_done(scratch), job)
    with scratch.collect(job) as collected:
        assert collected.result.status == "ok"
        assert [output.name for output in collected.outputs] == [
            spec.name for spec in protocol.OP_OUTPUTS[op]
        ]
        for output in collected.outputs:
            assert output.descriptor >= 0
            assert stat.S_ISREG(os.fstat(output.descriptor).st_mode)
    assert all(output.descriptor == -1 for output in collected.outputs)


def test_a_probe_result_carries_metadata_and_no_files(scratch: Scratch) -> None:
    job = make_job("probe", kind="photo")
    publish_result(_done(scratch), job)
    with scratch.collect(job) as collected:
        assert collected.result.metadata is not None
        assert collected.outputs == ()


@pytest.mark.parametrize(
    ("status", "code"),
    [("failed", "decode_failed"), ("timeout", "timeout"), ("unsupported", "too_large"),
     ("changed", "source_changed"), ("failed", "interrupted")],
)
def test_a_failure_result_has_only_its_result_file(
    scratch: Scratch, status: protocol.Status, code: str,
) -> None:
    job = make_job("thumbnail")
    publish_result(_done(scratch), job, status=status, code=code)
    with scratch.collect(job) as collected:
        assert collected.result.status == status
        assert collected.result.safe_error_code == code


# -- rejected results ---------------------------------------------------------------------


def test_a_symlinked_output_is_rejected(scratch: Scratch, tmp_path: Path) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    target = tmp_path / "elsewhere.webp"
    (directory / "thumbnail.webp").rename(target)
    (directory / "thumbnail.webp").symlink_to(target)
    assert _reason(scratch, job) == "not_regular"


def test_a_hard_linked_output_is_rejected(scratch: Scratch, tmp_path: Path) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    os.link(directory / "thumbnail.webp", tmp_path / "second-name.webp")
    assert _reason(scratch, job) == "hard_link"


def test_an_output_larger_than_its_job_bound_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail", output_bytes=64 << 10)
    big = synthetic_webp(400, 300, padding=70_000)
    raw = _forged(job, outputs=[
        {"name": "thumbnail", "format": "webp", "bytes": len(big), "width": 400, "height": 300},
    ])
    directory = _done(scratch) / job.id
    directory.mkdir()
    (directory / "thumbnail.webp").write_bytes(big)
    (directory / "result.json").write_bytes(raw)
    # The protocol refuses the declared size against the job; the worker never reads on.
    assert _reason(scratch, job) == "malformed"


def test_an_output_too_small_to_carry_its_header_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    tiny = synthetic_webp(400, 300)[:20]
    raw = _forged(job, outputs=[
        {"name": "thumbnail", "format": "webp", "bytes": len(tiny), "width": 400, "height": 300},
    ])
    directory = _done(scratch) / job.id
    directory.mkdir()
    (directory / "thumbnail.webp").write_bytes(tiny)
    (directory / "result.json").write_bytes(raw)
    assert _reason(scratch, job) == "size_bounds"


def test_an_output_whose_size_differs_from_its_declaration_is_rejected(
    scratch: Scratch,
) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    with open(directory / "thumbnail.webp", "ab") as handle:
        handle.write(b"\0\0")
    assert _reason(scratch, job) == "size_mismatch"


@pytest.mark.parametrize(
    "data",
    [
        b"\x89PNG\r\n\x1a\n" + b"\0" * 64,
        b"RIFF\0\0\0\0WAVE" + b"\0" * 64,
        b"\xff\xd8\xff\xe0" + b"\0" * 64,
    ],
)
def test_bad_magic_is_rejected(scratch: Scratch, data: bytes) -> None:
    job = make_job("thumbnail")
    publish_result(_done(scratch), job, outputs=[("thumbnail", data, 400, 300)])
    assert _reason(scratch, job) == "signature"


def test_an_mp4_without_ftyp_at_offset_four_is_rejected(scratch: Scratch) -> None:
    job = make_job("transcode", output_bytes=1 << 30)
    data = b"\0\0\0\x18moov" + b"\0" * 64
    publish_result(_done(scratch), job, outputs=[("transcode", data, 1920, 1080)])
    assert _reason(scratch, job) == "signature"


def test_a_riff_size_that_disagrees_with_the_file_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    data = bytearray(synthetic_webp(400, 300))
    data[4:8] = (len(data)).to_bytes(4, "little")
    publish_result(_done(scratch), job, outputs=[("thumbnail", bytes(data), 400, 300)])
    assert _reason(scratch, job) == "signature"


def test_declared_dimensions_must_match_the_webp_canvas(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    publish_result(_done(scratch), job, outputs=[("thumbnail", synthetic_webp(300, 300), 400, 300)])
    assert _reason(scratch, job) == "signature"


def test_dimensions_beyond_the_kind_bound_are_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    big = synthetic_webp(2048, 1536)
    raw = _forged(job, outputs=[
        {"name": "thumbnail", "format": "webp", "bytes": len(big), "width": 2048, "height": 1536},
    ])
    directory = _done(scratch) / job.id
    directory.mkdir()
    (directory / "thumbnail.webp").write_bytes(big)
    (directory / "result.json").write_bytes(raw)
    assert _reason(scratch, job) == "malformed"


def test_a_result_for_another_job_id_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    other = make_job("thumbnail")
    directory = publish_result(_done(scratch), other)
    directory.rename(_done(scratch) / job.id)
    assert _reason(scratch, job) == "malformed"


def test_a_result_shaped_for_another_operation_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    preview = make_job("preview")
    # A preview's outputs and result, published under the thumbnail job's id.
    directory = publish_result(_done(scratch), preview)
    document = json.loads((directory / "result.json").read_bytes())
    document["id"] = job.id
    (directory / "result.json").write_bytes(json.dumps(document).encode())
    directory.rename(_done(scratch) / job.id)
    assert _reason(scratch, job) == "malformed"


def test_probe_metadata_on_a_thumbnail_result_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    data = synthetic_webp(400, 300)
    raw = _forged(job, metadata={
        "width": 1, "height": 1, "orientation": None, "captured_at": None,
        "captured_at_source": None, "camera_make": None, "camera_model": None,
        "duration_ms": None, "video_codec": None, "audio_codec": None, "container": None,
        "has_location": False,
    }, outputs=[
        {"name": "thumbnail", "format": "webp", "bytes": len(data), "width": 400, "height": 300},
    ])
    directory = _done(scratch) / job.id
    directory.mkdir()
    (directory / "thumbnail.webp").write_bytes(data)
    (directory / "result.json").write_bytes(raw)
    assert _reason(scratch, job) == "malformed"


@pytest.mark.parametrize("extra", ["frame.png", "job.json", "thumbnail.webp.tmp", ".hidden"])
def test_any_extra_file_is_rejected(scratch: Scratch, extra: str) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    (directory / extra).write_bytes(b"x")
    assert _reason(scratch, job) == "unexpected_files"


def test_an_extra_directory_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    (directory / "out").mkdir()
    assert _reason(scratch, job) == "unexpected_files"


def test_a_missing_output_is_rejected(scratch: Scratch) -> None:
    job = make_job("poster", output_bytes=1 << 30)
    directory = publish_result(_done(scratch), job)
    (directory / "poster.webp").unlink()
    assert _reason(scratch, job) == "unexpected_files"


def test_outputs_on_a_failure_result_are_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job, status="failed", code="decode_failed")
    (directory / "thumbnail.webp").write_bytes(synthetic_webp(400, 300))
    assert _reason(scratch, job) == "unexpected_files"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b'{"version": 1}',
        b'{"version":1,"id":"x","status":"ok","safe_error_code":null,"metadata":null,'
        b'"outputs":[],"extra":1}',
        b"\xff\xfe",
        b'{"version":1,"version":1}',
        b'{"version":1.0,"id":"' + b"0" * 32 + b'","status":"ok","safe_error_code":null,'
        b'"metadata":null,"outputs":[]}',
    ],
)
def test_a_malformed_result_is_rejected(scratch: Scratch, raw: bytes) -> None:
    job = make_job("thumbnail")
    publish_result(_done(scratch), job, raw=raw)
    assert _reason(scratch, job) == "malformed"


def test_an_oversized_result_file_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    publish_result(_done(scratch), job, raw=b" " * (protocol.MAX_RESULT_BYTES + 1))
    assert _reason(scratch, job) == "malformed"


def test_a_symlinked_result_file_is_rejected(scratch: Scratch, tmp_path: Path) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    target = tmp_path / "forged.json"
    (directory / "result.json").rename(target)
    (directory / "result.json").symlink_to(target)
    assert _reason(scratch, job) == "unreadable"


def test_a_hard_linked_result_file_is_rejected(scratch: Scratch, tmp_path: Path) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    os.link(directory / "result.json", tmp_path / "copy.json")
    assert _reason(scratch, job) == "malformed"


def test_a_symlinked_job_directory_is_rejected(scratch: Scratch, tmp_path: Path) -> None:
    job = make_job("thumbnail")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    publish_result(elsewhere, job)
    (_done(scratch) / job.id).symlink_to(elsewhere / job.id)
    assert _reason(scratch, job) == "unreadable"


def test_a_missing_result_file_is_rejected(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    (directory / "result.json").unlink()
    assert _reason(scratch, job) == "unreadable"


def test_an_unreadable_job_directory_is_rejected(scratch: Scratch) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    directory.chmod(0)
    try:
        assert _reason(scratch, job) == "unreadable"
    finally:
        directory.chmod(0o700)


# -- headers ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "size"),
    [
        (synthetic_webp(400, 300), (400, 300)),
        (synthetic_vp8(2048, 1365), (2048, 1365)),
        (synthetic_vp8l(17, 4096), (17, 4096)),
    ],
)
def test_webp_canvas_sizes_are_read_from_each_header_kind(
    data: bytes, size: tuple[int, int],
) -> None:
    assert webp_dimensions(data[:32]) == size
    assert check_header("webp", data[:32], size=len(data), width=size[0], height=size[1])


@pytest.mark.parametrize(
    "head",
    [
        b"",
        b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 16,
        b"RIFF\0\0\0\0WEBPVP8L\0\0\0\0\x00" + b"\0" * 12,
        b"RIFF\0\0\0\0WEBPALPH" + b"\0" * 16,
    ],
)
def test_unreadable_webp_headers_have_no_size(head: bytes) -> None:
    assert webp_dimensions(head) is None


def test_mp4_headers_need_a_plausible_ftyp_box() -> None:
    data = synthetic_mp4()
    assert check_header("mp4", data[:32], size=len(data), width=1920, height=1080)
    huge_box = (len(data) + 1).to_bytes(4, "big") + data[4:32]
    assert not check_header("mp4", huge_box, size=len(data), width=1920, height=1080)


# -- the scratch directories --------------------------------------------------------------


def test_prepare_creates_the_worker_directories(tmp_path: Path) -> None:
    scratch = Scratch(str(tmp_path))
    scratch.prepare()
    assert (tmp_path / "inbox").is_dir() and (tmp_path / "done").is_dir()


def test_prepare_refuses_a_symlinked_scratch_or_subdirectory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(ScratchUnavailable):
        Scratch(str(tmp_path / "link")).prepare()
    (real / "inbox").symlink_to(tmp_path)
    with pytest.raises(ScratchUnavailable):
        Scratch(str(real)).prepare()


def test_submitted_jobs_are_queued_and_can_be_withdrawn(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    scratch.submit(job)
    assert scratch.queued(job.id)
    assert [queued.id for queued in queued_jobs(Path(scratch.root))] == [job.id]
    with pytest.raises(FileExistsError):
        scratch.submit(job)
    assert scratch.withdraw(job.id)
    assert not scratch.queued(job.id)
    assert not scratch.withdraw(job.id)


def test_startup_clears_an_earlier_workers_jobs_and_results(scratch: Scratch) -> None:
    stale = make_job("thumbnail")
    scratch.submit(stale)
    Path(scratch.inbox, f".{stale.id}.json.0011223344556677.tmp").write_bytes(b"{}")
    Path(scratch.inbox, "keep-me").write_bytes(b"")
    publish_result(_done(scratch), make_job("thumbnail"))
    (_done(scratch) / "junk").write_bytes(b"x")
    assert scratch.clear_stale() == 4
    assert sorted(os.listdir(scratch.inbox)) == ["keep-me"]
    assert os.listdir(scratch.done) == []


def test_discard_never_follows_a_symlink(scratch: Scratch, tmp_path: Path) -> None:
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "file").write_bytes(b"keep")
    (_done(scratch) / ("a" * 32)).symlink_to(precious)
    assert scratch.discard("a" * 32)
    assert (precious / "file").read_bytes() == b"keep"
    assert not (_done(scratch) / ("a" * 32)).exists()


def test_discard_removes_locked_directories(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    (directory / "nested").mkdir()
    (directory / "nested" / "file").write_bytes(b"x")
    (directory / "nested").chmod(0)
    directory.chmod(0o500)
    assert scratch.discard(job.id)
    assert not directory.exists()


@pytest.mark.parametrize("name", ["", ".", "..", "a/b"])
def test_discard_refuses_names_outside_done(scratch: Scratch, name: str) -> None:
    assert not scratch.discard(name)


def test_finished_lists_published_names(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    publish_result(_done(scratch), job)
    assert scratch.finished() == [job.id]


# -- FIFOs never block, and never pass ----------------------------------------------------


def test_a_fifo_result_file_is_rejected_without_blocking(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    (directory / "result.json").unlink()
    os.mkfifo(directory / "result.json")
    assert _reason(scratch, job) == "malformed"


def test_a_fifo_output_is_rejected_without_blocking(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    (directory / "thumbnail.webp").unlink()
    os.mkfifo(directory / "thumbnail.webp")
    assert _reason(scratch, job) == "not_regular"


def test_a_fifo_in_place_of_the_job_directory_is_rejected_and_removed(
    scratch: Scratch,
) -> None:
    job = make_job("thumbnail")
    os.mkfifo(_done(scratch) / job.id)
    assert _reason(scratch, job) == "unreadable"
    assert scratch.discard(job.id)
    assert not os.path.lexists(_done(scratch) / job.id)


def test_a_result_directory_with_many_names_is_refused_unread(scratch: Scratch) -> None:
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job)
    for index in range(50):
        (directory / f"extra-{index}").write_bytes(b"")
    assert _reason(scratch, job) == "unexpected_files"


# -- the held directories (a processor can swap any name in scratch) -----------------------


def _precious(tmp_path: Path) -> Path:
    precious = tmp_path.parent / f"{tmp_path.name}-precious"
    (precious / "aa" / "bb").mkdir(parents=True)
    (precious / "aa" / "bb" / "keep.webp").write_bytes(b"keep")
    (precious / ".keep.0011223344556677.tmp").write_bytes(b"staging")
    precious.chmod(0o750)
    return precious


def test_a_done_directory_swapped_for_a_symlink_is_never_followed(
    scratch: Scratch, tmp_path: Path,
) -> None:
    precious = _precious(tmp_path)
    job = make_job("thumbnail")
    publish_result(_done(scratch), job)
    os.rename(_done(scratch), tmp_path / "done.moved")
    (_done(scratch)).symlink_to(precious)
    # Every operation still works on the held directory, never on the symlink's target.
    assert scratch.finished() == [job.id]
    assert scratch.discard("aa")
    assert scratch.discard(job.id)
    assert (precious / "aa" / "bb" / "keep.webp").read_bytes() == b"keep"
    assert stat.S_IMODE(precious.stat().st_mode) == 0o750
    assert os.listdir(tmp_path / "done.moved") == []
    # And the next pass sees the swap.
    with pytest.raises(ScratchUnavailable):
        scratch.verify()
    assert not scratch.is_open


def test_an_inbox_swapped_for_a_symlink_is_never_followed(
    scratch: Scratch, tmp_path: Path,
) -> None:
    precious = _precious(tmp_path)
    os.rename(scratch.inbox, tmp_path / "inbox.moved")
    Path(scratch.inbox).symlink_to(precious)
    job = make_job("thumbnail")
    scratch.submit(job)
    assert scratch.queued(job.id)
    assert scratch.clear_stale() >= 1
    assert (precious / ".keep.0011223344556677.tmp").read_bytes() == b"staging"
    assert not any(name.endswith(".json") for name in os.listdir(precious))
    with pytest.raises(ScratchUnavailable):
        scratch.verify()


def test_a_replaced_done_directory_is_detected(scratch: Scratch, tmp_path: Path) -> None:
    os.rename(_done(scratch), tmp_path / "done.moved")
    _done(scratch).mkdir()
    with pytest.raises(ScratchUnavailable):
        scratch.verify()
    scratch.prepare()
    scratch.verify()


def test_a_replaced_scratch_root_is_detected(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    opened = Scratch(str(root))
    opened.prepare()
    os.rename(root, tmp_path / "scratch.moved")
    root.mkdir()
    with pytest.raises(ScratchUnavailable):
        opened.verify()


def test_verify_restores_the_workers_own_access(scratch: Scratch) -> None:
    os.chmod(scratch.done, 0o000)
    try:
        scratch.verify()
        assert stat.S_IMODE(os.stat(scratch.done).st_mode) & 0o700 == 0o700
    finally:
        os.chmod(scratch.done, 0o770)


def test_cleanup_never_changes_modes_through_a_symlink(
    scratch: Scratch, tmp_path: Path,
) -> None:
    precious = _precious(tmp_path)
    (precious / "aa").chmod(0o555)
    job = make_job("thumbnail")
    directory = publish_result(_done(scratch), job, status="failed", code="decode_failed")
    (directory / "nested").mkdir()
    (directory / "nested" / "link").symlink_to(precious / "aa")
    (directory / "nested").chmod(0)
    (_done(scratch) / ("b" * 32)).symlink_to(precious / "aa")
    try:
        assert scratch.discard(job.id)
        assert scratch.discard("b" * 32)
        assert stat.S_IMODE(precious.stat().st_mode) == 0o750
        assert stat.S_IMODE((precious / "aa").stat().st_mode) == 0o555
        assert (precious / "aa" / "bb" / "keep.webp").read_bytes() == b"keep"
        assert not directory.exists()
    finally:
        (precious / "aa").chmod(0o755)


def test_operations_need_a_prepared_scratch(tmp_path: Path) -> None:
    unopened = Scratch(str(tmp_path))
    with pytest.raises(ScratchUnavailable):
        unopened.finished()
    with pytest.raises(ScratchUnavailable):
        unopened.submit(make_job("thumbnail"))
    assert not unopened.discard("x")
    assert not unopened.withdraw("a" * 32)
