from __future__ import annotations

import json
import os
import re
import resource
import selectors
import subprocess
import sys
from dataclasses import replace
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn

import pytest
import yaml
from aegisctl.cli import main
from aegisctl.container_engine import REMOTE_VARIABLES
from aegisctl.mounts import (
    MAX_MOUNTINFO_BYTES,
    ConfigError,
    ValidatedSlot,
    local_identity,
    observe_mount_fingerprints,
    observe_mount_fingerprints_podman_run,
    parse_config,
    preflight_slots,
)

from tests.support.observer_engine import ObserverEngine

IMAGE = "registry.example.test/aegis/aegis-gateway@sha256:" + "b" * 64
LOCAL_IMAGE_ID = "c" * 64
READER_TYPE = "aegis_reader.process"
CONTAINER_ID = "d" * 64
PODMAN_COMMA_REFUSAL = (
    "mount slot photos: Podman cannot bind-mount an original root whose path contains a comma"
)
NAME_RE = re.compile(r"aegis-preflight-[0-9a-f]{16}")
TOKEN_RE = re.compile(r"[0-9a-f]{32}")
DIAGNOSTICS = {"mountinfo.out", "observer.stderr", "observer.argv.json"}
REAL_POPEN = subprocess.Popen
# The stand-in for the podman client: a plain local Python child, never a container.
CLIENT = r"""
import os, resource, sys, time
behavior, records = sys.argv[1], sys.argv[2]
os.write(2, b"client diagnostic\n")
if behavior == "overflow":
    while True:
        os.write(1, b"x" * 65536)
if behavior == "sleep":
    time.sleep(60)
if behavior == "stderr-flood":
    for _ in range(64):
        os.write(2, b"e" * 4096)
os.write(1, records.encode())
if behavior == "rlimit":
    os.write(2, repr(resource.getrlimit(resource.RLIMIT_FSIZE)).encode())
sys.exit(0 if behavior == "records" else 125)
"""


def _observer_script(targets: list[str]) -> str:
    expression = " || ".join(
        f'($5 == "{target}" || index($5, "{target}/") == 1)' for target in targets
    )
    return (
        "awk 'length($0) > 16384 { exit 65 } NR > 8192 { exit 66 } "
        f"({expression}) {{ print; matched++; if (matched > {len(targets) * 2}) exit 67 }}"
        "' /proc/self/mountinfo"
    )


def _slots(tmp_path: Path, names: tuple[str, ...] = ("private-canary",)) -> tuple[
    ValidatedSlot, ...
]:
    blocks = []
    for slot_id, name in zip(("photos", "uploads"), names, strict=False):
        source = tmp_path / name
        source.mkdir()
        blocks.append(
            "[[slots]]\n"
            f"slot_id = {json.dumps(slot_id)}\n"
            f"source = {json.dumps(str(source))}\n"
            f'container_path = "/srv/aegis/roots/{slot_id}"\n'
            'mode = "read_only"\n'
            f"expected_identity = {json.dumps(local_identity(source))}\n"
        )
    config = tmp_path / "mounts.toml"
    config.write_text("version = 1\n" + "\n".join(blocks), encoding="utf-8")
    return preflight_slots(parse_config(config))


