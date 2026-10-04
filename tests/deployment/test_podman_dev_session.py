from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from aegisctl.container_engine import podman_socket

from scripts.podman_dev import __main__ as cli
from scripts.podman_dev import api, lifecycle, session
from scripts.podman_dev.records import (
    RECORD_NAME,
    ApiRecord,
    PathIdentity,
    SessionError,
    SessionRecord,
    WebkitRecord,
    load_record,
    save_record,
)
from tests.support.podman_dev_doubles import ScriptedPodman, listing_replies, write_proc_entry

COMMAND = ["podman", "--remote=false", "system", "service", "--time=0", "unix:///x/podman.sock"]
CONTAINER = "c" * 64
IMAGE = "e" * 64
ROOTLESS = (("info", "--format", "{{.Host.Security.Rootless}}"), 0, "true\n")


@pytest.fixture
def runtime(tmp_path: Path) -> dict[str, str]:
    directory = tmp_path / "run"
    directory.mkdir()
    directory.chmod(0o700)
    return {"XDG_RUNTIME_DIR": str(directory)}


def state_of(runtime: dict[str, str]) -> Path:
    return Path(runtime["XDG_RUNTIME_DIR"]) / session.STATE_NAME


def api_record(tmp_path: Path, *, pid: int = 4242, start_time: int = 111) -> ApiRecord:
    directory = tmp_path / "api-dir"
    directory.mkdir(mode=0o700)
    socket_path = directory / "podman.sock"
    socket_path.write_bytes(b"")
    return ApiRecord(
        pid=pid, start_time=start_time, command=list(COMMAND), socket=str(socket_path),
        directory_identity=PathIdentity.of(directory),
        socket_identity=PathIdentity.of(socket_path),
    )


# --- process identity -----------------------------------------------------------------


def test_read_process_identity_handles_parentheses_in_the_command_name(tmp_path: Path) -> None:
    write_proc_entry(tmp_path, 7, start_time=123456, command=COMMAND)
    assert api.read_process_identity(7, tmp_path) == api.ProcessIdentity(123456, tuple(COMMAND))


def test_zombie_and_missing_processes_count_as_exited(tmp_path: Path) -> None:
    write_proc_entry(tmp_path, 7, start_time=1, command=COMMAND, state="Z")
    assert api.read_process_identity(7, tmp_path) is None
    assert api.read_process_identity(8, tmp_path) is None


# --- preflight and exports ------------------------------------------------------------


@pytest.mark.parametrize(("change", "message"), [
    ({"euid": 0}, "root"),
    ({"rootless": False}, "rootless"),
    ({"selinux_enforce": "0\n"}, "SELinux"),
    ({"remote": "CONTAINER_HOST"}, "CONTAINER_HOST"),
    ({"mode": 0o755}, "XDG_RUNTIME_DIR"),
    ({"state": True}, "run stop first"),
])
def test_preflight_refuses_unsafe_hosts(
    runtime: dict[str, str], change: dict[str, object], message: str,
) -> None:
    environ = dict(runtime)
    if "remote" in change:
        environ[str(change["remote"])] = "unix:///elsewhere"
    if "mode" in change:
        Path(runtime["XDG_RUNTIME_DIR"]).chmod(int(str(change["mode"])))
    if change.get("state"):
        state_of(runtime).mkdir()
    problems = session.preflight_problems(
        environ,
        euid=int(str(change.get("euid", os.geteuid()))),
        rootless=bool(change.get("rootless", True)),
        selinux_enforce=str(change.get("selinux_enforce", "1\n")),
    )
    assert any(message in problem for problem in problems), problems


def test_preflight_accepts_a_safe_host(runtime: dict[str, str]) -> None:
    assert session.preflight_problems(
        runtime, euid=os.geteuid(), rootless=True, selinux_enforce="1\n",
    ) == []


