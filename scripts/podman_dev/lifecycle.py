"""Start, check and stop one helper session; every removal is by recorded identity."""
from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from scripts.podman_dev import api, inventory, podman, session, tools, webkit
from scripts.podman_dev.records import (
    RECORD_NAME,
    TEMPORARY_NAME,
    PathIdentity,
    SessionError,
    SessionRecord,
    WebkitRecord,
    load_record,
    save_record,
)

SELINUX_ENFORCE = Path("/sys/fs/selinux/enforce")
STATE_ENTRIES = frozenset({
    RECORD_NAME, TEMPORARY_NAME, session.BASELINE_NAME, session.LOG_NAME,
})


def selinux_enforce(path: Path = SELINUX_ENFORCE) -> str:
    try:
        return path.read_text(encoding="ascii")
    except OSError:
        return ""


def podman_rootless(run: podman.Runner) -> bool:
    result = run(["info", "--format", "{{.Host.Security.Rootless}}"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def _write_private_json(path: Path, value: object) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)


def start(
    environ: Mapping[str, str], *, home: Path, run: podman.Runner = podman.run,
) -> SessionRecord:
    problems = session.preflight_problems(
        environ, euid=os.geteuid(), rootless=podman_rootless(run),
        selinux_enforce=selinux_enforce(),
    )
    for name in ("node", "docker-compose"):
        tool = tools.load_lock()[name]
        if tools.installed_state(tool, home) != "verified":
            problems.append(f"{name} {tool.version} is not installed and verified; "
                            "run the tools command first")
    if problems:
        raise SessionError("\n".join(problems))
    state = session.state_directory(environ)
    try:
        state.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SessionError(f"a previous session exists in {state}; run stop first") from error
    state.chmod(0o700)
    record = SessionRecord(state_identity=PathIdentity.of(state))
    save_record(state, record)
    _write_private_json(state / session.BASELINE_NAME, inventory.snapshot(run))
    record.api = api.launch_api(state)
    save_record(state, record)
    record.api.socket_identity = api.wait_until_ready(record.api)
    save_record(state, record)

    def remember(created: WebkitRecord) -> None:
        record.webkit = created
        save_record(state, record)

    webkit.start_webkit(remember, run)
    return record


def _open_session(environ: Mapping[str, str]) -> tuple[Path, SessionRecord] | None:
    state = session.state_directory(environ)
    if not os.path.lexists(state):
        return None
    metadata = state.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SessionError(f"{state} is not a private directory you own; nothing was changed")
    if not (state / RECORD_NAME).is_file():
        raise SessionError(f"{state} has no session record; inspect it by hand; "
                           "nothing was changed")
    record = load_record(state)
    if PathIdentity.of(state) != record.state_identity:
        raise SessionError(f"{state} was replaced since the session started; nothing was changed")
    return state, record


def _check(
    environ: Mapping[str, str], run: podman.Runner,
) -> tuple[list[str], bool, SessionRecord | None]:
    opened = _open_session(environ)
    if opened is None:
        return ["no session"], False, None
    record = opened[1]
    lines: list[str] = []
    healthy = True
    socket_path = Path(record.api.socket) if record.api else None
    if record.api is None or record.api.socket_identity is None or socket_path is None:
        lines.append("api: not started")
        healthy = False
    elif (
        api.process_matches(record.api)
        and os.path.lexists(socket_path)
        and PathIdentity.of(socket_path) == record.api.socket_identity
        and api.ping(socket_path)
    ):
        lines.append(f"api: ready pid={record.api.pid} socket={record.api.socket}")
    else:
        lines.append("api: NOT RUNNING (run stop, then start)")
        healthy = False
    if record.webkit is None:
        lines.append("webkit: not started")
        healthy = False
    elif webkit.webkit_running(record.webkit, run):
        lines.append(f"webkit: ready ws://127.0.0.1:{record.webkit.port}/<token>")
    else:
        lines.append("webkit: NOT RUNNING (run stop, then start)")
        healthy = False
    return lines, healthy, record


def status(environ: Mapping[str, str], run: podman.Runner = podman.run) -> tuple[list[str], bool]:
    lines, healthy, _record = _check(environ, run)
    return lines, healthy


def environment(
    environ: Mapping[str, str], *, home: Path, run: podman.Runner = podman.run,
) -> str:
    lines, healthy, record = _check(environ, run)
    if not healthy or record is None:
        raise SessionError("no healthy session:\n" + "\n".join(lines))
    return session.export_lines(
        record, node_bin=tools.tool_path("node", home) / "bin",
        compose_provider=tools.tool_path("docker-compose", home),
    )


def stop(environ: Mapping[str, str], run: podman.Runner = podman.run) -> tuple[list[str], bool]:
    """Stop everything recorded; return the report and whether pre-existing objects held."""
    opened = _open_session(environ)
    if opened is None:
        return ["no session to stop"], True
    state, record = opened
    report: list[str] = []
    if record.webkit is not None:
        webkit.remove_webkit(record.webkit, run)
        record.webkit = None
        save_record(state, record)
        report.append("webkit: removed")
    if record.api is not None:
        api.stop_api(record.api)
        record.api = None
        save_record(state, record)
        report.append("api: stopped")
    unchanged = True
    baseline = state / session.BASELINE_NAME
    if baseline.is_file():
        before = json.loads(baseline.read_text(encoding="utf-8"))
        problems = inventory.compare(before, inventory.snapshot(run))
        unchanged = not problems
        report += problems or ["inventory: no pre-existing Podman object removed or changed"]
    else:
        report.append("inventory: no baseline was recorded")
    unexpected = sorted({entry.name for entry in state.iterdir()} - STATE_ENTRIES)
    if unexpected:
        raise SessionError(f"{state} holds unexpected entries {unexpected}; left in place")
    for name in sorted(STATE_ENTRIES - {RECORD_NAME}):
        (state / name).unlink(missing_ok=True)
    (state / RECORD_NAME).unlink()
    state.rmdir()
    return report, unchanged