class PodmanRunEngine:
    """The plain podman CLI boundary: exact-name existence, run, inspect and ID removal.

    ``run`` goes through ``popen``, which spawns a local Python child in place of the client.
    """

    def __init__(
        self, *, behavior: str = "records", failure: BaseException | None = None,
        preexisting: bool = False, label: str | None = None, name: str | None = None,
        remains: bool = False, vanishes: bool = False, inspect_output: str | None = None,
        rm_returncode: int = 0, undecodable_exists: bool = False,
    ) -> None:
        self.behavior = behavior
        self.failure = failure
        self.present = preexisting
        self.label = label
        self.name = name
        self.remains = remains
        self.vanishes = vanishes
        self.inspect_output = inspect_output
        self.rm_returncode = rm_returncode
        self.undecodable_exists = undecodable_exists
        self.commands: list[list[str]] = []
        self.popen_kwargs: dict[str, Any] = {}
        self.client: subprocess.Popen[bytes] | None = None
        self.container_name = ""
        self.token = ""

    def __call__(self, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(arguments))
        assert arguments[:2] == ["podman", "--remote=false"]
        assert not REMOTE_VARIABLES & set(kwargs["env"])
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 30
        args = arguments[2:]
        if args[:2] == ["container", "exists"]:
            assert args == ["container", "exists", self.container_name or args[2]]
            assert NAME_RE.fullmatch(args[2])
            self.container_name = args[2]
            if self.undecodable_exists:
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return subprocess.CompletedProcess(arguments, 0 if self.present else 1, "", "")
        if args[:2] == ["container", "inspect"]:
            assert args == ["container", "inspect", self.container_name]
            if self.vanishes:
                self.present = False
                return subprocess.CompletedProcess(arguments, 125, "", "no such container")
            if self.inspect_output is not None:
                return subprocess.CompletedProcess(arguments, 0, self.inspect_output, "")
            info = {
                "Id": CONTAINER_ID,
                "Name": self.name if self.name is not None else self.container_name,
                "Config": {"Labels": {
                    "maintainer": "image label",
                    "aegis.observer": self.label if self.label is not None else self.token,
                }},
            }
            return subprocess.CompletedProcess(arguments, 0, json.dumps([info]), "")
        if args[:2] == ["rm", "--force"]:
            assert args[2] == CONTAINER_ID
            self.present = self.remains
            return subprocess.CompletedProcess(arguments, self.rm_returncode, "", "")
        raise AssertionError(arguments)

    def popen(self, arguments: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        self.commands.append(list(arguments))
        args = arguments[2:]
        assert arguments[:2] == ["podman", "--remote=false"] and args[0] == "run"
        assert args[args.index("--name") + 1] == self.container_name
        self.token = args[args.index("--label") + 1].removeprefix("aegis.observer=")
        self.popen_kwargs = kwargs
        # Rootless podman writes its own database: no file-size limit may reach it.
        assert "preexec_fn" not in kwargs
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE and kwargs["stderr"] is subprocess.PIPE
        assert not REMOTE_VARIABLES & set(kwargs["env"])
        if self.failure is not None:
            # A client lost after creating the container leaves it for exact cleanup.
            self.present = True
            raise self.failure
        targets = [value.split(":")[1] for flag, value in pairwise(args) if flag == "-v"]
        records = "".join(
            f"64{index} 631 0:5{index} /private/{index} {target} ro "
            f"- fakeowner /run/host_mark/private{index} rw\n"
            for index, target in enumerate(targets)
        )
        # --rm removes the container when the client sees it exit; a killed client does not.
        self.present = self.behavior in ("overflow", "sleep")
        self.client = REAL_POPEN(
            [sys.executable, "-I", "-S", "-c", CLIENT, self.behavior, records],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return self.client


@pytest.fixture
def no_compose(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        pytest.fail("the podman-run observer used Compose or the development mask guard")

    for name in (
        "require_podman_mask_compatibility", "require_empty_project", "compose_command",
        "compose_environment", "_cleanup_observer_project", "set_observer_output_limit",
    ):
        monkeypatch.setattr(f"aegisctl.mounts.{name}", forbidden)
    # Neither a Compose provider nor the private API socket exists: any engine selection fails.
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "podman")
    monkeypatch.delenv("PODMAN_COMPOSE_PROVIDER", raising=False)
    monkeypatch.delenv("AEGIS_PODMAN_SOCKET", raising=False)
    monkeypatch.setenv("CONTAINER_HOST", "ssh://remote.example.test/run/podman.sock")
    monkeypatch.setenv("DOCKER_HOST", "tcp://remote.example.test:2375")


def _install(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: PodmanRunEngine,
) -> None:
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", engine)
    monkeypatch.setattr("aegisctl.mounts.subprocess.Popen", engine.popen)


def _observe(slots: tuple[ValidatedSlot, ...], **overrides: Any) -> tuple[ValidatedSlot, ...]:
    options: dict[str, Any] = {
        "image": IMAGE, "host_gid": os.getegid(), "reader_type": READER_TYPE,
    }
    options.update(overrides)
    return observe_mount_fingerprints_podman_run(slots, **options)


def _retained_path(error: BaseException) -> Path:
    marker = "diagnostics retained at "
    message = str(error)
    assert marker in message
    return Path(message.split(marker, maxsplit=1)[1])


def _cleanup_commands(engine: PodmanRunEngine) -> list[list[str]]:
    return [command[2:] for command in engine.commands[2:]]


@pytest.mark.usefixtures("no_compose")
@pytest.mark.parametrize("slot_count", [1, 2])
def test_podman_run_observer_builds_the_exact_constrained_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, slot_count: int,
) -> None:
    slots = _slots(tmp_path, ("private-canary", "second-canary")[:slot_count])
    engine = PodmanRunEngine()
    _install(monkeypatch, tmp_path, engine)

    observed = _observe(slots)

    name = engine.container_name
    assert NAME_RE.fullmatch(name) and TOKEN_RE.fullmatch(engine.token)
    targets = [slot.container_path for slot in slots]
    expected_run = [
        "podman", "--remote=false", "run", "--rm", "--pull", "never",
        "--name", name, "--label", f"aegis.observer={engine.token}",
        "--network", "none", "--read-only", "--user", "101:101", "--cap-drop", "all",
        "--security-opt", "no-new-privileges",
        "--security-opt", "unmask=/sys/devices/virtual/powercap",
        "--security-opt", f"label=type:{READER_TYPE}",
        "--gidmap", f"+g101:@{os.getegid()}:1",
        "--cpus", "0.5", "--memory", "64m", "--pids-limit", "64",
        # Compose's stop_grace_period: 3s.
        "--stop-timeout", "3",
        "--entrypoint", "/bin/sh",
        *(item for slot in slots
          for item in ("-v", f"{slot.source}:{slot.container_path}:ro")),
        IMAGE, "-eu", "-c", _observer_script(targets),
    ]
    assert engine.commands == [
        ["podman", "--remote=false", "container", "exists", name],
        expected_run,
        ["podman", "--remote=false", "container", "exists", name],
    ]
    assert all(re.fullmatch(r"[0-9a-f]{64}", slot.mount_fingerprint) for slot in observed)
    assert len({slot.mount_fingerprint for slot in observed}) == slot_count
    assert [replace(slot, mount_fingerprint="") for slot in observed] == list(slots)
    assert engine.client is not None and engine.client.returncode == 0
    assert not list(tmp_path.glob("aegis-mount-preflight-*"))


def test_podman_run_and_compose_observers_run_the_same_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    scripts: list[str] = []
    compose_engine = ObserverEngine(slots[0].source)

    def compose_run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "run" in arguments:
            compose_path = Path(arguments[arguments.index("-f") + 1])
            service = yaml.safe_load(compose_path.read_text())["services"]["mount-observer"]
            assert service["entrypoint"] == ["/bin/sh", "-eu", "-c"]
            scripts.append(service["command"][0])
        return compose_engine(arguments, **kwargs)

    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(
        "aegisctl.mounts.require_podman_mask_compatibility", compose_engine.checked_policy,
    )
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", compose_run)
    observe_mount_fingerprints(slots)
    podman = PodmanRunEngine()
    _install(monkeypatch, tmp_path, podman)
    _observe(slots)

    run = podman.commands[1]
    assert run[-3:-1] == ["-eu", "-c"]
    assert scripts == [run[-1]] == [_observer_script(["/srv/aegis/roots/photos"])]


@pytest.mark.usefixtures("no_compose")
def test_podman_run_client_inherits_no_file_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(behavior="rlimit")
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    retained = _retained_path(caught.value)
    reported = (retained / "observer.stderr").read_text(encoding="ascii").splitlines()
    assert reported[0] == "client diagnostic"
    assert reported[1] == repr(resource.getrlimit(resource.RLIMIT_FSIZE))
    assert reported[1] != repr((MAX_MOUNTINFO_BYTES + 1, MAX_MOUNTINFO_BYTES + 1))


@pytest.mark.usefixtures("no_compose")
def test_podman_run_output_overflow_kills_the_client_and_removes_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(behavior="overflow")
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    assert engine.client is not None and engine.client.returncode == -9
    name = engine.container_name
    assert _cleanup_commands(engine) == [
        ["container", "exists", name],
        ["container", "inspect", name],
        ["rm", "--force", CONTAINER_ID],
        ["container", "exists", name],
    ]
    assert engine.present is False
    retained = _retained_path(caught.value)
    assert {path.name for path in retained.iterdir()} == DIAGNOSTICS
    assert (retained / "mountinfo.out").stat().st_size == MAX_MOUNTINFO_BYTES + 1
    assert str(slots[0].source) not in str(caught.value)


@pytest.mark.usefixtures("no_compose")
def test_podman_run_deadline_kills_the_client_and_removes_the_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(behavior="sleep")
    _install(monkeypatch, tmp_path, engine)
    monkeypatch.setattr("aegisctl.mounts.OBSERVER_TIMEOUT_SECONDS", 0.5)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
    assert engine.client is not None and engine.client.returncode == -9
    assert ["rm", "--force", CONTAINER_ID] in _cleanup_commands(engine)
    assert engine.present is False
    retained = _retained_path(caught.value)
    assert (retained / "observer.stderr").read_bytes() == b"client diagnostic\n"


@pytest.mark.usefixtures("no_compose")
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_podman_run_interruption_while_reading_kills_the_client_and_removes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException],
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(behavior="sleep")
    _install(monkeypatch, tmp_path, engine)
    original = interruption(17)

    class Interrupted(selectors.DefaultSelector):
        def select(self, timeout: float | None = None) -> NoReturn:
            del timeout
            raise original

    monkeypatch.setattr(
        "aegisctl.mounts.selectors",
        SimpleNamespace(DefaultSelector=Interrupted, EVENT_READ=selectors.EVENT_READ),
    )

    with pytest.raises(interruption) as caught:
        _observe(slots)

    assert caught.value is original
    assert engine.client is not None and engine.client.returncode == -9
    assert ["rm", "--force", CONTAINER_ID] in _cleanup_commands(engine)
    assert "diagnostics retained at " in " ".join(getattr(original, "__notes__", []))


