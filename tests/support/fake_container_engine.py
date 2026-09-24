"""Explicit engine selection for resource-free subprocess doubles."""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest


def select_fake_engine(
    engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    *, checked_mask_policy: bool = False,
) -> list[str]:
    if checked_mask_policy:
        def checked_policy(*args: object, **kwargs: object) -> tuple[str, ...]:
            del args, kwargs
            return ("unmask=/sys/devices/virtual/powercap",) if engine == "podman" else ()

        monkeypatch.setattr(
            "aegisctl.container_launch.require_podman_mask_compatibility", checked_policy,
        )
        monkeypatch.setattr("aegisctl.mounts.require_podman_mask_compatibility", checked_policy)
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", engine)
    if engine == "docker":
        return ["docker"]
    assert engine == "podman"
    provider = tmp_path / "fake-compose-provider"
    provider.write_text("synthetic provider", encoding="ascii")
    provider.chmod(0o700)
    private = tmp_path / "fake-podman-service"
    private.mkdir(mode=0o700)
    socket = private / "podman.sock"
    socket.touch(mode=0o600)
    original_lstat = Path.lstat

    def socket_metadata(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if path == Path("/tmp"):
            fields = list(metadata)
            fields[stat.ST_UID] = 0
            return os.stat_result(fields)
        if path == socket:
            fields = list(metadata)
            fields[stat.ST_MODE] = stat.S_IFSOCK | stat.S_IMODE(metadata.st_mode)
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(Path, "lstat", socket_metadata)
    monkeypatch.setenv("PODMAN_COMPOSE_PROVIDER", str(provider))
    monkeypatch.setenv("AEGIS_PODMAN_SOCKET", str(socket))
    return ["podman", "--remote=false"]


class ProjectEngine:
    """Model complete Compose inventories and exact removal at the CLI boundary."""

    def __init__(
        self, engine: str, project: str, resources: tuple[tuple[str, str], ...],
    ) -> None:
        self.prefix = ["podman", "--remote=false"] if engine == "podman" else ["docker"]
        self.engine = engine
        self.project = project
        self.declared = resources
        self.resources: dict[tuple[str, str], dict[str, object]] = {}
        self.removals: list[tuple[str, ...]] = []
        self.queries: list[str] = []

    def create(self) -> None:
        for kind, name in self.declared:
            labels = {
                "com.docker.compose.project": self.project,
                f"com.docker.compose.{kind}": name,
            }
            fields: dict[str, object]
            if kind == "network":
                fields = {
                    "Id": f"immutable-{name}", "Name": f"{self.project}_{name}",
                    "Created": "2026-09-23T00:00:00Z", "Driver": "bridge",
                    "Labels": labels, "Internal": False,
                }
                if self.engine == "podman":
                    fields = {key.lower(): value for key, value in fields.items()}
            else:
                assert kind == "volume"
                fields = {
                    "Name": f"{self.project}_{name}", "CreatedAt": "2026-09-23T00:00:00Z",
                    "Driver": "local", "Mountpoint": f"/synthetic/{name}",
                    "Options": {}, "Scope": "local", "Labels": labels,
                }
            self.resources[(kind, name)] = fields

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert command[:len(self.prefix)] == self.prefix
        args = command[len(self.prefix):]
        if args == ["compose", "run"]:
            self.create()
            return subprocess.CompletedProcess(command, 64, "", "invalid host")
        kind, action = args[:2]
        assert kind in {"container", "network", "volume"}
        if action == "ls":
            expected = [kind, "ls", *(["--all"] if kind == "container" else []),
                        "--quiet", "--filter", f"label=com.docker.compose.project={self.project}"]
            assert args == expected
            self.queries.append(kind)
            output = "\n".join(
                f"{self.project}_{name}" for resource_kind, name in self.resources
                if resource_kind == kind
            )
            return subprocess.CompletedProcess(command, 0, output, "")
        if action == "inspect":
            assert len(args) == 3
            name = args[2].removeprefix(self.project + "_")
            return subprocess.CompletedProcess(
                command, 0, json.dumps([self.resources[(kind, name)]]), "",
            )
        assert action == "rm" and len(args) == 3
        name = args[2].removeprefix("immutable-" if kind == "network" else self.project + "_")
        assert (kind, name) in self.resources
        self.removals.append(tuple(args))
        del self.resources[(kind, name)]
        return subprocess.CompletedProcess(command, 0, "", "")