def full_record(socket_path: str) -> SessionRecord:
    return SessionRecord(
        state_identity=PathIdentity(1, 2),
        api=ApiRecord(
            pid=1, start_time=2, command=["podman"], socket=socket_path,
            directory_identity=PathIdentity(3, 4), socket_identity=PathIdentity(5, 6),
        ),
        webkit=WebkitRecord(CONTAINER, IMAGE, 41234, "a" * 32),
    )


def test_export_lines_quote_shell_metacharacters() -> None:
    text = session.export_lines(
        full_record("/run/user/1000/a b/$x/podman.sock"),
        node_bin=Path("/home/o'brien/node/bin"),
        compose_provider=Path("/home/x/.local/libexec/aegis/docker-compose"),
    )
    assert text == (
        "export AEGIS_CONTAINER_ENGINE=podman\n"
        "export PODMAN_COMPOSE_PROVIDER=/home/x/.local/libexec/aegis/docker-compose\n"
        "export AEGIS_PODMAN_SOCKET='/run/user/1000/a b/$x/podman.sock'\n"
        f"export E2E_WEBKIT_WS_ENDPOINT=ws://127.0.0.1:41234/{'a' * 32}\n"
        "export PATH='/home/o'\"'\"'brien/node/bin':\"$PATH\"\n"
    )


def test_export_lines_round_trip_through_bash() -> None:
    text = session.export_lines(
        full_record("/run/user/1000/a b/$x/podman.sock"),
        node_bin=Path("/home/o'brien/node/bin"),
        compose_provider=Path("/home/x/.local/libexec/aegis/docker-compose"),
    )
    shown = subprocess.run(
        ["bash", "-c", text + 'printf "%s\\n%s\\n" "$AEGIS_PODMAN_SOCKET" "${PATH%%:*}"'],
        capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"},
    )
    assert shown.stdout == "/run/user/1000/a b/$x/podman.sock\n/home/o'brien/node/bin\n"


def test_export_lines_refuse_an_incomplete_record() -> None:
    with pytest.raises(SessionError, match="incomplete"):
        session.export_lines(
            SessionRecord(state_identity=PathIdentity(1, 2)),
            node_bin=Path("/n"), compose_provider=Path("/c"),
        )


# --- API service ----------------------------------------------------------------------


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def test_launch_api_records_identity_and_a_private_socket_directory(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    def popen(command: Sequence[str], **options: object) -> FakeProcess:
        assert options["start_new_session"] is True
        write_proc_entry(proc, 4242, start_time=555, command=command)
        return FakeProcess(4242)

    record = api.launch_api(state, popen=popen, proc=proc)
    socket_path = Path(record.socket)
    assert record.command[:5] == ["podman", "--remote=false", "system", "service", "--time=0"]
    assert record.command[5] == f"unix://{socket_path}"
    assert (record.pid, record.start_time, record.socket_identity) == (4242, 555, None)
    assert socket_path.parent.parent == state
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / session.LOG_NAME).stat().st_mode) == 0o600


def test_launch_api_removes_its_directory_when_podman_is_missing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    def popen(command: Sequence[str], **options: object) -> FakeProcess:
        raise FileNotFoundError("podman")

    with pytest.raises(SessionError, match="could not start"):
        api.launch_api(state, popen=popen, proc=tmp_path / "proc")
    assert [path.name for path in state.iterdir()] == [session.LOG_NAME]


def test_session_socket_layout_satisfies_the_guarded_launcher() -> None:
    with tempfile.TemporaryDirectory(prefix="pd-", dir="/tmp") as base:
        runtime = Path(base) / "run"
        runtime.mkdir(mode=0o700)
        state = runtime / session.STATE_NAME
        state.mkdir(mode=0o700)
        proc = Path(base) / "proc"

        def popen(command: Sequence[str], **options: object) -> FakeProcess:
            write_proc_entry(proc, 9, start_time=1, command=command)
            return FakeProcess(9)

        record = api.launch_api(state, popen=popen, proc=proc)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(record.socket)
            accepted = podman_socket({"AEGIS_PODMAN_SOCKET": record.socket})
        assert accepted == Path(record.socket)