@pytest.mark.usefixtures("no_compose")
def test_podman_run_keeps_only_bounded_client_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(behavior="stderr-flood")
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    retained = _retained_path(caught.value)
    assert (retained / "observer.stderr").stat().st_size == 64 * 1024
    # The client was drained to its exit, not killed, and its output was kept.
    assert engine.client is not None and engine.client.returncode == 125
    assert (retained / "mountinfo.out").read_bytes().endswith(b"rw\n")
    argv = json.loads((retained / "observer.argv.json").read_text(encoding="ascii"))
    assert argv == engine.commands[1]


@pytest.mark.usefixtures("no_compose")
@pytest.mark.parametrize("failure", [OSError("exec failed"), subprocess.SubprocessError("x")])
def test_podman_run_observer_failure_removes_exactly_the_labeled_run_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(failure=failure)
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    name = engine.container_name
    assert _cleanup_commands(engine) == [
        ["container", "exists", name],
        ["container", "inspect", name],
        ["rm", "--force", CONTAINER_ID],
        ["container", "exists", name],
    ]
    assert engine.present is False
    assert caught.value.__cause__ is failure
    retained = _retained_path(caught.value)
    assert {path.name for path in retained.iterdir()} == DIAGNOSTICS
    assert str(slots[0].source) not in str(caught.value)


