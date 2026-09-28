"""Resource-free regressions for the Task 7 indexer runtime fixtures.

The real fixture and test bodies of ``test_indexer_runtime`` run against an in-memory
engine at the CLI boundary, under both engine selections. Only the runtime case's ORM
setup is stubbed. No engine, image, container, database or host relabel is used.
"""
from __future__ import annotations

import contextlib
import copy
import inspect
import json
import os
import re
import stat
import subprocess
import uuid
from collections.abc import Callable, Generator, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aegisctl.container_engine import ContainerEngineError
from aegisctl.mounts import MountAttestationError, parse_mountinfo

import tests.deployment.test_indexer_runtime as runtime
from tests.support.fake_container_engine import select_fake_engine

OWNER = "aegis.indexer.owner"
TARGET = "/srv/aegis/indexer-coordination"
POWERCAP = "/sys/devices/virtual/powercap"
MASK = f"unmask={POWERCAP}"
ZERO = "0000000000000000"
FLAGS = frozenset({"--rm", "--init", "--read-only", "--interactive"})
# Tmpfs options each engine refuses: Podman 5.8.7 measured uid/gid; Docker has no U.
REFUSED_TMPFS = {"podman": frozenset({"uid", "gid"}),
                 "docker": frozenset({"U", "notmpcopyup", "tmpcopyup"})}
# The engine model's own copy of rootless Podman's measured `--cap-drop ALL` inspect form.
MODELED_PODMAN_CAP_DROP = [
    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_KILL",
    "CAP_NET_BIND_SERVICE", "CAP_SETFCAP", "CAP_SETGID", "CAP_SETPCAP", "CAP_SETUID",
    "CAP_SYS_CHROOT",
]
DYNAMIC = (os.geteuid(), os.getegid())
DYNAMIC_USER = f"{DYNAMIC[0]}:{DYNAMIC[1]}"
PODMAN_POSITIVE = f"{TARGET}:rw,nosuid,nodev,size=1m,U,notmpcopyup,mode=0700"
# Exact accepted Docker strings, spelled as in the pre-correction fixtures.
EXPECTED_COORDINATION: dict[str, dict[str, list[str]]] = {
    "docker": {
        "transport": [f"{TARGET}:rw,nosuid,nodev,size=1m,"
                      f"uid={DYNAMIC[0]},gid={DYNAMIC[1]},mode=0700"],
        "runtime": [f"{TARGET}:rw,nosuid,nodev,size=1m,uid=501,gid=20,mode=0700"],
        "restart": [f"{TARGET}:rw,nosuid,nodev,size=1m,uid=501,gid=20,mode=0700"],
        "misowned": [f"{TARGET}:rw,nosuid,nodev,uid=0,gid=0,mode=0777"],
        "absent": [],
    },
    "podman": {
        "transport": [PODMAN_POSITIVE],
        "runtime": [PODMAN_POSITIVE],
        "restart": [PODMAN_POSITIVE],
        "misowned": [f"{TARGET}:rw,nosuid,nodev,notmpcopyup,mode=0777"],
        "absent": [],
    },
}
_BASE_ISOLATION: list[tuple[str, str | None]] = [
    ("--init", None), ("--network", "none"), ("--read-only", None), ("--cap-drop", "ALL"),
    ("--security-opt", "no-new-privileges:true"), ("--entrypoint", "python"),
]
_SOURCE = "type=bind,src=<source>,dst=/srv/aegis/roots/synthetic,readonly"
# Synthetic loopback port of the stubbed disposable database (never 5432, so a launch can
# only match by taking the port from role_database).
DATABASE_PORT = 47813
# The runtime case's network: Docker keeps host networking. Rootless Podman host networking
# rbinds the host /sys (duplicate /sys/fs/cgroup and /sys/fs/selinux), which the probe's
# unchanged strict parser refuses (root O12); pasta -T forwards only the database port (O13).
RUNTIME_NETWORK = {"docker": "host", "podman": f"pasta:-T,{DATABASE_PORT}"}
# Every process-user, security, network and limit option of the pre-correction launches,
# except the runtime network, which is engine-specific (RUNTIME_NETWORK).
EXPECTED_ISOLATION: dict[str, list[tuple[str, str | None]]] = {
    "transport": [*_BASE_ISOLATION, ("--user", DYNAMIC_USER), ("--memory", "128m"),
                  ("--pids-limit", "32"), ("--mount", _SOURCE)],
    "runtime": [
        ("--interactive", None), ("--init", None), ("--read-only", None),
        ("--cap-drop", "ALL"), ("--security-opt", "no-new-privileges:true"),
        ("--user", "501:20"), ("--memory", "256m"), ("--pids-limit", "64"),
        ("--tmpfs", "/tmp:rw,nosuid,nodev,size=16m,mode=1777"),
        ("--mount", "type=bind,src=<source>,dst=/srv/aegis/roots/slot-large,readonly"),
        ("--mount", "type=bind,src=<source>,dst=/srv/aegis/roots/slot-small,readonly"),
        ("--entrypoint", "python"),
    ],
    "restart": [*_BASE_ISOLATION, ("--user", "501:20"), ("--mount", _SOURCE)],
    "misowned": [*_BASE_ISOLATION, ("--user", "501:20")],
    "absent": [*_BASE_ISOLATION, ("--user", "501:20")],
}
CONTAINER_FLOWS = ("transport", "runtime", "restart", "misowned")


@dataclass
class FakeContainer:
    identity: str
    name: str
    labels: dict[str, str]
    image: str
    options: list[tuple[str, str | None]]
    command: list[str]
    status: str = "created"

    def values(self, option: str) -> list[str]:
        return [value for key, value in self.options if key == option and value is not None]

    def has(self, option: str) -> bool:
        return any(key == option for key, _ in self.options)


def parse_launch(arguments: Sequence[str]) -> tuple[list[tuple[str, str | None]], str, list[str]]:
    """Split a native run/create argv into options, image reference and payload."""
    options: list[tuple[str, str | None]] = []
    index = 0
    while arguments[index].startswith("-"):
        if arguments[index] in FLAGS:
            options.append((arguments[index], None))
            index += 1
        else:
            options.append((arguments[index], arguments[index + 1]))
            index += 2
    return options, arguments[index], list(arguments[index + 1:])