def test_wait_until_ready_fails_when_the_process_exits(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    with pytest.raises(SessionError, match="exited during startup"):
        api.wait_until_ready(record, proc=tmp_path / "proc", probe=lambda path: True)


def test_wait_until_ready_returns_the_socket_identity(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    write_proc_entry(tmp_path / "proc", 4242, start_time=111, command=COMMAND)
    identity = api.wait_until_ready(record, proc=tmp_path / "proc", probe=lambda path: True)
    assert identity == PathIdentity.of(Path(record.socket))


def test_stop_api_refuses_a_reused_pid(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    write_proc_entry(tmp_path / "proc", 4242, start_time=999, command=COMMAND)
    signals: list[tuple[int, int]] = []
    with pytest.raises(SessionError, match="no longer matches"):
        api.stop_api(record, proc=tmp_path / "proc",
                     kill=lambda pid, number: signals.append((pid, number)))
    assert signals == []
    assert Path(record.socket).exists()


def test_stop_api_terminates_then_removes_recorded_entries(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    proc = tmp_path / "proc"
    write_proc_entry(proc, 4242, start_time=111, command=COMMAND)
    signals: list[tuple[int, int]] = []

    def kill(pid: int, number: int) -> None:
        signals.append((pid, number))
        for entry in (proc / str(pid)).iterdir():
            entry.unlink()
        (proc / str(pid)).rmdir()

    api.stop_api(record, proc=proc, kill=kill, sleep=lambda seconds: None)
    assert signals == [(4242, signal.SIGTERM)]
    assert not Path(record.socket).parent.exists()


def test_stop_api_recovers_when_process_is_already_gone(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    signals: list[tuple[int, int]] = []
    api.stop_api(record, proc=tmp_path / "proc",
                 kill=lambda pid, number: signals.append((pid, number)))
    assert signals == []
    assert not Path(record.socket).parent.exists()


def test_stop_api_refuses_a_replaced_socket(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    socket_path = Path(record.socket)
    socket_path.unlink()
    (socket_path.parent / "keep-inode-busy").write_bytes(b"")
    socket_path.write_bytes(b"replacement")
    with pytest.raises(SessionError, match="not the recorded socket"):
        api.stop_api(record, proc=tmp_path / "proc")
    assert socket_path.read_bytes() == b"replacement"


def test_stop_api_leaves_a_process_that_ignores_sigterm(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    write_proc_entry(tmp_path / "proc", 4242, start_time=111, command=COMMAND)
    clock = iter([0.0, 0.0, 25.0])
    with pytest.raises(SessionError, match="ignored SIGTERM"):
        api.stop_api(record, proc=tmp_path / "proc", kill=lambda pid, number: None,
                     sleep=lambda seconds: None, now=lambda: next(clock))
    assert Path(record.socket).exists()


# --- lifecycle ------------------------------------------------------------------------


def allow_host(monkeypatch: pytest.MonkeyPatch, *, selinux: str = "1\n") -> None:
    monkeypatch.setattr(lifecycle, "selinux_enforce", lambda: selinux)
    monkeypatch.setattr(lifecycle.tools, "installed_state", lambda tool, home: "verified")


def write_session(
    runtime: dict[str, str], record_api: ApiRecord | None = None,
    record_webkit: WebkitRecord | None = None, baseline: object = None,
) -> Path:
    state = state_of(runtime)
    state.mkdir(mode=0o700)
    save_record(state, SessionRecord(
        state_identity=PathIdentity.of(state), api=record_api, webkit=record_webkit,
    ))
    empty = {"containers": {}, "volumes": {}, "networks": {}, "images": {}}
    (state / session.BASELINE_NAME).write_text(json.dumps(baseline or empty), encoding="utf-8")
    return state


def test_start_refuses_on_a_failed_preflight_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    allow_host(monkeypatch, selinux="0\n")
    with pytest.raises(SessionError, match="SELinux"):
        lifecycle.start(runtime, home=tmp_path, run=ScriptedPodman([ROOTLESS]))
    assert not state_of(runtime).exists()


def test_start_refuses_when_state_exists_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    allow_host(monkeypatch)
    state_of(runtime).mkdir(mode=0o700)
    (state_of(runtime) / RECORD_NAME).write_text("sentinel", encoding="utf-8")
    with pytest.raises(SessionError, match="run stop first"):
        lifecycle.start(runtime, home=tmp_path, run=ScriptedPodman([ROOTLESS]))
    assert (state_of(runtime) / RECORD_NAME).read_text(encoding="utf-8") == "sentinel"


def test_start_keeps_a_record_when_the_api_launch_fails(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    allow_host(monkeypatch)

    def failing_launch(state: Path) -> ApiRecord:
        assert (state / RECORD_NAME).is_file()
        raise SessionError("launch failed")

    monkeypatch.setattr(lifecycle.api, "launch_api", failing_launch)
    run = ScriptedPodman([ROOTLESS, *listing_replies()])
    with pytest.raises(SessionError, match="launch failed"):
        lifecycle.start(runtime, home=tmp_path, run=run)
    record = load_record(state_of(runtime))
    assert (record.api, record.webkit) == (None, None)
    assert (state_of(runtime) / session.BASELINE_NAME).is_file()
    assert stat.S_IMODE(state_of(runtime).stat().st_mode) == 0o700


def test_stop_removes_webkit_before_api_and_cleans_state(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(lifecycle.webkit, "remove_webkit",
                        lambda record, run: order.append("webkit"))
    monkeypatch.setattr(lifecycle.api, "stop_api", lambda record: order.append("api"))
    write_session(runtime, api_record(tmp_path), WebkitRecord(CONTAINER, IMAGE, 41234, "a" * 32))
    report, unchanged = lifecycle.stop(runtime, run=ScriptedPodman(listing_replies()))
    assert order == ["webkit", "api"]
    assert unchanged is True
    assert "inventory: no pre-existing Podman object removed or changed" in report
    assert not state_of(runtime).exists()


def test_stop_reports_changed_inventory_and_still_cleans_up(runtime: dict[str, str]) -> None:
    baseline = {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1]},
        "volumes": {}, "networks": {}, "images": {},
    }
    write_session(runtime, baseline=baseline)
    report, unchanged = lifecycle.stop(runtime, run=ScriptedPodman(listing_replies()))
    assert unchanged is False
    assert any(CONTAINER in line and "disappeared" in line for line in report)
    assert not state_of(runtime).exists()


def test_stop_refuses_a_replaced_state_directory(runtime: dict[str, str]) -> None:
    state = write_session(runtime)
    save_record(state, SessionRecord(state_identity=PathIdentity(0, 0)))
    with pytest.raises(SessionError, match="replaced"):
        lifecycle.stop(runtime, run=ScriptedPodman([]))
    assert (state / RECORD_NAME).is_file()


def test_stop_refuses_unexpected_state_entries(runtime: dict[str, str]) -> None:
    state = write_session(runtime)
    (state / "notes.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(SessionError, match="unexpected entries"):
        lifecycle.stop(runtime, run=ScriptedPodman(listing_replies()))
    assert (state / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_environment_refuses_a_dead_session(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    write_session(runtime, api_record(tmp_path), WebkitRecord(CONTAINER, IMAGE, 41234, "a" * 32))
    monkeypatch.setattr(lifecycle.api, "process_matches", lambda record: False)
    run = ScriptedPodman([(("container", "inspect"), 0, f"{CONTAINER} {IMAGE} true")])
    with pytest.raises(SessionError, match="no healthy session"):
        lifecycle.environment(runtime, home=tmp_path, run=run)


def test_cli_env_without_session_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime["XDG_RUNTIME_DIR"])
    assert cli.main(["env"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no healthy session" in captured.err
