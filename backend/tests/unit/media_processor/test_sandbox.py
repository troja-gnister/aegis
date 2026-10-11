"""Tool children: memory, CPU, file size, wall clock, output, environment and descriptors."""

from __future__ import annotations

import json
import os
import resource
import signal
import sys
import time
from pathlib import Path

import pytest
from aegis_media_processor.sandbox import (
    CPU_GRACE_SECONDS,
    ChildLimits,
    Completed,
    run,
    scrubbed_environment,
)

MiB = 1 << 20


def limits(**changes: float) -> ChildLimits:
    values: dict[str, float] = {
        "memory_bytes": 512 * MiB, "cpu_seconds": 30, "wall_seconds": 30,
        "output_bytes": 8 * MiB,
    }
    values.update(changes)
    return ChildLimits(**values)  # type: ignore[arg-type]


def python(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def report(tmp_path: Path, code: str, **changes: float) -> object:
    completed = run(python(code), limits(**changes), cwd=str(tmp_path))
    assert completed.returncode == 0, completed
    return json.loads(completed.stdout)


def test_address_space_limit_makes_a_large_allocation_fail(tmp_path: Path) -> None:
    code = (
        "import sys\n"
        "try:\n"
        "    block = bytearray(1024 * 1024 * 1024)\n"
        "except MemoryError:\n"
        "    sys.exit(42)\n"
    )

    completed = run(python(code), limits(memory_bytes=256 * MiB), cwd=str(tmp_path))

    assert completed.returncode == 42
    assert not completed.timed_out


def test_cpu_limit_stops_a_busy_loop_with_sigxcpu(tmp_path: Path) -> None:
    started = time.monotonic()

    completed = run(python("while True:\n    pass\n"), limits(cpu_seconds=1), cwd=str(tmp_path))

    assert completed.returncode in (-signal.SIGXCPU, -signal.SIGKILL)
    assert completed.failure_code in ("cpu_limit", "crashed")
    assert not completed.timed_out
    assert time.monotonic() - started < 1 + CPU_GRACE_SECONDS + 10


def test_wall_clock_limit_kills_a_sleeping_child(tmp_path: Path) -> None:
    started = time.monotonic()

    completed = run(python("import time\ntime.sleep(60)\n"), limits(wall_seconds=1),
                    cwd=str(tmp_path))

    assert completed.timed_out
    assert completed.returncode == -signal.SIGKILL
    assert completed.failure_code == "timeout"
    assert time.monotonic() - started < 10


def test_file_size_limit_refuses_writes_past_the_output_limit(tmp_path: Path) -> None:
    code = (
        "import errno, sys\n"
        "try:\n"
        "    with open('big.bin', 'wb') as handle:\n"
        "        handle.write(b'x' * (2 * 1024 * 1024))\n"
        "except OSError as error:\n"
        "    sys.exit(43 if error.errno == errno.EFBIG else 1)\n"
    )

    completed = run(python(code), limits(output_bytes=MiB), cwd=str(tmp_path))

    assert completed.returncode == 43
    assert (tmp_path / "big.bin").stat().st_size <= MiB


def test_a_native_tool_past_the_file_size_limit_dies_of_sigxfsz(tmp_path: Path) -> None:
    command = ["/bin/sh", "-c", "exec head -c 2000000 /dev/zero > big.bin"]

    completed = run(command, limits(output_bytes=MiB), cwd=str(tmp_path))

    assert completed.returncode == -signal.SIGXFSZ
    assert completed.failure_code == "output_limit"
    assert (tmp_path / "big.bin").stat().st_size <= MiB


def test_stdout_past_its_bound_kills_the_child(tmp_path: Path) -> None:
    code = "import sys, time\nsys.stdout.write('x' * 1048576)\nsys.stdout.flush()\ntime.sleep(60)\n"

    completed = run(python(code), ChildLimits(512 * MiB, 30, 30, 8 * MiB, stdout_bytes=4096),
                    cwd=str(tmp_path))

    assert completed.stdout_overflow
    assert len(completed.stdout) == 4096
    assert completed.failure_code == "invalid_result"


def test_the_environment_is_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_TEST_SECRET", "must-not-leak")
    monkeypatch.setenv("LD_PRELOAD", "/nonexistent/preload.so")

    environment = report(tmp_path, "import json, os\nprint(json.dumps(dict(os.environ)))")

    assert environment == {
        "PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "MALLOC_ARENA_MAX": "2",
    }


def test_extra_environment_is_validated() -> None:
    assert scrubbed_environment({"TMPDIR": "/tmp/job"})["TMPDIR"] == "/tmp/job"
    for extra in ({"PATH": "/tmp"}, {"lower": "x"}, {"BAD-NAME": "x"}, {"VALUE": "a\0b"}):
        with pytest.raises(ValueError):
            scrubbed_environment(extra)


def test_rlimits_niceness_and_no_new_privs_are_applied(tmp_path: Path) -> None:
    code = (
        "import json, os, resource\n"
        "names = ('RLIMIT_AS', 'RLIMIT_CPU', 'RLIMIT_FSIZE', 'RLIMIT_NOFILE', 'RLIMIT_CORE')\n"
        "status = open('/proc/self/status').read()\n"
        "found = {n: resource.getrlimit(getattr(resource, n)) for n in names}\n"
        "print(json.dumps({'limits': found, 'nice': os.nice(0), 'cwd': os.getcwd(),\n"
        "                  'nnp': 'NoNewPrivs:\\t1' in status}))\n"
    )
    parent_nice = os.nice(0)

    observed = report(tmp_path, code, memory_bytes=300 * MiB, cpu_seconds=7, output_bytes=3 * MiB)

    def bounded(name: str, value: int) -> list[int]:
        _, hard = resource.getrlimit(getattr(resource, name))
        if hard != resource.RLIM_INFINITY:
            value = min(value, hard)
        return [value, value]

    assert isinstance(observed, dict)
    assert observed["limits"]["RLIMIT_AS"] == bounded("RLIMIT_AS", 300 * MiB)
    assert observed["limits"]["RLIMIT_FSIZE"] == bounded("RLIMIT_FSIZE", 3 * MiB)
    assert observed["limits"]["RLIMIT_NOFILE"] == bounded("RLIMIT_NOFILE", 64)
    assert observed["limits"]["RLIMIT_CORE"] == [0, 0]
    assert observed["limits"]["RLIMIT_CPU"][0] <= 7
    assert observed["limits"]["RLIMIT_CPU"][1] <= 7 + CPU_GRACE_SECONDS
    assert observed["nice"] == min(parent_nice + 10, 19)
    assert observed["cwd"] == str(tmp_path)
    assert observed["nnp"] is True


def test_only_explicitly_passed_descriptors_are_inherited(tmp_path: Path) -> None:
    leaked = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    passed = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    os.set_inheritable(leaked, True)
    try:
        code = (
            "import json, os\n"
            "def alive(n):\n"
            "    try:\n"
            "        return os.fstat(n).st_ino\n"
            "    except OSError:\n"
            "        return None\n"
            f"print(json.dumps([alive({leaked}), alive({passed})]))\n"
        )
        completed = run(python(code), limits(), cwd=str(tmp_path), pass_fds=(passed,))
    finally:
        os.close(leaked)
        os.close(passed)

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == [None, tmp_path.stat().st_ino]


def test_a_deadline_kills_the_whole_process_group(tmp_path: Path) -> None:
    code = (
        "import subprocess, sys, time\n"
        "sleeper = [sys.executable, '-I', '-c', 'import time; time.sleep(60)']\n"
        "grandchild = subprocess.Popen(sleeper)\n"
        "print(grandchild.pid, flush=True)\n"
        "time.sleep(60)\n"
    )

    completed = run(python(code), limits(wall_seconds=2), cwd=str(tmp_path))
    grandchild = int(completed.stdout.split()[0])
    deadline = time.monotonic() + 10
    state = "R"
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{grandchild}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            state = "gone"
        if state in ("gone", "Z", "X"):
            break
        time.sleep(0.1)

    assert completed.timed_out
    assert state in ("gone", "Z", "X")


def test_commands_are_argument_arrays_only(tmp_path: Path) -> None:
    for command in ("echo unsafe", b"echo unsafe", [], ["echo", "a\0b"], ["echo", 1]):
        with pytest.raises(TypeError):
            run(command, limits(), cwd=str(tmp_path))  # type: ignore[arg-type]


@pytest.mark.parametrize("changes", [
    {"memory_bytes": 0}, {"cpu_seconds": 0}, {"output_bytes": -1}, {"wall_seconds": 0},
    {"wall_seconds": 86_401}, {"nice": 20}, {"nice": -1}, {"open_files": 0},
    {"memory_bytes": 1.5},
])
def test_limits_are_validated(changes: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        limits(**changes)


@pytest.mark.parametrize("completed,code", [
    (Completed(0, False, b"", False), None),
    (Completed(-signal.SIGKILL, True, b"", False), "timeout"),
    (Completed(-signal.SIGKILL, True, b"x", True), "invalid_result"),
    (Completed(-signal.SIGXCPU, False, b"", False), "cpu_limit"),
    (Completed(-signal.SIGXFSZ, False, b"", False), "output_limit"),
    (Completed(-signal.SIGSEGV, False, b"", False), "crashed"),
    (Completed(1, False, b"", False), "tool_failed"),
])
def test_failure_codes(completed: Completed, code: str | None) -> None:
    assert completed.failure_code == code
