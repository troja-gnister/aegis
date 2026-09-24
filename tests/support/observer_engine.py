"""Reviewed observer subprocess double shared by mount-policy regressions."""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from aegisctl.mounts import MAX_MOUNTINFO_BYTES


def write_observer_record(output: Any) -> None:
    output.write(
        b"643 631 0:50 /private/source /srv/aegis/roots/photos ro "
        b"- fakeowner /run/host_mark/private rw\n"
    )
    output.flush()


class ObserverEngine:
    """In-memory engine boundary with full inspection and exact-ID removal."""

    def __init__(
        self, source: Path, *, run_timeout: bool = False,
        cleanup_failure: str = "", oversized: bool = False,
        output: Callable[[list[str], dict[str, Any]], None] | None = None,
    ) -> None:
        self.source = source
        self.run_timeout = run_timeout
        self.cleanup_failure = cleanup_failure
        self.oversized = oversized
        self.output = output
        self.project = ""
        self.resources: dict[str, str] = {}
        self.removals: list[tuple[str, ...]] = []
        self.queries: list[tuple[str, str]] = []
        self.diagnostics: Path | None = None

    def __call__(self, arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert arguments[0] in {"docker", "podman"}
        prefix = 2 if arguments[0] == "podman" else 1
        if prefix == 2:
            assert arguments[1] == "--remote=false"
        args = arguments[prefix:]
        if "run" in args:
            assert args[0] == "compose"
            assert args[args.index("--project-name") + 1] == self.project
            compose_path = Path(args[args.index("-f") + 1])
            self.diagnostics = compose_path.parent
            assert compose_path.stat().st_mode & 0o777 == 0o600
            service = yaml.safe_load(compose_path.read_text())["services"]["mount-observer"]
            assert service["volumes"][0]["source"] == str(self.source)
            assert service["volumes"][0]["type"] == "bind"
            assert service["volumes"][0]["read_only"] is True
            assert service["cpus"] == 0.5
            assert service["mem_limit"] == "64m"
            assert service["pids_limit"] == 64
            assert service["stop_grace_period"] == "3s"
            assert kwargs.get("capture_output") is not True
            assert kwargs["stderr"] is subprocess.DEVNULL
            self.resources = {"container": "observer-immutable", "network": "network-immutable"}
            if self.output is not None:
                self.output(arguments, kwargs)
            elif self.oversized:
                kwargs["stdout"].write(b"x" * (MAX_MOUNTINFO_BYTES + 1))
                kwargs["stdout"].flush()
            else:
                write_observer_record(kwargs["stdout"])
            if self.run_timeout:
                raise subprocess.TimeoutExpired(arguments, 30)
            return subprocess.CompletedProcess(arguments, 0, "", "")
        kind = args[0]
        if len(args) > 1 and args[1] == "ls":
            assert kind in {"container", "network", "volume"}
            label = args[args.index("--filter") + 1]
            marker = "label=com.docker.compose.project="
            assert label.startswith(marker)
            if not self.project:
                self.project = label.removeprefix(marker)
            assert label == marker + self.project
            output = f"{kind}-handle\n" if kind in self.resources else ""
            self.queries.append((kind, output))
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if len(args) > 1 and args[1] == "inspect":
            assert args == [kind, "inspect", f"{kind}-handle"]
            identity = self.resources[kind]
            fields: dict[str, Any] = {
                "Id": identity, "Name": f"{self.project}_{kind}", "Created": "now",
            }
            labels = {"com.docker.compose.project": self.project}
            if kind == "container":
                labels.update({"com.docker.compose.service": "mount-observer",
                               "com.docker.compose.oneoff": "True"})
                fields.update({"Image": "observer-image", "Config": {"Labels": labels}})
            else:
                assert kind == "network"
                labels["com.docker.compose.network"] = "default"
                fields.update({"Driver": "bridge", "Labels": labels, "Internal": False})
                if prefix == 2:
                    fields = {key.lower(): value for key, value in fields.items()}
            return subprocess.CompletedProcess(arguments, 0, json.dumps([fields]), "")
        assert tuple(args) in {
            ("rm", "--force", "observer-immutable"), ("network", "rm", "network-immutable"),
        }
        self.removals.append(tuple(args))
        if self.cleanup_failure == "exception":
            raise subprocess.SubprocessError(str(self.source))
        if self.cleanup_failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, 30)
        if self.cleanup_failure == "nonzero":
            return subprocess.CompletedProcess(arguments, 125, "", str(self.source))
        kind = "container" if args[0] == "rm" else "network"
        if self.cleanup_failure != "residual":
            del self.resources[kind]
        return subprocess.CompletedProcess(arguments, 0, "", "")


def assert_observer_engine_cleaned(engine: ObserverEngine) -> None:
    assert engine.removals == [
        ("rm", "--force", "observer-immutable"), ("network", "rm", "network-immutable"),
    ]
    assert engine.resources == {}
    assert engine.queries[-3:] == [("container", ""), ("network", ""), ("volume", "")]