@pytest.mark.usefixtures("no_compose")
@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_podman_run_observer_interruption_removes_the_run_container_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: type[BaseException],
) -> None:
    slots = _slots(tmp_path)
    original = interruption(17)
    engine = PodmanRunEngine(failure=original)
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(interruption) as caught:
        _observe(slots)

    assert caught.value is original
    assert original.__cause__ is None
    assert ["rm", "--force", CONTAINER_ID] in _cleanup_commands(engine)
    assert engine.present is False
    notes = " ".join(getattr(original, "__notes__", []))
    assert "diagnostics retained at " in notes


@pytest.mark.usefixtures("no_compose")
@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"label": "f" * 32}, "identity"),
        ({"name": "aegis-preflight-0000000000000000"}, "identity"),
        ({"inspect_output": "not json"}, "inspected"),
        ({"inspect_output": json.dumps([{"Id": "D" * 64}])}, "identity"),
        ({"inspect_output": "[{}, {}]"}, "identity"),
        ({"inspect_output": "{}"}, "identity"),
    ],
    ids=["foreign-label", "foreign-name", "not-json", "bad-id", "two-entries", "not-a-list"],
)
def test_podman_run_observer_never_removes_a_container_it_cannot_prove_it_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, options: dict[str, Any], reason: str,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(failure=subprocess.SubprocessError("client lost"), **options)
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    assert not any(command[2] == "rm" for command in engine.commands)
    assert engine.present is True
    assert isinstance(caught.value.__cause__, ConfigError)
    assert reason in str(caught.value.__cause__)


@pytest.mark.usefixtures("no_compose")
def test_podman_run_observer_accepts_a_container_that_removed_itself_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    failure = subprocess.SubprocessError("client lost")
    engine = PodmanRunEngine(failure=failure, vanishes=True)
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    assert not any(command[2] == "rm" for command in engine.commands)
    assert caught.value.__cause__ is failure


