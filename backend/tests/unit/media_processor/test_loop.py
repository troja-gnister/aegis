"""The processor loop: claims by rename, stale-claim sweep, publication, shutdown, configuration."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from aegis_media_processor.loop import (
    ConfigError,
    Processor,
    ScratchError,
    _is_stray,
    concurrency_from_environment,
    verify_outputs,
)
from aegis_media_processor.protocol import (
    MAX_RESULT_BYTES,
    Job,
    Limits,
    Output,
    Result,
    decode_result,
    encode_job,
    read_regular_file,
    write_job,
)
from PIL import Image

from .support import LIMITS, make_job, make_roots, save_image, write_source

# A stand-in decoding child, run as ``python -I -c FAKE_CHILD <behaviour> <context> <op>
# <job dir> <fd>`` under the same Landlock ruleset as the real one (so it can write only in
# out/, and cannot create directories, symlinks or FIFOs at all).
FAKE_CHILD = r'''
import json, os, subprocess, sys, time
behaviour, context, op, directory, number = sys.argv[1:6]
job = json.loads(open(os.path.join(directory, "job.json"), "rb").read())
out = os.path.join(directory, "out")
def result(**fields):
    document = {"version": 1, "id": job["id"], "status": "ok", "safe_error_code": None,
                "metadata": None, "outputs": []}
    document.update(fields)
    sys.stdout.write(json.dumps(document))
def attempt(action):
    try:
        action()
    except OSError:
        pass
def write(name, data):
    with open(os.path.join(out, name), "wb") as handle:
        handle.write(data)
webp = b"RIFF\0\0\0\0WEBP" + b"\0" * 4
output = {"name": "thumbnail", "format": "webp", "bytes": 16, "width": 10, "height": 10}
sleeper = [sys.executable, "-I", "-c", "import time; time.sleep(60)", context]
if behaviour == "sleep":
    time.sleep(60)
elif behaviour == "garbage":
    sys.stdout.write("not json")
elif behaviour == "missing-output":
    result(outputs=[output])
elif behaviour == "wrong-magic":
    write("thumbnail.webp", b"\x89PNG" + b"\0" * 12)
    result(outputs=[output])
elif behaviour == "symlink-output":
    attempt(lambda: os.symlink("/etc/hostname", os.path.join(out, "thumbnail.webp")))
    result(outputs=[output])
elif behaviour == "extra-files":
    write("thumbnail.webp", webp)
    write("frame.png", b"temporary")
    attempt(lambda: os.mkdir(os.path.join(out, "nested")))
    attempt(lambda: os.symlink("/etc", os.path.join(out, "escape")))
    result(outputs=[output])
elif behaviour == "chmod":
    write("thumbnail.webp", webp)
    changed = []
    for path in (os.path.join(out, "thumbnail.webp"), out, directory, context):
        try:
            os.chmod(path, 0)
            changed.append(path)
        except PermissionError:
            pass
    if changed:
        sys.exit(3)
    result(outputs=[output])
elif behaviour == "exit":
    sys.exit(3)
elif behaviour == "orphan":
    subprocess.Popen(sleeper, start_new_session=True)
    sys.stdout.write("not json")
elif behaviour == "orphan-sleep":
    subprocess.Popen(sleeper, start_new_session=True)
    time.sleep(60)
elif behaviour == "tool-sleep":
    from aegis_media_processor.sandbox import Child, ChildLimits, scrubbed_environment
    Child(sleeper, ChildLimits(512 << 20, 60, 60, 1 << 20, nice=0), cwd=out,
          environment=scrubbed_environment())
    time.sleep(60)
elif behaviour == "inspect":
    descriptor = int(number)
    open_fds = []
    for candidate in range(3, 1024):
        try:
            os.fstat(candidate)
            open_fds.append(candidate)
        except OSError:
            pass
    try:
        open(context, "rb").close()
        root = "readable"
    except PermissionError:
        root = "refused"
    passed = (open_fds == [descriptor] and os.pread(descriptor, 2, 0) == b"\xff\xd8"
              and sorted(os.environ) == ["LANG", "LC_ALL", "MALLOC_ARENA_MAX", "PATH", "TMPDIR"]
              and os.environ["TMPDIR"] == out and os.getcwd() == out and root == "refused")
    if not passed:
        sys.exit(3)
    result(status="unsupported", safe_error_code="unsupported_format")
'''


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    roots = make_roots(tmp_path)
    save_image(roots, b"album/photo.jpg", Image.new("RGB", (1200, 800), (10, 120, 200)), "JPEG")
    return roots


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return scratch


def processor(scratch: Path, roots: Path, **options: object) -> Processor:
    return Processor(str(scratch), str(roots), **options)  # type: ignore[arg-type]


def fake_processor(scratch: Path, roots: Path, behaviour: str, context: str = "-") -> Processor:
    return processor(scratch, roots,
                     child_command=(sys.executable, "-I", "-c", FAKE_CHILD, behaviour, context))


def alive(marker: str) -> list[int]:
    """Live (not zombie) processes whose command line carries ``marker``."""
    found = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            command = Path(f"/proc/{name}/cmdline").read_bytes()
            state = Path(f"/proc/{name}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
        except OSError:
            continue
        if marker.encode() in command.split(b"\0") and state not in (b"Z", b"X"):
            found.append(int(name))
    return found


def enqueue(scratch: Path, job: Job) -> None:
    (scratch / "inbox").mkdir(exist_ok=True)
    write_job(str(scratch / "inbox"), job)


def published(scratch: Path, job: Job) -> Result:
    data = read_regular_file(str(scratch / "done" / job.id / "result.json"), MAX_RESULT_BYTES)
    return decode_result(data, job)


def assert_drained(scratch: Path) -> None:
    assert os.listdir(scratch / "inbox") == []
    assert os.listdir(scratch / "work") == []


# -- configuration -------------------------------------------------------------------------------


@pytest.mark.parametrize("environ,expected", [
    ({}, 2), ({"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "1"}, 1),
    ({"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "16"}, 16),
    ({"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "04"}, 4),
])
def test_concurrency_comes_from_the_environment(environ: dict[str, str], expected: int) -> None:
    assert concurrency_from_environment(environ) == expected


@pytest.mark.parametrize("value", [
    "0", "17", "", " 2", "2 ", "+2", "-1", "2.0", "two", "\u0661", "100",
])
def test_invalid_concurrency_is_refused(value: str) -> None:
    with pytest.raises(ConfigError):
        concurrency_from_environment({"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": value})


def test_processor_paths_and_bounds_are_validated(scratch: Path, roots: Path) -> None:
    for scratch_path, roots_path, concurrency in (
        ("scratch", str(roots), 2), (str(scratch), "roots", 2), (str(scratch), str(roots), 0),
        (str(scratch), str(roots), 17),
    ):
        with pytest.raises(ConfigError):
            Processor(scratch_path, roots_path, concurrency)


def test_prepare_creates_the_directories_and_refuses_symlinks(
    tmp_path: Path, scratch: Path, roots: Path,
) -> None:
    with processor(scratch, roots) as ready:
        ready.prepare()
    assert sorted(os.listdir(scratch)) == ["done", "inbox", "rejected", "work"]

    other = tmp_path / "other-scratch"
    other.mkdir()
    (other / "inbox").symlink_to(tmp_path)
    with processor(other, roots) as refused, pytest.raises(ScratchError):
        refused.prepare()
    with processor(tmp_path / "absent", roots) as absent, pytest.raises(ScratchError):
        absent.prepare()


# -- claiming ------------------------------------------------------------------------------------


def test_a_claim_moves_the_job_by_rename_exactly_once(scratch: Path, roots: Path) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    enqueue(scratch, job)
    with processor(scratch, roots) as claimant:
        claimant.prepare()
        inode = os.stat(scratch / "inbox" / f"{job.id}.json").st_ino

        directory = claimant.claim(job.id)

        assert directory == str(scratch / "work" / job.id)
        assert os.listdir(scratch / "inbox") == []
        assert os.stat(scratch / "work" / job.id / "job.json").st_ino == inode
        assert claimant.claim(job.id) is None


def test_a_published_or_claimed_id_is_refused(scratch: Path, roots: Path) -> None:
    published_job = make_job(roots, b"album/photo.jpg", "thumbnail")
    claimed_job = make_job(roots, b"album/photo.jpg", "preview")
    with processor(scratch, roots) as claimant:
        claimant.prepare()
        (scratch / "done" / published_job.id).mkdir()
        (scratch / "done" / published_job.id / "result.json").write_text("kept")
        (scratch / "work" / claimed_job.id).mkdir()
        enqueue(scratch, published_job)
        enqueue(scratch, claimed_job)

        assert claimant.claim(published_job.id) is None
        assert claimant.claim(claimed_job.id) is None

    assert os.listdir(scratch / "inbox") == []
    assert (scratch / "done" / published_job.id / "result.json").read_text() == "kept"


def test_interactive_jobs_are_claimed_first_then_the_oldest(scratch: Path, roots: Path) -> None:
    old = make_job(roots, b"album/photo.jpg", "thumbnail")
    new = make_job(roots, b"album/photo.jpg", "thumbnail")
    urgent = make_job(roots, b"album/photo.jpg", "preview", lane="interactive")
    for job, mtime in ((new, 3_000_000_000), (urgent, 4_000_000_000), (old, 1_000_000_000)):
        enqueue(scratch, job)
        os.utime(scratch / "inbox" / f"{job.id}.json", ns=(mtime, mtime))
    (scratch / "inbox" / ("f" * 32 + ".json")).write_bytes(b"unreadable")
    (scratch / "inbox" / f".{old.id}.json.0123abcd.tmp").write_bytes(encode_job(old))
    (scratch / "inbox" / "notes.txt").write_text("ignored")

    with processor(scratch, roots) as claimant:
        claimant.prepare()
        order = claimant._candidates()

    assert order == [urgent.id, old.id, new.id, "f" * 32]


# -- the stale-claim sweep -----------------------------------------------------------------------


def test_startup_publishes_stale_claims_as_interrupted(
    tmp_path: Path, scratch: Path, roots: Path,
) -> None:
    stale = make_job(roots, b"album/photo.jpg", "thumbnail")
    half_claimed = make_job(roots, b"album/photo.jpg", "preview")
    already_done = make_job(roots, b"album/photo.jpg", "probe")
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched")
    with processor(scratch, roots) as starting:
        starting.prepare()
        work = scratch / "work"
        (work / stale.id).mkdir()
        (work / stale.id / "job.json").write_bytes(encode_job(stale))
        (work / stale.id / "thumbnail.webp").write_bytes(b"partial")
        (work / stale.id / "link").symlink_to(outside)
        (work / half_claimed.id).mkdir()
        enqueue(scratch, half_claimed)
        (work / already_done.id).mkdir()
        (work / already_done.id / "job.json").write_bytes(encode_job(already_done))
        (scratch / "done" / already_done.id).mkdir()
        (work / "junk").write_text("junk")
        (work / ("e" * 32)).symlink_to(tmp_path)

        assert starting.sweep() == 1

    assert os.listdir(work) == []
    assert os.listdir(scratch / "done" / stale.id) == ["result.json"]
    assert published(scratch, stale) == Result(stale.id, "failed", "interrupted", None, ())
    assert os.listdir(scratch / "inbox") == [f"{half_claimed.id}.json"]
    assert os.listdir(scratch / "done" / already_done.id) == []
    assert outside.read_text() == "untouched"
    assert tmp_path.is_dir()


def test_the_sweep_survives_hostile_leftovers(scratch: Path, roots: Path) -> None:
    locked = make_job(roots, b"album/photo.jpg", "thumbnail")
    sealed = make_job(roots, b"album/photo.jpg", "preview")
    with processor(scratch, roots) as starting:
        starting.prepare()
        work = scratch / "work"
        claim = work / locked.id
        (claim / "out" / "inner").mkdir(parents=True)
        (claim / "job.json").write_bytes(encode_job(locked))
        (claim / "out" / "inner" / "file").write_bytes(b"x")
        os.mkfifo(claim / "out" / "pipe")
        (claim / "out" / "inner").chmod(0o500)
        (claim / "out").chmod(0o500)
        (work / sealed.id).mkdir()
        (work / sealed.id / "job.json").write_bytes(encode_job(sealed))
        (work / sealed.id).chmod(0)
        os.mkfifo(work / "stray-fifo")
        (work / ("c" * 32)).write_bytes(b"a file named like a job")
        deep = work / "deep"
        for _ in range(70):
            deep = deep / "d"
        deep.mkdir(parents=True)
        (deep / "x").write_bytes(b"x")
        deep.chmod(0o500)  # beyond the unlock depth: only moving it away is possible

        assert starting.sweep() == 1

    assert os.listdir(scratch / "work") == []
    assert os.listdir(scratch / "done") == [locked.id]
    assert published(scratch, locked) == Result(locked.id, "failed", "interrupted", None, ())
    rejected = os.listdir(scratch / "rejected")
    assert len(rejected) == 1 and rejected[0].startswith("deep-")
    for directory, _, _ in os.walk(scratch / "rejected"):
        os.chmod(directory, 0o700)


WEBP = b"RIFF\0\0\0\0WEBP" + b"\0" * 4


@pytest.mark.parametrize("plant", [
    "directory", "fifo", "symlink", "hardlink", "size", "unreadable", "magic", "missing",
])
def test_output_checks_refuse_anything_but_a_plain_matching_file(
    tmp_path: Path, roots: Path, plant: str,
) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    out = tmp_path / "out"
    out.mkdir()
    target, real = out / "thumbnail.webp", tmp_path / "real.webp"
    real.write_bytes(WEBP)
    if plant == "directory":
        target.mkdir()
    elif plant == "fifo":
        os.mkfifo(target)
    elif plant == "symlink":
        target.symlink_to(real)
    elif plant == "hardlink":
        os.link(real, target)
    elif plant == "size":
        target.write_bytes(WEBP + b"x")
    elif plant == "unreadable":
        target.write_bytes(WEBP)
        target.chmod(0)
    elif plant == "magic":
        target.write_bytes(b"\x89PNG" + b"\0" * 12)
    claimed = Result(job.id, "ok", None, None, (Output("thumbnail", "webp", 16, 10, 10),))

    assert verify_outputs(str(out), job, claimed) == Result(
        job.id, "failed", "invalid_result", None, (),
    )


def test_output_checks_accept_a_plain_matching_file(tmp_path: Path, roots: Path) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    (tmp_path / "thumbnail.webp").write_bytes(WEBP)
    claimed = Result(job.id, "ok", None, None, (Output("thumbnail", "webp", 16, 10, 10),))

    assert verify_outputs(str(tmp_path), job, claimed) == claimed


def test_discard_never_raises(scratch: Path, roots: Path) -> None:
    with processor(scratch, roots) as serving:
        serving.prepare()
        serving.discard(str(scratch / "work" / "absent"))
        (scratch / "work" / "file").write_bytes(b"x")
        serving.discard(str(scratch / "work" / "file"))

    assert os.listdir(scratch / "work") == []


# -- end to end with the real tool child ---------------------------------------------------------


def test_jobs_are_processed_by_real_children_and_published_atomically(
    scratch: Path, roots: Path,
) -> None:
    thumbnail = make_job(roots, b"album/photo.jpg", "thumbnail")
    preview = make_job(roots, b"album/photo.jpg", "preview", lane="interactive")
    probe = make_job(roots, b"album/photo.jpg", "probe")
    for job in (thumbnail, preview, probe):
        enqueue(scratch, job)

    with processor(scratch, roots, concurrency=2) as serving:
        serving.run(until_idle=True)

    assert_drained(scratch)
    assert sorted(os.listdir(scratch / "done" / thumbnail.id)) == ["result.json", "thumbnail.webp"]
    assert sorted(os.listdir(scratch / "done" / preview.id)) == ["preview.webp", "result.json"]
    assert os.listdir(scratch / "done" / probe.id) == ["result.json"]
    (output,) = published(scratch, thumbnail).outputs
    assert output.width == 400 and output.height in (266, 267)
    assert output.bytes == (scratch / "done" / thumbnail.id / "thumbnail.webp").stat().st_size
    assert published(scratch, preview).outputs[0].width == 1200
    metadata = published(scratch, probe).metadata
    assert metadata is not None and (metadata.width, metadata.height) == (1200, 800)


def test_changed_corrupt_and_invalid_jobs_are_published_as_failures(
    scratch: Path, roots: Path,
) -> None:
    write_source(roots, b"corrupt.png", b"\x89PNG not really " * 20)
    changed = make_job(roots, b"album/photo.jpg", "thumbnail")
    changed = replace(changed, input=replace(changed.input, size=changed.input.size + 1))
    corrupt = make_job(roots, b"corrupt.png", "thumbnail")
    enqueue(scratch, changed)
    enqueue(scratch, corrupt)
    invalid_id = "d" * 32
    (scratch / "inbox" / f"{invalid_id}.json").write_bytes(b'{"version": 1, "id": "x"}')

    with processor(scratch, roots) as serving:
        serving.run(until_idle=True)

    assert_drained(scratch)
    assert (published(scratch, changed).status, published(scratch, changed).safe_error_code) == (
        "changed", "source_changed",
    )
    assert published(scratch, corrupt).safe_error_code == "decode_failed"
    invalid = json.loads((scratch / "done" / invalid_id / "result.json").read_bytes())
    assert (invalid["status"], invalid["safe_error_code"]) == ("failed", "invalid_job")
    assert os.listdir(scratch / "done" / invalid_id) == ["result.json"]


@pytest.mark.parametrize("behaviour,code", [
    ("garbage", "invalid_result"),
    ("missing-output", "invalid_result"),
    ("wrong-magic", "invalid_result"),
    ("symlink-output", "invalid_result"),
    ("exit", "crashed"),
])
def test_a_misbehaving_child_never_publishes_its_claims(
    scratch: Path, roots: Path, behaviour: str, code: str,
) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    enqueue(scratch, job)

    with fake_processor(scratch, roots, behaviour) as serving:
        serving.run(until_idle=True)

    assert published(scratch, job) == Result(job.id, "failed", code, None, ())
    assert os.listdir(scratch / "done" / job.id) == ["result.json"]
    assert_drained(scratch)
    assert os.listdir(scratch / "rejected") == []


def test_a_child_cannot_change_modes(scratch: Path, roots: Path) -> None:
    """Landlock cannot restrict chmod; the seccomp filter does, for every path the child names."""
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    enqueue(scratch, job)

    with fake_processor(scratch, roots, "chmod", str(scratch / "inbox")) as serving:
        serving.run(until_idle=True)

    assert published(scratch, job).status == "ok"
    assert (scratch / "inbox").stat().st_mode & 0o777 == 0o770


def test_modes_are_restored_every_pass(scratch: Path, roots: Path) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    enqueue(scratch, job)

    with fake_processor(scratch, roots, "sleep") as serving:
        serving.prepare()
        assert serving.fill() == 1
        directories = [scratch / name for name in ("inbox", "work", "rejected")]
        directories += [scratch / "work" / job.id, scratch / "work" / job.id / "out"]
        for directory in directories:
            directory.chmod(0o500)
        (scratch / "done").chmod(0o2750)

        serving.restore_modes()

        modes = {directory.name: directory.stat().st_mode & 0o7777 for directory in directories}
        done = (scratch / "done").stat().st_mode & 0o7777
        serving.shutdown()

    assert set(modes.values()) == {0o770}
    assert done == 0o2770
    assert scratch.stat().st_mode & 0o777 == 0o770


def test_a_surviving_stray_means_no_outputs_are_published(
    scratch: Path, roots: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    enqueue(scratch, job)

    with fake_processor(scratch, roots, "extra-files") as serving:
        monkeypatch.setattr(serving, "kill_strays", lambda: False)
        serving.run(until_idle=True)

    assert published(scratch, job) == Result(job.id, "failed", "crashed", None, ())
    assert os.listdir(scratch / "done" / job.id) == ["result.json"]


def test_only_declared_outputs_survive_publication(scratch: Path, roots: Path) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    enqueue(scratch, job)

    with fake_processor(scratch, roots, "extra-files") as serving:
        serving.run(until_idle=True)

    assert published(scratch, job).status == "ok"
    assert sorted(os.listdir(scratch / "done" / job.id)) == ["result.json", "thumbnail.webp"]


def test_a_child_past_its_wall_clock_is_killed_and_reported(scratch: Path, roots: Path) -> None:
    limits = Limits(wall_seconds=1, cpu_seconds=10, memory_bytes=LIMITS.memory_bytes,
                    output_bytes=LIMITS.output_bytes)
    job = make_job(roots, b"album/photo.jpg", "thumbnail", limits=limits)
    enqueue(scratch, job)
    started = time.monotonic()

    with fake_processor(scratch, roots, "sleep") as serving:
        serving.run(until_idle=True)

    assert time.monotonic() - started < 15
    assert published(scratch, job) == Result(job.id, "timeout", "timeout", None, ())


# -- shutdown ------------------------------------------------------------------------------------


def test_shutdown_kills_children_and_returns_their_jobs_from_memory(
    scratch: Path, roots: Path,
) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    enqueue(scratch, job)
    original = (scratch / "inbox" / f"{job.id}.json").read_bytes()
    forged = replace(job, limits=replace(job.limits, memory_bytes=16 << 30))

    with fake_processor(scratch, roots, "sleep") as serving:
        serving.prepare()
        assert serving.fill() == 1
        assert serving.running == 1
        child = next(iter(serving._running.values())).child.process
        # A compromised child cannot do this (Landlock), but the loop must not care: it
        # re-enqueues the job it validated, never the file on disk.
        (scratch / "work" / job.id / "job.json").chmod(0o660)
        (scratch / "work" / job.id / "job.json").write_bytes(encode_job(forged))
        started = time.monotonic()
        serving.request_stop()
        serving.wait()
        assert time.monotonic() - started < serving.poll_seconds
        serving.shutdown()

    assert child.returncode is not None
    assert os.listdir(scratch / "work") == []
    assert os.listdir(scratch / "done") == []
    assert (scratch / "inbox" / f"{job.id}.json").read_bytes() == original


def test_the_child_gets_only_its_input_descriptor_and_no_root_path(
    scratch: Path, roots: Path,
) -> None:
    job = make_job(roots, b"album/photo.jpg", "thumbnail")
    enqueue(scratch, job)
    other = str(roots / "synthetic" / "album" / "photo.jpg")

    with fake_processor(scratch, roots, "inspect", other) as serving:
        serving.run(until_idle=True)

    assert published(scratch, job) == Result(job.id, "unsupported", "unsupported_format", None,
                                             ())


@pytest.mark.parametrize("behaviour,status", [
    ("orphan", "failed"), ("orphan-sleep", "timeout"), ("tool-sleep", "timeout"),
])
def test_nothing_a_job_started_outlives_it(
    scratch: Path, roots: Path, behaviour: str, status: str,
) -> None:
    marker = f"aegis-stray-{uuid.uuid4().hex}"
    limits = Limits(wall_seconds=2, cpu_seconds=10, memory_bytes=LIMITS.memory_bytes,
                    output_bytes=LIMITS.output_bytes)
    job = make_job(roots, b"album/photo.jpg", "thumbnail", limits=limits)
    enqueue(scratch, job)

    with fake_processor(scratch, roots, behaviour, marker) as serving:
        serving.run(until_idle=True)

    assert published(scratch, job).status == status
    assert alive(marker) == []


def test_stray_detection_spares_running_jobs() -> None:
    me = 100
    parents = {100: 1, 200: 100, 201: 200, 300: 100, 301: 300, 302: 301, 400: 1, 401: 400}

    strays = {pid for pid in parents if _is_stray(pid, parents, me, {200})}

    # 200 is a running job's child (with its tool 201); 300 and below were orphaned to the loop.
    assert strays == {300, 301, 302}


def test_the_loop_never_imports_a_decoder() -> None:
    code = ("import sys, aegis_media_processor.loop\n"
            "assert not {'PIL', 'pillow_heif'} & set(sys.modules), sorted(sys.modules)\n")

    completed = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True,
                               timeout=60, check=False)

    assert completed.returncode == 0, completed.stderr


def test_sigterm_stops_the_processor_cleanly(tmp_path: Path, scratch: Path, roots: Path) -> None:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("AEGIS_")}
    environment["AEGIS_MEDIA_PROCESSOR_CONCURRENCY"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-I", "-m", "aegis_media_processor", "--scratch", str(scratch),
         "--roots", str(roots)],
        env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not (scratch / "done").is_dir():
            assert process.poll() is None, process.stderr.read() if process.stderr else b""
            assert time.monotonic() < deadline
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if process.stderr is not None:
            process.stderr.close()


def test_invalid_configuration_stops_the_processor_before_it_starts(
    scratch: Path, roots: Path,
) -> None:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("AEGIS_")}
    environment["AEGIS_MEDIA_PROCESSOR_CONCURRENCY"] = "99"

    completed = subprocess.run(
        [sys.executable, "-I", "-m", "aegis_media_processor", "--scratch", str(scratch),
         "--roots", str(roots)],
        env=environment, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
        check=False,
    )

    assert completed.returncode == 2
    assert b"AEGIS_MEDIA_PROCESSOR_CONCURRENCY" in completed.stderr
    assert os.listdir(scratch) == []