def _values(arguments: Sequence[str]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for index in range(0, len(arguments) - 1, 2):
        result.setdefault(arguments[index], []).append(arguments[index + 1])
    return result


class FakeEngine:
    """In-memory engine CLI: images, containers, labels, exact removal and scenarios.

    ``scenario`` injects an adversarial or failure event at creation or right after the
    payload (before cleanup); ``build_scenario`` does the same for the image build.
    """

    def __init__(self, engine: str, prefix: list[str]) -> None:
        self.engine = engine
        self.prefix = prefix
        self.images: dict[str, dict[str, Any]] = {}
        self.tags: dict[str, str] = {}
        self.containers: dict[str, FakeContainer] = {}
        self.launched: list[FakeContainer] = []
        self.built: list[str] = []
        self.calls: list[list[str]] = []
        self.launches: list[list[str]] = []
        self.removals: list[list[str]] = []
        self.scenario = "success"
        self.payload_fails = False
        self.build_scenario = "success"
        self.build_volumes: dict[str, object] | None = None
        self.network_drift: str | None = None
        self.probe_errors: list[str] = []

    # -- subprocess boundaries -------------------------------------------------------
    def run(self, command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        argv = list(command)
        if argv[0] == "chcon":  # prepare_owned_test_inventory's SELinux relabel
            return self._complete(argv, (0, "", ""), kwargs)
        assert argv[:len(self.prefix)] == self.prefix, argv
        arguments = argv[len(self.prefix):]
        self.calls.append(arguments)
        return self._complete(argv, self._dispatch(arguments), kwargs)

    def popen(self, command: Sequence[str], **kwargs: Any) -> FakeAttachedProcess:
        del kwargs
        return FakeAttachedProcess(self, list(command))

    @staticmethod
    def _complete(
        argv: list[str], result: tuple[int, str, str], kwargs: dict[str, Any],
    ) -> subprocess.CompletedProcess[Any]:
        code, out, err = result
        output: Any = out if kwargs.get("text") else out.encode()
        error: Any = err if kwargs.get("text") else err.encode()
        if kwargs.get("check") and code:
            raise subprocess.CalledProcessError(code, argv, output, error)
        return subprocess.CompletedProcess(argv, code, output, error)

    def _dispatch(self, arguments: list[str]) -> tuple[int, str, str]:
        action = arguments[0]
        if action == "build":
            return self._build(arguments[1:])
        if action == "image":
            return self._image(arguments[1], arguments[2:])
        if action in ("run", "create"):
            return self._launch(action, arguments[1:])
        if action == "start":
            assert arguments[1] == "--attach" and len(arguments) == 3, arguments
            container = self.containers[arguments[2]]
            assert container.status == "created"
            return self._execute(container)
        if action == "inspect" or arguments[:2] == ["container", "inspect"]:
            container = self.resolve_container(arguments[-1])
            if container is None:
                return 125, "", f"Error: no such container {arguments[-1]}"
            return 0, json.dumps([self._container_info(container)]), ""
        if arguments[:2] == ["container", "ls"]:
            assert {"--all", "--no-trunc", "--quiet"} <= set(arguments), arguments
            filters = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--filter"]
            listed = [identity for identity, container in self.containers.items()
                      if all(self._labelled(container.labels, value) for value in filters)]
            return 0, "".join(f"{identity}\n" for identity in listed), ""
        assert arguments[:2] == ["rm", "--force"] and len(arguments) == 3, arguments
        self.removals.append(arguments)
        removed = self.resolve_container(arguments[2])
        if removed is None:
            return 1, "", "no such container"
        del self.containers[removed.identity]
        return 0, "", ""

    # -- images --------------------------------------------------------------------------
    def add_image(
        self, labels: dict[str, str], *, tag: str | None = None,
        volumes: dict[str, object] | None = None,
    ) -> str:
        identity = uuid.uuid4().hex + uuid.uuid4().hex
        self.images[identity] = {"labels": dict(labels), "volumes": volumes}
        if tag is not None:
            self.tags[tag] = identity
        return identity

    def resolve_image(self, reference: str) -> str | None:
        if reference in self.tags:
            return self.tags[reference]
        identity = reference.removeprefix("sha256:")
        return identity if identity in self.images else None

    def tag_of(self, identity: str) -> str:
        (tag,) = [tag for tag, target in self.tags.items() if target == identity]
        return tag

    def _build(self, arguments: list[str]) -> tuple[int, str, str]:
        assert arguments[-1] == "."
        values = _values(arguments[:-1])
        assert values["--file"] == ["docker/backend.Dockerfile"]
        labels = dict(label.split("=", 1) for label in values["--label"])
        (tag,) = values["--tag"]
        identity = self.add_image(labels, tag=tag, volumes=self.build_volumes)
        self.built.append(identity)
        for iidfile in values.get("--iidfile", []):
            Path(iidfile).write_text(f"sha256:{identity}\n", encoding="ascii")
        if self.build_scenario == "interrupted":
            raise KeyboardInterrupt
        if self.build_scenario == "unrecorded":
            for iidfile in values.get("--iidfile", []):
                Path(iidfile).unlink()
            return 1, "", "synthetic build failure after tagging"
        return 0, "", ""

    def _image_info(self, identity: str) -> dict[str, Any]:
        podman = self.engine == "podman"
        config: dict[str, Any] = {"Labels": dict(self.images[identity]["labels"])}
        if self.images[identity]["volumes"]:
            config["Volumes"] = self.images[identity]["volumes"]
        tags = sorted(tag for tag, target in self.tags.items() if target == identity)
        return {
            "Id": identity if podman else f"sha256:{identity}",
            "RepoTags": [f"localhost/{tag}:latest" if podman else f"{tag}:latest" for tag in tags],
            "Config": config,
        }

    def _image(self, action: str, arguments: list[str]) -> tuple[int, str, str]:
        if action == "inspect":
            (reference,) = arguments
            identity = self.resolve_image(reference)
            if identity is None:
                return 125, "", f"Error: {reference}: image not known"
            return 0, json.dumps([self._image_info(identity)]), ""
        if action == "ls":
            assert {"--no-trunc", "--quiet"} <= set(arguments), arguments
            filters = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--filter"]
            listed = [identity for identity in self.images
                      if all(self._image_filter(identity, value) for value in filters)]
            return 0, "".join(f"sha256:{identity}\n" for identity in listed), ""
        assert action == "rm", action
        self.removals.append(["image", "rm", *arguments])
        (reference,) = arguments
        identity = self.resolve_image(reference)
        if identity is None:
            return 1, "", "image not known"
        if any(container.image == identity for container in self.containers.values()):
            return 2, "", "image is in use by a container"
        if reference in self.tags:
            del self.tags[reference]
            if identity in self.tags.values():
                return 0, "", ""
        else:
            tags = [tag for tag, target in self.tags.items() if target == identity]
            if len(tags) > 1:
                return 2, "", "image is referenced by multiple tags"
            for tag in tags:
                del self.tags[tag]
        del self.images[identity]
        return 0, "", ""

    def _image_filter(self, identity: str, value: str) -> bool:
        key, _, expected = value.partition("=")
        if key == "reference":
            return self.tags.get(expected) == identity
        assert key == "label", value
        return self._labelled(self.images[identity]["labels"], value)

    @staticmethod
    def _labelled(labels: dict[str, str], value: str) -> bool:
        assert value.startswith("label="), value
        key, _, expected = value.removeprefix("label=").partition("=")
        return labels.get(key) == expected

    # -- containers ------------------------------------------------------------------------
    def resolve_container(self, reference: str) -> FakeContainer | None:
        if reference in self.containers:
            return self.containers[reference]
        named = [container for container in self.containers.values() if container.name == reference]
        return named[0] if named else None

    def add_container(self, source: FakeContainer, name: str) -> FakeContainer:
        container = FakeContainer(
            uuid.uuid4().hex + uuid.uuid4().hex, name, dict(source.labels), source.image,
            list(source.options), list(source.command),
        )
        self.containers[container.identity] = container
        return container

    def _launch(
        self, action: str, arguments: list[str], *, execute: bool = True,
    ) -> tuple[int, str, str]:
        self.launches.append([action, *arguments])
        options, reference, command = parse_launch(arguments)
        if self.network_drift is not None:  # engine-state drift; the recorded argv is intact
            options = [(key, self.network_drift if key == "--network" else value)
                       for key, value in options]
        masks = [value for key, value in options if key == "--security-opt" and value == MASK]
        assert len(masks) == (1 if self.engine == "podman" else 0), "mask policy placement"
        for key, value in options:
            if key == "--tmpfs":
                assert value is not None
                for option in value.partition(":")[2].split(","):
                    if option.partition("=")[0] in REFUSED_TMPFS[self.engine]:
                        return 125, "", f'Error: unknown mount option "{option}": invalid'
        image = self.resolve_image(reference)
        if image is None:
            return 125, "", f"Error: {reference}: image not known"
        if self.scenario == "interrupt-before-create":
            raise KeyboardInterrupt
        if self.scenario == "no-create":
            return 125, "", "synthetic create failure"
        names = [value for key, value in options if key == "--name" and value is not None]
        name = names[0] if names else f"generated-{uuid.uuid4().hex[:12]}"
        assert self.resolve_container(name) is None, "name already in use"
        labels = {
            **self.images[image]["labels"],
            **dict(value.split("=", 1) for key, value in options if key == "--label" and value),
        }
        container = FakeContainer(
            uuid.uuid4().hex + uuid.uuid4().hex, name, labels, image, options, command,
        )
        self.containers[container.identity] = container
        self.launched.append(container)
        for key, value in options:
            if key == "--cidfile" and value is not None:
                Path(value).write_text(container.identity + "\n", encoding="ascii")
        if self.scenario == "ambiguous-create":
            self.add_container(container, f"{name}-twin")
            return 125, "", "synthetic ambiguous create"
        if self.scenario == "interrupt-after-create":
            raise KeyboardInterrupt
        if action == "create" or not execute:
            return 0, container.identity + "\n", ""
        return self._execute(container)

    def _execute(self, container: FakeContainer) -> tuple[int, str, str]:
        container.status = "exited"
        if self.scenario == "payload-timeout":
            raise subprocess.TimeoutExpired(["synthetic-payload"], 1)
        result = (1, "", "synthetic payload failure") if self.payload_fails else self._payload(
            container,
        )
        if self.scenario == "replaced":
            del self.containers[container.identity]
            self.add_container(container, container.name)
        elif self.scenario == "extra-owned":
            self.add_container(container, f"{container.name}-extra")
        elif self.scenario == "owner-drift":
            container.labels[OWNER] = "aegis-indexer-someone-else"
        elif self.scenario == "resource-drift":
            container.labels["aegis.indexer.resource"] = "aegis-indexer-someone-else"
        if container.has("--rm") and container.identity in self.containers:
            del self.containers[container.identity]  # the engine's own auto-removal
        return result

    def _payload(self, container: FakeContainer) -> tuple[int, str, str]:
        assert container.command[:1] == ["-c"], container.command
        program = container.command[1]
        if program == runtime.TRANSPORT_PROBE:
            evidence: dict[str, Any] = {"observed": 1101, "complete": True, "reaped": True}
        elif program == runtime.RESTART_PROBE:
            evidence = {"parent_death": "SIGKILL", "child_reaped": True, "replacement": True,
                        "reaper": container.command[2]}
        elif program == runtime.RUNTIME_PROBE:
            evidence = {"reaped": 2, "counts": [1501, 3], "live_at_65": True}
        elif program == getattr(runtime, "COORDINATION_PROBE", None):
            return self._coordination_payload(container)
        else:  # the pre-correction inline negative probe
            assert "unowned or absent mount accepted" in program, program
            return 0, "rejected\n", ""
        return 0, json.dumps(evidence) + "\n", ""

    def _coordination_payload(self, container: FakeContainer) -> tuple[int, str, str]:
        """Model the metadata probe from the launch options, as each engine applies them."""
        podman = self.engine == "podman"
        uid, gid = (int(part) for part in container.values("--user")[0].split(":"))
        declared = [value for value in container.values("--tmpfs") if value.startswith(TARGET)]
        duplicate = podman and MASK not in container.values("--security-opt")
        lines = ["100 90 0:40 / / ro,relatime - overlay overlay ro",
                 "101 100 0:41 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw",
                 *(f"{102 + n} 100 0:42 /crun/.empty-directory {POWERCAP} ro,relatime - "
                   "tmpfs tmpfs ro" for n in range(2 if duplicate else 1))]
        target = {"type": stat.S_IFDIR, "uid": 501, "gid": 20, "mode": 0o700, "links": 2,
                  "device": 40, "bytes": 1 << 30, "readOnly": True}
        if declared:
            options = dict(item.partition("=")[::2] for item in declared[0][len(TARGET) + 1:]
                           .split(","))
            if podman:
                owner = (uid, gid) if "U" in options else (0, 0)
            else:
                owner = (int(options.get("uid", "0")), int(options.get("gid", "0")))
            mode = int(options["mode"], 8)
            size = 1 << 20 if options.get("size") == "1m" else 8 << 30
            flags = [flag for flag in ("rw", "nosuid", "nodev") if flag in options]
            lines.append(f"110 100 0:50 / {TARGET} {','.join([*flags, 'noexec', 'relatime'])} "
                         f"- tmpfs tmpfs rw,size={size // 1024}k,mode={mode:o}")
            target = {"type": stat.S_IFDIR, "uid": owner[0], "gid": owner[1], "mode": mode,
                      "links": 2, "device": 50, "bytes": size, "readOnly": False}
        try:
            parse_mountinfo(("\n".join(lines) + "\n").encode("ascii"))
            parse_error = None
        except MountAttestationError as error:
            parse_error = f"MountAttestationError: {error}"
        capabilities = ZERO if container.values("--cap-drop") == ["ALL"] else "000001ffffffffff"
        nnp = "1" if "no-new-privileges:true" in container.values("--security-opt") else "0"
        evidence = {
            "status": {"Uid": "\t".join([str(uid)] * 4), "Gid": "\t".join([str(gid)] * 4),
                       **dict.fromkeys(("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"),
                                       capabilities),
                       "NoNewPrivs": nnp},
            "label": "system_u:system_r:container_t:s0:c1,c2" if podman
            else "docker-default (enforce)",
            "mountinfo": lines, "parseError": parse_error,
            "root": {"bytes": 1 << 30, "readOnly": True}, "target": target, "entries": [],
        }
        if declared and target["uid"] == uid:
            lock = {"type": stat.S_IFREG, "uid": uid, "gid": gid, "mode": 0o600, "links": 1,
                    "device": 50}
            name = f"root-{uuid.uuid4()}.lock"
            outcome: dict[str, Any] = {
                "coordination": "created", "deploymentLock": lock,
                "concurrent": "CoordinationError", "rootLockName": name, "rootLock": dict(lock),
                "rootConcurrent": "CoordinationBusy", "replacement": True,
                "entries": sorted(["deployment.lock", name]),
            }
        else:
            outcome = {"coordination": "CoordinationError", "entries": []}
        return 0, json.dumps(evidence) + "\n" + json.dumps(outcome) + "\n", ""

    def host_network_refusal(self, container: FakeContainer) -> str | None:
        """Root O12: rootless Podman host networking rbinds the host /sys, so /sys/fs/cgroup
        and /sys/fs/selinux appear twice; the probe's unchanged strict parser refuses."""
        if self.engine != "podman" or container.values("--network") != ["host"]:
            return None
        lines = ["100 90 0:40 / / ro,relatime - overlay overlay ro",
                 "101 100 0:22 / /sys ro,nosuid,nodev,noexec,relatime - sysfs sysfs rw",
                 "102 101 0:27 / /sys/fs/cgroup rw,nosuid,nodev,noexec,relatime - cgroup2 "
                 "cgroup2 rw",
                 "103 101 0:21 / /sys/fs/selinux rw,nosuid,noexec,relatime - selinuxfs "
                 "selinuxfs rw",
                 "104 101 0:42 / /sys/fs/cgroup ro,nosuid,nodev,noexec,relatime - cgroup2 "
                 "cgroup2 rw",
                 "105 101 0:43 / /sys/fs/selinux ro,nosuid,noexec,relatime - selinuxfs "
                 "selinuxfs rw"]
        try:
            parse_mountinfo(("\n".join(lines) + "\n").encode("ascii"))
        except MountAttestationError as error:
            message = f"aegisctl.mounts.MountAttestationError: {error}"
            self.probe_errors.append(message)
            return message
        raise AssertionError("the strict parser admitted duplicated host /sys mounts")

    def database_reachable(self, container: FakeContainer, port: int) -> bool:
        """Root O13: under Podman only pasta -T of this exact port reaches host loopback."""
        network = container.values("--network")
        return network == (["host"] if self.engine == "docker" else [f"pasta:-T,{port}"])

    def _container_info(self, container: FakeContainer) -> dict[str, Any]:
        podman = self.engine == "podman"
        single = {key: value for key, value in container.options if value is not None}
        mounts = []
        for value in container.values("--mount"):
            fields = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
            mounts.append({"Type": fields["type"], "Source": fields["src"],
                           "Destination": fields["dst"], "RW": "readonly" not in value})
        nnp = "no-new-privileges:true" in container.values("--security-opt")
        return {
            "Id": container.identity,
            "Name": container.name if podman else f"/{container.name}",
            "Image": container.image if podman else f"sha256:{container.image}",
            "Config": {"Labels": dict(container.labels), "User": single.get("--user", "")},
            "State": {"Status": container.status, "Running": False},
            "HostConfig": {
                "ReadonlyRootfs": container.has("--read-only"),
                "Privileged": False,
                "NetworkMode": single.get("--network", "bridge"),
                "CapDrop": (MODELED_PODMAN_CAP_DROP if podman else ["ALL"])
                if single.get("--cap-drop") == "ALL" else [],
                "CapAdd": [] if podman else None,
                "SecurityOpt": (["no-new-privileges"] if podman else ["no-new-privileges:true"])
                if nnp else [],
                "Memory": {"128m": 128 << 20, "256m": 256 << 20}.get(single.get("--memory", ""), 0),
                "PidsLimit": int(single.get("--pids-limit", "2048" if podman else "0")),
            },
            "Mounts": mounts,
            "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2" if podman else "",
            "MountLabel": "system_u:object_r:container_file_t:s0:c1,c2" if podman else "",
        }


class _Sink:
    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, data: str) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        return None


class _Lines:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def readline(self) -> str:
        return self.lines.pop(0) if self.lines else ""


class FakeAttachedProcess:
    """The runtime case's attached interactive process with its two-line handshake."""

    def __init__(self, fake: FakeEngine, argv: list[str]) -> None:
        assert argv[:len(fake.prefix)] == fake.prefix, argv
        arguments = argv[len(fake.prefix):]
        fake.calls.append(arguments)
        self.fake = fake
        self.args = argv
        self.returncode: int | None = None
        self.error = ""
        self.container: FakeContainer | None
        if arguments[0] == "run":  # the pre-correction `run --interactive`
            code, out, self.error = fake._launch("run", arguments[1:], execute=False)
            self.container = fake.containers.get(out.strip()) if code == 0 else None
        else:
            assert arguments[:3] == ["start", "--attach", "--interactive"], arguments
            (identity,) = arguments[3:]
            self.container = fake.containers[identity]
            assert self.container.status == "created"
        # RUNTIME_PROBE parses mountinfo before its ready line; a refusal ends it there.
        self.refusal = None if self.container is None else fake.host_network_refusal(
            self.container,
        )
        if self.container is not None and self.refusal is not None:
            self.container.status = "exited"
        self.stdin = _Sink()
        ready = json.dumps({"phase": "ready", "digest": "0" * 64}) + "\n"
        self.stdout = _Lines(
            [ready] if self.container is not None and self.refusal is None else [],
        )

    def __enter__(self) -> FakeAttachedProcess:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def communicate(
        self, input: str | None = None, timeout: float | None = None,
    ) -> tuple[str, str]:
        del timeout
        data = json.loads(self.stdin.written[0])
        assert input == "go\n" and data["mode"]
        if self.container is None:
            self.returncode = 125
            return "", self.error
        if self.refusal is not None:
            self.returncode = 1
            return "", self.refusal
        if not self.fake.database_reachable(self.container, data["port"]):
            self.container.status = "exited"
            self.returncode = 1
            return "", f"psycopg.OperationalError: 127.0.0.1:{data['port']} connection refused"
        code, out, err = self.fake._execute(self.container)
        self.returncode = code
        return out, err

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def kill(self) -> None:
        return None


def stub_runtime_database(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Replace only the runtime case's ORM setup; its container lifecycle runs unchanged."""
    from aegis_apps.catalog.models import CatalogEntry
    from aegis_apps.indexing.models import IndexDeployment

    roots = iter([SimpleNamespace(slot_id="slot-large", pk=uuid.uuid4()),
                  SimpleNamespace(slot_id="slot-small", pk=uuid.uuid4())])
    monkeypatch.setattr(
        "tests.deployment.test_database_roles._create_scan_fixture",
        lambda: (next(roots), "synthetic-worker", "a" * 64),
    )
    prior = SimpleNamespace(source_state="present", refresh_from_db=lambda: None)
    monkeypatch.setattr(CatalogEntry, "objects", SimpleNamespace(
        get=lambda **_: SimpleNamespace(), create=lambda **_: prior,
    ))
    monkeypatch.setattr(IndexDeployment, "objects", SimpleNamespace(
        filter=lambda **_: SimpleNamespace(update=lambda **_: 1),
    ))
    return SimpleNamespace(port=DATABASE_PORT, database_name="synthetic",
                           passwords={"aegis_indexer": "synthetic"})


@dataclass
class Harness:
    engine: str
    fake: FakeEngine
    factory: pytest.TempPathFactory
    image: str
    database: SimpleNamespace
    generators: list[Generator[str]] = field(default_factory=list)

    def call(self, function: Callable[..., Any], **parameters: Any) -> Any:
        """Call a fixture or test body with exactly the arguments its signature requests."""
        available = {
            "tmp_path": self.factory.mktemp("flow"), "tmp_path_factory": self.factory,
            "indexer_image": self.image, "role_database": self.database, **parameters,
        }
        requested = inspect.signature(function).parameters
        return function(**{name: available[name] for name in requested})


@pytest.fixture(params=("docker", "podman"))
def harness(
    request: pytest.FixtureRequest, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Harness]:
    engine = request.param
    prefix = select_fake_engine(engine, tmp_path, monkeypatch, checked_mask_policy=True)
    fake = FakeEngine(engine, prefix)
    monkeypatch.setattr(runtime, "CONTAINER_COMMAND", prefix)
    monkeypatch.setattr(subprocess, "run", fake.run)
    monkeypatch.setattr(subprocess, "Popen", fake.popen)
    image = fake.add_image({OWNER: "aegis-task7-synthetic"}, tag="aegis-task7-synthetic")
    fake.add_image({}, tag="aegis-backend")
    state = Harness(engine, fake, tmp_path_factory, image, stub_runtime_database(monkeypatch))
    yield state
    # A fixture generator left suspended by a failed assertion must finish here, while the
    # in-memory engine is still installed; later garbage collection would reach real ones.
    for generator in state.generators:
        with contextlib.suppress(Exception):
            generator.close()


def run_flow(harness: Harness, flow: str) -> None:
    if flow == "transport":
        harness.call(runtime.test_actual_linux_reader_transport)
    elif flow == "runtime":
        harness.call(runtime._run_runtime_case, mode="kill")
    elif flow == "restart":
        harness.call(
            runtime.test_parent_death_reaps_reader_before_replacement_admission, reaper="owned",
        )
    elif flow in ("misowned", "absent"):
        harness.call(runtime.test_coordination_fails_closed_without_owned_mount, mount=flow)
    else:
        assert flow in ("dynamic", "fixed"), flow
        harness.call(runtime.test_private_coordination_tmpfs_belongs_to_process_user, user=flow)


def attempt(harness: Harness, flow: str) -> Exception | None:
    try:
        run_flow(harness, flow)
    except Exception as exc:
        return exc
    return None


def launch_profile(launch: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """The launch's isolation options (sources normalized) and its coordination tmpfs."""
    options, _, _ = parse_launch(launch[1:])
    isolating = {"--init", "--interactive", "--network", "--read-only", "--cap-drop",
                 "--security-opt", "--user", "--memory", "--pids-limit", "--tmpfs", "--mount",
                 "--entrypoint"}
    coordination = [value or "" for key, value in options
                    if key == "--tmpfs" and (value or "").startswith(TARGET)]
    isolation = sorted(
        (key, re.sub(r"src=[^,]*", "src=<source>", value or ""))
        for key, value in options
        if key in isolating and value != MASK and (value or "") not in coordination
    )
    return isolation, coordination


def owned(fake: FakeEngine, name: str) -> list[str]:
    return [identity for identity, container in fake.containers.items()
            if container.labels.get(OWNER) == name]


# -- Engine-exact coordination tmpfs, with every other isolation option unchanged ---------


def expected_isolation(engine: str, flow: str) -> list[tuple[str, str]]:
    expected = list(EXPECTED_ISOLATION[flow])
    if flow == "runtime":
        expected.append(("--network", RUNTIME_NETWORK[engine]))
    return sorted((key, value or "") for key, value in expected)


@pytest.mark.parametrize("flow", ("transport", "runtime", "restart", "misowned", "absent"))
def test_coordination_tmpfs_is_engine_exact_and_isolation_is_unchanged(
    harness: Harness, flow: str,
) -> None:
    failure = attempt(harness, flow)
    (launch,) = harness.fake.launches
    isolation, coordination = launch_profile(launch)
    assert coordination == EXPECTED_COORDINATION[harness.engine][flow]
    assert isolation == expected_isolation(harness.engine, flow)
    assert failure is None, failure


@pytest.mark.parametrize("harness", ["podman"], indirect=True)
@pytest.mark.parametrize("drift", ["host", f"pasta:-T,{DATABASE_PORT + 1}"])
def test_podman_runtime_network_drift_is_refused(harness: Harness, drift: str) -> None:
    """Root O12/O13: host networking duplicates /sys mounts, which the unchanged strict
    parser refuses before the ready line; pasta forwarding any other port cannot reach
    the database. Either drift fails closed, and cleanup removes exactly the recorded ID."""
    harness.fake.network_drift = drift
    failure = attempt(harness, "runtime")
    (launch,) = harness.fake.launches
    assert ("--network", RUNTIME_NETWORK["podman"]) in parse_launch(launch[1:])[0]
    if drift == "host":
        assert isinstance(failure, json.JSONDecodeError), failure  # the empty ready line
        assert harness.fake.probe_errors == [
            "aegisctl.mounts.MountAttestationError: mountinfo contains an ambiguous mountpoint",
        ]
    else:
        assert isinstance(failure, AssertionError), failure
        assert f"127.0.0.1:{DATABASE_PORT} connection refused" in str(failure)
    (original,) = harness.fake.launched
    assert harness.fake.removals == [["rm", "--force", original.identity]]
    assert harness.fake.containers == {}


def expected_metadata_tmpfs(engine: str, flow: str) -> list[str]:
    if flow in ("misowned", "absent"):
        return EXPECTED_COORDINATION[engine][flow]
    if engine == "podman":
        return [PODMAN_POSITIVE]
    uid, gid = DYNAMIC if flow == "dynamic" else (501, 20)
    return [f"{TARGET}:rw,nosuid,nodev,size=1m,uid={uid},gid={gid},mode=0700"]


@pytest.mark.parametrize("flow", ("dynamic", "fixed", "misowned", "absent"))
def test_metadata_cases_run_recorded_without_source_or_database(
    harness: Harness, flow: str,
) -> None:
    assert attempt(harness, flow) is None
    (launch,) = harness.fake.launches
    options, image, command = parse_launch(launch[1:])
    assert launch[0] == "create" and image == harness.image
    assert command[:2] == ["-c", runtime.COORDINATION_PROBE]
    assert not [value for key, value in options if key in ("--mount", "--volume")]
    assert ("--network", "none") in options
    assert launch_profile(launch)[1] == expected_metadata_tmpfs(harness.engine, flow)


def test_coordination_tmpfs_accepts_only_known_fixture_identities(harness: Harness) -> None:
    unknown = (DYNAMIC[0] + 7919, DYNAMIC[1] + 7919)
    with pytest.raises(ValueError, match="known fixture identity"):
        runtime.coordination_tmpfs(unknown)
    for owner in (DYNAMIC, (501, 20), None):
        declared = runtime.coordination_tmpfs(owner)
        options = declared.removeprefix(f"{TARGET}:").split(",")
        if harness.engine == "podman":
            assert not {"uid", "gid"} & {option.partition("=")[0] for option in options}
            assert options.count("notmpcopyup") == 1
            assert options.count("U") == (0 if owner is None else 1)


# -- Recorded workload lifecycle ------------------------------------------------------


@pytest.mark.parametrize("flow", CONTAINER_FLOWS)
def test_identity_is_recorded_and_checked_before_the_payload_starts(
    harness: Harness, flow: str,
) -> None:
    assert attempt(harness, flow) is None
    (original,) = harness.fake.launched
    calls = harness.fake.calls
    launched = next(index for index, call in enumerate(calls) if call[0] in ("run", "create"))
    started = next(index for index, call in enumerate(calls)
                   if call[0] == "run" or call[:2] == ["start", "--attach"])
    assert calls[launched][0] == "create"
    assert ["container", "inspect", original.identity] in calls[launched + 1:started]
    options, image, _ = parse_launch(calls[launched][1:])
    assert ("--pull", "never") in options
    assert image == original.image, "launch must name the pinned immutable image ID"
    if flow == "transport":
        assert original.image == harness.fake.resolve_image("aegis-backend")


@pytest.mark.parametrize("flow", CONTAINER_FLOWS)
def test_cleanup_removes_only_the_recorded_id_and_proves_absence(
    harness: Harness, flow: str,
) -> None:
    assert attempt(harness, flow) is None
    (original,) = harness.fake.launched
    removal = ["rm", "--force", original.identity]
    assert harness.fake.removals == [removal]
    assert owned(harness.fake, original.name) == []
    later = harness.fake.calls[harness.fake.calls.index(removal) + 1:]
    assert ["container", "ls", "--all", "--no-trunc", "--quiet",
            "--filter", f"label={OWNER}={original.name}"] in later
    assert ["container", "ls", "--all", "--no-trunc", "--quiet"] in later


@pytest.mark.parametrize("flow", CONTAINER_FLOWS)
def test_same_name_same_label_replacement_is_refused_not_removed(
    harness: Harness, flow: str,
) -> None:
    harness.fake.scenario = "replaced"
    failure = attempt(harness, flow)
    assert isinstance(failure, ContainerEngineError), failure
    assert harness.fake.removals == []
    (original,) = harness.fake.launched
    assert len(owned(harness.fake, original.name)) == 1, "replacement must survive"


@pytest.mark.parametrize("flow", CONTAINER_FLOWS)
def test_unknown_owned_container_stops_cleanup_before_the_first_deletion(
    harness: Harness, flow: str,
) -> None:
    harness.fake.scenario = "extra-owned"
    failure = attempt(harness, flow)
    assert isinstance(failure, ContainerEngineError), failure
    assert harness.fake.removals == []


@pytest.mark.parametrize("scenario", ("owner-drift", "resource-drift"))
def test_label_drift_on_the_recorded_id_is_refused(harness: Harness, scenario: str) -> None:
    harness.fake.scenario = scenario
    failure = attempt(harness, "restart")
    assert isinstance(failure, (ContainerEngineError, AssertionError)), failure
    assert harness.fake.removals == []


@pytest.mark.parametrize("scenario", ("replaced", "owner-drift"))
def test_cleanup_refusal_preserves_the_original_failure(harness: Harness, scenario: str) -> None:
    harness.fake.scenario = scenario
    harness.fake.payload_fails = True
    failure = attempt(harness, "restart")
    assert isinstance(failure, AssertionError), failure
    assert "synthetic payload failure" in str(failure)
    assert any("cleanup refused" in note for note in getattr(failure, "__notes__", ()))
    assert harness.fake.removals == []


@pytest.mark.parametrize("flow", ("restart", "runtime"))
def test_interrupted_create_records_identity_then_propagates(harness: Harness, flow: str) -> None:
    harness.fake.scenario = "interrupt-after-create"
    with pytest.raises(KeyboardInterrupt):
        run_flow(harness, flow)
    (original,) = harness.fake.launched
    assert harness.fake.removals == [["rm", "--force", original.identity]]
    assert harness.fake.containers == {}


def test_interrupt_before_creation_deletes_nothing(harness: Harness) -> None:
    harness.fake.scenario = "interrupt-before-create"
    with pytest.raises(KeyboardInterrupt):
        run_flow(harness, "restart")
    assert harness.fake.launched == [] and harness.fake.removals == []


def test_ambiguous_creation_is_retained_never_adopted(harness: Harness) -> None:
    harness.fake.scenario = "ambiguous-create"
    failure = attempt(harness, "restart")
    assert isinstance(failure, AssertionError) and "synthetic ambiguous create" in str(failure)
    assert any("cleanup refused" in note for note in getattr(failure, "__notes__", ()))
    assert harness.fake.removals == []
    assert len(harness.fake.containers) == 2


def test_failed_creation_without_a_container_deletes_nothing(harness: Harness) -> None:
    harness.fake.scenario = "no-create"
    failure = attempt(harness, "restart")
    assert isinstance(failure, AssertionError) and "synthetic create failure" in str(failure)
    assert harness.fake.removals == [] and harness.fake.containers == {}


def test_payload_timeout_after_identity_removes_the_recorded_id(harness: Harness) -> None:
    harness.fake.scenario = "payload-timeout"
    failure = attempt(harness, "restart")
    assert isinstance(failure, subprocess.TimeoutExpired), failure
    (original,) = harness.fake.launched
    assert harness.fake.removals == [["rm", "--force", original.identity]]
    assert harness.fake.containers == {}


# -- Pinned image ledger --------------------------------------------------------------


def start_image(harness: Harness) -> Generator[str]:
    generator: Generator[str] = harness.call(runtime.indexer_image.__wrapped__)
    harness.generators.append(generator)
    return generator


def test_image_is_pinned_by_built_id_and_removed_exactly(harness: Harness) -> None:
    generator = start_image(harness)
    pinned = next(generator)
    (built,) = harness.fake.built
    assert pinned == built, "tests must receive the immutable built ID, not a tag"
    with pytest.raises(StopIteration):
        next(generator)
    removal = ["image", "rm", built]
    assert harness.fake.removals == [removal]
    assert built not in harness.fake.images and harness.fake.resolve_image(pinned) is None
    later = harness.fake.calls[harness.fake.calls.index(removal) + 1:]
    assert ["image", "ls", "--no-trunc", "--quiet", "--all"] in later


@pytest.mark.parametrize("scenario", ("replaced", "rebound", "label-drift"))
def test_image_scope_change_is_refused_without_deletion(harness: Harness, scenario: str) -> None:
    generator = start_image(harness)
    next(generator)
    fake = harness.fake
    (built,) = fake.built
    tag = fake.tag_of(built)
    labels = dict(fake.images[built]["labels"])
    if scenario == "replaced":
        del fake.images[built], fake.tags[tag]
        fake.add_image(labels, tag=tag)
    elif scenario == "rebound":
        fake.add_image(labels, tag=tag)
    else:
        fake.images[built]["labels"][OWNER] = "aegis-task7-someone-else"
    with pytest.raises((ContainerEngineError, AssertionError)):
        next(generator)
    assert fake.removals == []


def test_unrecorded_build_identity_is_retained_not_adopted(harness: Harness) -> None:
    harness.fake.build_scenario = "unrecorded"
    generator = start_image(harness)
    with pytest.raises(AssertionError) as caught:
        next(generator)
    (built,) = harness.fake.built
    assert harness.fake.removals == [] and built in harness.fake.images
    assert any("never recorded" in note for note in getattr(caught.value, "__notes__", ()))


def test_interrupted_build_records_identity_and_removes_it_exactly(harness: Harness) -> None:
    harness.fake.build_scenario = "interrupted"
    generator = start_image(harness)
    with pytest.raises(KeyboardInterrupt):
        next(generator)
    (built,) = harness.fake.built
    assert harness.fake.removals == [["image", "rm", built]]
    assert built not in harness.fake.images


def test_image_declaring_a_volume_is_refused_before_any_container(harness: Harness) -> None:
    harness.fake.build_volumes = {"/var/lib/anonymous": {}}
    generator = start_image(harness)
    with pytest.raises(AssertionError, match="anonymous volumes"):
        next(generator)
    (built,) = harness.fake.built
    assert harness.fake.removals == [["image", "rm", built]]
    assert harness.fake.launches == []


# -- Engine-written image ID file ------------------------------------------------------


@pytest.mark.parametrize("content", ("sha256:{identity}\n", "{identity}"))
def test_iidfile_yields_the_normalized_identity(tmp_path: Path, content: str) -> None:
    identity = "a1" * 32
    path = tmp_path / "image.iid"
    path.write_text(content.format(identity=identity), encoding="ascii")
    assert runtime._read_iidfile(path) == identity


@pytest.mark.parametrize("unsafe", ("missing", "symlink", "hardlink", "malformed", "oversized"))
def test_unusable_iidfile_records_no_identity(tmp_path: Path, unsafe: str) -> None:
    path = tmp_path / "image.iid"
    outside = tmp_path / "outside"
    outside.write_text("sha256:" + "b2" * 32, encoding="ascii")
    if unsafe == "symlink":
        path.symlink_to(outside)
    elif unsafe == "hardlink":
        os.link(outside, path)
    elif unsafe == "malformed":
        path.write_text("sha256:not-an-image-id", encoding="ascii")
    elif unsafe == "oversized":
        path.write_text("sha256:" + "c3" * 32 + " " * 100, encoding="ascii")
    assert runtime._read_iidfile(path) == ""


# -- Pure metadata validators ---------------------------------------------------------


def valid_rows(case: str) -> list[str]:
    rows = ["100 90 0:40 / / ro,relatime - overlay overlay ro",
            "101 100 0:41 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw",
            f"102 100 0:42 /crun/.empty-directory {POWERCAP} ro,relatime - tmpfs tmpfs ro"]
    superblock = {"misowned": "rw,mode=777", "absent": ""}.get(case, "rw,size=1024k,mode=700")
    if superblock:
        rows.append(f"110 100 0:50 / {TARGET} rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs "
                    f"{superblock}")
    return rows


def valid_evidence(engine: str, case: str, user: tuple[int, int]) -> dict[str, Any]:
    owner, mode = {"misowned": ((0, 0), 0o777), "absent": ((501, 20), 0o700)}.get(
        case, (user, 0o700),
    )
    mounted = case != "absent"
    return {
        "status": {"Uid": "\t".join([str(user[0])] * 4), "Gid": "\t".join([str(user[1])] * 4),
                   **dict.fromkeys(("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"), ZERO),
                   "NoNewPrivs": "1"},
        "label": "system_u:system_r:container_t:s0:c1,c2" if engine == "podman"
        else "docker-default (enforce)",
        "mountinfo": valid_rows(case), "parseError": None,
        "root": {"bytes": 1 << 30, "readOnly": True},
        "target": {"type": stat.S_IFDIR, "uid": owner[0], "gid": owner[1], "mode": mode,
                   "links": 2, "device": 50 if mounted else 40,
                   "bytes": 1 << 20 if case in ("dynamic", "fixed") else 1 << 30,
                   "readOnly": not mounted},
        "entries": [],
    }


def valid_outcome(case: str, user: tuple[int, int]) -> dict[str, Any]:
    if case in ("misowned", "absent"):
        return {"coordination": "CoordinationError", "entries": []}
    lock = {"type": stat.S_IFREG, "uid": user[0], "gid": user[1], "mode": 0o600, "links": 1,
            "device": 50}
    name = "root-0a3c1d6e-2b7f-4c1a-9e8d-5f6a7b8c9d0e.lock"
    return {"coordination": "created", "deploymentLock": lock, "concurrent": "CoordinationError",
            "rootLockName": name, "rootLock": dict(lock), "rootConcurrent": "CoordinationBusy",
            "replacement": True, "entries": ["deployment.lock", name]}


def _row(evidence: dict[str, Any], mountpoint: str, old: str, new: str) -> None:
    lines = evidence["mountinfo"]
    (index,) = [i for i, line in enumerate(lines) if line.split(" ")[4] == mountpoint]
    assert old in lines[index]
    lines[index] = lines[index].replace(old, new, 1)


CASE_USERS = {"dynamic": DYNAMIC, "fixed": (501, 20), "misowned": (501, 20), "absent": (501, 20)}
EVIDENCE_DRIFT: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
    "process-uid": ("fixed", lambda e: e["status"].update(Uid="0\t0\t0\t0")),
    "process-gid": ("fixed", lambda e: e["status"].update(Gid="0\t0\t0\t0")),
    "effective-capability": ("fixed", lambda e: e["status"].update(CapEff="0000000000000400")),
    "bounding-capability": ("misowned", lambda e: e["status"].update(CapBnd="00000000a80425fb")),
    "no-new-privileges": ("fixed", lambda e: e["status"].update(NoNewPrivs="0")),
    "parser-refused": ("fixed", lambda e: e.update(
        parseError="MountAttestationError: mountinfo contains an ambiguous mountpoint")),
    "writable-root": ("fixed", lambda e: _row(e, "/", "/ / ro,", "/ / rw,") or _row(
        e, "/", "overlay overlay ro", "overlay overlay rw")),
    "writable-root-statvfs": ("misowned", lambda e: e["root"].update(readOnly=False)),
    "missing-mount": ("fixed", lambda e: e["mountinfo"].pop()),
    "not-tmpfs": ("fixed", lambda e: _row(e, TARGET, "- tmpfs tmpfs", "- ext4 /dev/vda")),
    "read-only-mount": ("misowned", lambda e: _row(e, TARGET, " rw,nosuid", " ro,nosuid")),
    "subtree-bind": ("fixed", lambda e: _row(e, TARGET, "0:50 / ", "0:50 /other ")),
    "shared-backing": ("fixed", lambda e: e["mountinfo"].append(
        "111 100 0:50 / /elsewhere rw,relatime - tmpfs tmpfs rw")),
    "shared-propagation": (
        "misowned", lambda e: _row(e, TARGET, "relatime -", "relatime shared:7 -"),
    ),
    "missing-nosuid": ("fixed", lambda e: _row(e, TARGET, "rw,nosuid,", "rw,")),
    "missing-nodev": ("misowned", lambda e: _row(e, TARGET, "nodev,", "")),
    "owner": ("fixed", lambda e: e["target"].update(uid=0, gid=0)),
    "mode": ("dynamic", lambda e: e["target"].update(mode=0o755)),
    "not-empty": ("fixed", lambda e: e.update(entries=["copied-image-content"])),
    "not-directory": ("misowned", lambda e: e["target"].update(type=stat.S_IFREG)),
    "target-read-only": ("fixed", lambda e: e["target"].update(readOnly=True)),
    "size-bytes": ("fixed", lambda e: e["target"].update(bytes=2 << 20)),
    "size-superblock": ("dynamic", lambda e: _row(e, TARGET, "size=1024k", "size=2048k")),
    "misowned-owner": ("misowned", lambda e: e["target"].update(uid=501, gid=20)),
    "misowned-mode": ("misowned", lambda e: e["target"].update(mode=0o700)),
    "absent-has-mount": ("absent", lambda e: e["mountinfo"].extend(valid_rows("misowned")[3:])),
    "absent-not-empty": ("absent", lambda e: e.update(entries=["deployment.lock"])),
}
PODMAN_ONLY_DRIFT: dict[str, Callable[[dict[str, Any]], None]] = {
    "selinux-type": lambda e: e.update(label="system_u:system_r:spc_t:s0"),
    "selinux-unavailable": lambda e: e.update(label="PermissionError"),
    "missing-powercap": lambda e: e["mountinfo"].pop(2),
    "writable-powercap": lambda e: _row(e, POWERCAP, "ro,relatime", "rw,relatime") or _row(
        e, POWERCAP, "tmpfs tmpfs ro", "tmpfs tmpfs rw"),
    "duplicate-powercap": lambda e: e["mountinfo"].append(
        f"103 100 0:42 /crun/.empty-directory {POWERCAP} ro,relatime - tmpfs tmpfs ro"),
}


@pytest.mark.parametrize("engine", ("docker", "podman"))
@pytest.mark.parametrize("case", ("dynamic", "fixed", "misowned", "absent"))
def test_valid_coordination_evidence_is_accepted(engine: str, case: str) -> None:
    user = CASE_USERS[case]
    evidence = valid_evidence(engine, case, user)
    runtime.validate_coordination_evidence(engine, case, user, evidence)
    runtime.validate_coordination_outcome(case, user, evidence, valid_outcome(case, user))


@pytest.mark.parametrize("engine", ("docker", "podman"))
@pytest.mark.parametrize("drift", sorted(EVIDENCE_DRIFT))
def test_coordination_evidence_drift_is_refused(engine: str, drift: str) -> None:
    case, mutate = EVIDENCE_DRIFT[drift]
    user = CASE_USERS[case]
    evidence = valid_evidence(engine, case, user)
    mutate(evidence)
    with pytest.raises((AssertionError, MountAttestationError)):
        runtime.validate_coordination_evidence(engine, case, user, evidence)


@pytest.mark.parametrize("drift", sorted(PODMAN_ONLY_DRIFT))
def test_podman_confinement_and_single_mask_drift_is_refused(drift: str) -> None:
    evidence = valid_evidence("podman", "fixed", (501, 20))
    PODMAN_ONLY_DRIFT[drift](evidence)
    with pytest.raises((AssertionError, MountAttestationError)):
        runtime.validate_coordination_evidence("podman", "fixed", (501, 20), evidence)


def _lock(field_name: str, value: object) -> Callable[[dict[str, Any]], None]:
    return lambda outcome: outcome["deploymentLock"].update({field_name: value})


OUTCOME_DRIFT: dict[str, tuple[str, Callable[[dict[str, Any]], None]]] = {
    "positive-rejected": ("fixed", lambda o: o.clear() or o.update(
        coordination="CoordinationError", entries=[])),
    "concurrent-admitted": ("fixed", lambda o: o.update(concurrent="admitted")),
    "root-concurrent-admitted": ("dynamic", lambda o: o.update(rootConcurrent="admitted")),
    "root-concurrent-other": ("fixed", lambda o: o.update(rootConcurrent="CoordinationError")),
    "lock-mode": ("fixed", _lock("mode", 0o644)),
    "lock-owner": ("dynamic", _lock("uid", 0)),
    "lock-group": ("fixed", _lock("gid", 0)),
    "lock-links": ("fixed", _lock("links", 2)),
    "lock-device": ("fixed", _lock("device", 51)),
    "lock-type": ("fixed", _lock("type", stat.S_IFLNK)),
    "root-lock-mode": ("fixed", lambda o: o["rootLock"].update(mode=0o640)),
    "root-lock-name": ("fixed", lambda o: o.update(rootLockName="root-x.lock")),
    "no-replacement": ("fixed", lambda o: o.pop("replacement")),
    "extra-entry": ("fixed", lambda o: o["entries"].append("unexpected")),
    "negative-created": ("misowned", lambda o: o.update(coordination="created")),
    "negative-busy": ("misowned", lambda o: o.update(coordination="CoordinationBusy")),
    "negative-left-lock": ("misowned", lambda o: o.update(entries=["deployment.lock"])),
    "absent-created": ("absent", lambda o: o.update(coordination="created")),
}


@pytest.mark.parametrize("drift", sorted(OUTCOME_DRIFT))
def test_coordination_outcome_drift_is_refused(drift: str) -> None:
    case, mutate = OUTCOME_DRIFT[drift]
    user = CASE_USERS[case]
    evidence = valid_evidence("podman", case, user)
    outcome = copy.deepcopy(valid_outcome(case, user))
    mutate(outcome)
    with pytest.raises(AssertionError):
        runtime.validate_coordination_outcome(case, user, evidence, outcome)


def valid_container(engine: str) -> dict[str, Any]:
    podman = engine == "podman"
    return {
        "Config": {"User": "501:20"},
        "HostConfig": {
            "ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "none",
            "CapDrop": list(MODELED_PODMAN_CAP_DROP) if podman else ["ALL"],
            "CapAdd": [] if podman else None,
            "SecurityOpt": ["no-new-privileges"] if podman else ["no-new-privileges:true"],
            "Memory": 128 << 20, "PidsLimit": 32,
        },
        "Mounts": [],
        "ProcessLabel": "system_u:system_r:container_t:s0:c1,c2" if podman else "",
        "MountLabel": "system_u:object_r:container_file_t:s0:c1,c2" if podman else "",
    }


CONTAINER_DRIFT: dict[str, Callable[[dict[str, Any]], None]] = {
    "user": lambda i: i["Config"].update(User="0:0"),
    "writable-root": lambda i: i["HostConfig"].update(ReadonlyRootfs=False),
    "privileged": lambda i: i["HostConfig"].update(Privileged=True),
    "network": lambda i: i["HostConfig"].update(NetworkMode="bridge"),
    "cap-drop": lambda i: i["HostConfig"].update(CapDrop=["CAP_NET_RAW"]),
    "cap-add": lambda i: i["HostConfig"].update(CapAdd=["CAP_NET_RAW"]),
    "security-opt": lambda i: i["HostConfig"].update(SecurityOpt=["label=disable"]),
    "source-bind": lambda i: i["Mounts"].append({"Type": "bind", "Destination": "/srv"}),
    "volume": lambda i: i["Mounts"].append({"Type": "volume", "Destination": TARGET}),
    "limits": lambda i: i["HostConfig"].update(PidsLimit=64),
}


@pytest.mark.parametrize("engine", ("docker", "podman"))
def test_valid_metadata_container_is_accepted(engine: str) -> None:
    runtime.validate_coordination_container(engine, valid_container(engine), "501:20", (
        128 << 20, 32,
    ))


@pytest.mark.parametrize("engine", ("docker", "podman"))
@pytest.mark.parametrize("drift", sorted(CONTAINER_DRIFT))
def test_metadata_container_drift_is_refused(engine: str, drift: str) -> None:
    info = valid_container(engine)
    CONTAINER_DRIFT[drift](info)
    with pytest.raises(AssertionError):
        runtime.validate_coordination_container(engine, info, "501:20", (128 << 20, 32))


@pytest.mark.parametrize("label", ("system_u:system_r:spc_t:s0", ""))
def test_podman_metadata_container_requires_selinux_confinement(label: str) -> None:
    info = valid_container("podman")
    info["ProcessLabel"] = label
    with pytest.raises(AssertionError):
        runtime.validate_coordination_container("podman", info, "501:20", None)