@pytest.mark.usefixtures("no_compose")
def test_podman_run_observer_accepts_a_failed_removal_when_the_container_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    failure = subprocess.SubprocessError("client lost")
    engine = PodmanRunEngine(failure=failure, rm_returncode=1)
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    assert _cleanup_commands(engine)[-2:] == [
        ["rm", "--force", CONTAINER_ID], ["container", "exists", engine.container_name],
    ]
    assert caught.value.__cause__ is failure


@pytest.mark.usefixtures("no_compose")
def test_podman_run_observer_reports_incomplete_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(failure=subprocess.SubprocessError("client lost"), remains=True)
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    assert "incomplete" in str(caught.value.__cause__)


@pytest.mark.usefixtures("no_compose")
def test_podman_run_observer_refuses_an_existing_container_name_without_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(preexisting=True)
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained"):
        _observe(slots)

    assert [command[2:4] for command in engine.commands] == [["container", "exists"]]


@pytest.mark.usefixtures("no_compose")
def test_podman_run_observer_retains_an_undecodable_engine_reply_as_a_safe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(undecodable_exists=True)
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    assert "cannot be checked" in str(caught.value.__cause__)
    assert [command[2:4] for command in engine.commands] == [["container", "exists"]]


@pytest.mark.usefixtures("no_compose")
def test_podman_run_observer_failed_run_retains_diagnostics_after_cleanup_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine(behavior="exit125")
    _install(monkeypatch, tmp_path, engine)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        _observe(slots)

    assert engine.commands[-1][2:4] == ["container", "exists"]
    retained = _retained_path(caught.value)
    assert {path.name for path in retained.iterdir()} == DIAGNOSTICS
    assert (retained / "observer.stderr").read_bytes() == b"client diagnostic\n"


@pytest.mark.parametrize(
    "overrides",
    [
        {"image": "aegis-gateway"},
        {"image": "registry.example.test/aegis/aegis-gateway:latest"},
        {"image": "aegis-gateway@sha256:" + "b" * 63},
        {"image": "Aegis-Gateway@sha256:" + "b" * 64},
        {"image": LOCAL_IMAGE_ID},
        {"reader_type": "spc_t"},
        {"reader_type": "unconfined_t"},
        {"reader_type": "aegis_reader"},
        {"reader_type": "aegis_Reader.process"},
        {"reader_type": "container_t "},
        {"reader_type": "aegis_reader.process,label=disable"},
        {"host_gid": 0},
        {"host_gid": -1},
    ],
)
def test_podman_run_observer_validates_inputs_before_any_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any],
) -> None:
    slots = _slots(tmp_path)
    for name in ("run", "Popen"):
        monkeypatch.setattr(
            f"aegisctl.mounts.subprocess.{name}", lambda *a, **k: pytest.fail("a command ran"),
        )

    with pytest.raises(ConfigError):
        _observe(slots, **overrides)


def test_podman_run_observer_requires_the_invoking_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    for name in ("run", "Popen"):
        monkeypatch.setattr(
            f"aegisctl.mounts.subprocess.{name}", lambda *a, **k: pytest.fail("a command ran"),
        )

    with pytest.raises(ConfigError, match="invoking group"):
        _observe(slots, host_gid=os.getegid() + 1)


@pytest.mark.usefixtures("no_compose")
@pytest.mark.parametrize("reader_type", ["container_t", "aegis_reader.process"])
def test_podman_run_observer_admits_a_test_image_id_only_behind_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader_type: str,
) -> None:
    slots = _slots(tmp_path)
    engine = PodmanRunEngine()
    _install(monkeypatch, tmp_path, engine)

    _observe(slots, image=LOCAL_IMAGE_ID, allow_test_image_id=True, reader_type=reader_type)

    run = engine.commands[1]
    assert run[run.index("-eu") - 1] == LOCAL_IMAGE_ID
    assert f"label=type:{reader_type}" in run


@pytest.mark.parametrize("engine", ["docker", "podman"])
@pytest.mark.parametrize("name", ["source,with-comma", "source:with-colon", "source with space"])
def test_podman_run_observer_refuses_unit_unsafe_sources_on_any_selected_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: str, name: str,
) -> None:
    slots = _slots(tmp_path, (name,))
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", engine)
    temp = tmp_path / "observer-temp"
    temp.mkdir(mode=0o700)
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(temp))
    for command in ("run", "Popen"):
        monkeypatch.setattr(
            f"aegisctl.mounts.subprocess.{command}", lambda *a, **k: pytest.fail("a command ran"),
        )

    with pytest.raises(ConfigError) as caught:
        _observe(slots)

    if "," in name:
        assert str(caught.value) == PODMAN_COMMA_REFUSAL
    else:
        assert "Quadlet original root path" in str(caught.value)
    assert not list(temp.iterdir())


def test_podman_run_observer_refuses_a_temporary_directory_below_an_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = _slots(tmp_path)
    monkeypatch.setattr("tempfile.tempdir", str(slots[0].source))
    for name in ("run", "Popen"):
        monkeypatch.setattr(
            f"aegisctl.mounts.subprocess.{name}", lambda *a, **k: pytest.fail("a command ran"),
        )

    with pytest.raises(ConfigError, match="original"):
        _observe(slots)

    assert not list(slots[0].source.iterdir())


def _config(tmp_path: Path) -> Path:
    source = tmp_path / "private-name"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    config.write_text(
        "version = 1\n[[slots]]\n"
        f'slot_id = "photos"\nsource = {json.dumps(str(source))}\n'
        'container_path = "/srv/aegis/roots/photos"\nmode = "read_only"\n'
        f'expected_identity = "{local_identity(source)}"\n',
        encoding="utf-8",
    )
    return config


def test_cli_preflight_podman_run_observer_writes_the_manifest_without_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    manifest = tmp_path / "manifest.json"
    calls: list[dict[str, Any]] = []

    def observed(
        slots: tuple[ValidatedSlot, ...], **kwargs: Any,
    ) -> tuple[ValidatedSlot, ...]:
        calls.append(kwargs)
        return tuple(replace(slot, mount_fingerprint="a" * 64) for slot in slots)

    monkeypatch.setattr("aegisctl.cli.observe_mount_fingerprints_podman_run", observed)
    monkeypatch.setattr(
        "aegisctl.cli.observe_mount_fingerprints", lambda *a: pytest.fail("compose observer"),
    )
    # The Podman deployment does not use the compose runtime identity.
    monkeypatch.setenv("AEGIS_UID", str(os.geteuid() + 1))
    monkeypatch.setenv("AEGIS_GID", str(os.getegid() + 1))

    assert main([
        "mounts", "preflight", "--config", str(config), "--manifest", str(manifest),
        "--observer", "podman-run", "--observer-image", IMAGE,
        "--host-gid", str(os.getegid()), "--reader-type", READER_TYPE,
    ]) == 0

    assert calls == [{
        "image": IMAGE, "host_gid": os.getegid(), "reader_type": READER_TYPE,
        "allow_test_image_id": False,
    }]
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "preflighted" and len(result["manifestSha256"]) == 64
    assert manifest.stat().st_mode & 0o777 == 0o600


def test_cli_preflight_podman_run_refuses_a_group_mismatch_before_observing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(
        "aegisctl.cli.observe_mount_fingerprints_podman_run",
        lambda *a, **k: pytest.fail("observer ran"),
    )

    assert main([
        "mounts", "preflight", "--config", str(config),
        "--manifest", str(tmp_path / "manifest.json"),
        "--observer", "podman-run", "--observer-image", IMAGE,
        "--host-gid", str(os.getegid() + 1), "--reader-type", READER_TYPE,
    ]) == 2

    assert "invoking group" in json.loads(capsys.readouterr().err)["message"]
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--observer", "podman-run", "--host-gid", "1000", "--reader-type", READER_TYPE],
        ["--observer", "podman-run", "--observer-image", IMAGE, "--reader-type", READER_TYPE],
        ["--observer", "podman-run", "--observer-image", IMAGE, "--host-gid", "1000"],
        ["--observer-image", IMAGE],
        ["--host-gid", "1000"],
        ["--host-gid", "0"],
        ["--reader-type", READER_TYPE],
        ["--allow-test-image-id"],
        ["--observer", "other"],
    ],
)
def test_cli_preflight_rejects_options_of_the_other_observer(
    arguments: list[str], capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["mounts", "preflight", "--config", "c", "--manifest", "m", *arguments])

    assert caught.value.code == 2
    assert "aegisctl mounts preflight: error:" in capsys.readouterr().err
