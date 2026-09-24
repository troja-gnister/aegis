from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from tests.support.fake_container_engine import select_fake_engine

POWERCAP = "/sys/devices/virtual/powercap"
IMAGE = "a" * 64
ROOT_ROW = "1 0 0:1 / / ro - overlay overlay ro"
MASK_ROW = f"2 1 0:2 /crun/.empty-directory {POWERCAP} ro - tmpfs tmpfs ro"
DUPLICATE_ROW = f"3 2 0:2 /crun/.empty-directory {POWERCAP} ro - tmpfs tmpfs ro"


def policy_module() -> Any:
    name = "aegisctl.podman_mask_compatibility"
    assert importlib.util.find_spec(name), "shared checked Podman mask policy is missing"
    return importlib.import_module(name)


class MaskEngine:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.commands: list[list[str]] = []
        self.resources: dict[str, dict[str, Any]] = {}
        self.created: list[tuple[str, bool]] = []
        self.started: list[str] = []
        self.removed: list[str] = []
        self.bad_oci = False
        self.fail_start: BaseException | None = None
        self.fail_cleanup = False
        self.unknown = False
        self.provider_version = "2.39.4"
        self.info: dict[str, Any] = {
            "host": {
                "arch": "amd64",
                "os": "linux",
                "hostname": "fixture",
                "kernel": "test",
                "cgroupVersion": "v2",
                "cgroupManager": "systemd",
                "ociRuntime": {
                    "name": "crun",
                    "path": str(root / "crun"),
                    "version": "crun version 1.28\ncommit: fixture",
                },
                "security": {"rootless": True, "selinuxEnabled": True, "seccompEnabled": True},
                "idMappings": {"uidmap": [{"host_id": os.geteuid()}]},
            },
            "store": {
                "graphRoot": str(root),
                "runRoot": str(root),
                "graphDriverName": "overlay",
                "volumePath": str(root / "volumes"),
            },
            "version": {
                "Version": "5.8.7",
                "APIVersion": "5.8.7",
                "GitCommit": "fixture",
                "Built": 1789516800,
                "BuiltTime": "Wed Sep 16 00:00:00 2026",
            },
        }
        self.api_info = deepcopy(self.info)
        self.api_version: dict[str, Any] = {
            "Version": "5.8.7",
            "ApiVersion": "1.44",
            "GitCommit": "fixture",
            "Components": [
                {
                    "Name": "Podman Engine",
                    "Version": "5.8.7",
                    "Details": {"APIVersion": "5.8.7", "GitCommit": "fixture"},
                },
                {"Name": "OCI Runtime (crun)", "Version": "crun version 1.28\ncommit: fixture"},
            ],
        }
        for executable in ("podman", "crun"):
            path = root / executable
            path.write_text("synthetic executable")
            path.chmod(0o700)

    def api(self, socket: Path, path: str) -> dict[str, Any]:
        del socket
        assert path in ("/v5.8.7/libpod/info", "/version")
        return deepcopy(self.api_info if path.endswith("info") else self.api_version)

    def run(self, command: list[str], environment: Any = None, **kwargs: Any) -> Any:
        del environment, kwargs
        self.commands.append(command)
        if command[0].endswith("fake-compose-provider"):
            assert command[1:] == ["version", "--short"]
            return subprocess.CompletedProcess(command, 0, self.provider_version, "")
        assert command[:2] == ["podman", "--remote=false"]
        args = command[2:]
        output: Any = ""
        if args == ["info", "--format", "json"]:
            output = self.info
        elif args == ["version", "--format", "json"]:
            output = {"Client": self.info["version"]}
        elif args[:2] == ["image", "inspect"]:
            assert args[2] in ("aegis-backend", IMAGE, "sha256:" + IMAGE)
            output = [{"Id": "sha256:" + IMAGE, "Architecture": "amd64", "Os": "linux"}]
        elif args[0] in ("container", "network", "volume") and args[1] == "ls":
            label = args[-1].removeprefix("label=")
            key, value = label.split("=", 1)
            output = "\n".join(
                cid
                for cid, item in self.resources.items()
                if args[0] == "container" and item["Config"]["Labels"].get(key) == value
            )
        elif args[:2] == ["container", "inspect"]:
            output = [self.resources[args[2]]]
        elif args[0] in ("create", "compose"):
            if args[0] == "compose":
                config = json.loads(Path(args[args.index("-f") + 1]).read_text())
                service = config["services"]["probe"]
                assert set(config["services"]) == {"probe"}
                assert service["image"] == IMAGE and service["pull_policy"] == "never"
                assert "volumes" not in service and "ports" not in service
                owner = args[args.index("-p") + 1]
                labels = service["labels"] | {
                    "com.docker.compose.project": owner,
                    "com.docker.compose.service": "probe",
                }
                candidate = f"unmask={POWERCAP}" in service["security_opt"]
                transport = "compose"
            else:
                owner = args[args.index("--name") + 1]
                labels = dict(
                    args[index + 1].split("=", 1)
                    for index, value in enumerate(args)
                    if value == "--label"
                )
                candidate = f"unmask={POWERCAP}" in args
                transport = "native"
                for option, value in (
                    ("--pull", "never"),
                    ("--network", "none"),
                    ("--user", "10001:10001"),
                    ("--memory", "64m"),
                    ("--pids-limit", "64"),
                    ("--cpus", "0.5"),
                ):
                    assert args[args.index(option) + 1] == value
                assert "--read-only" in args and "no-new-privileges:true" in args
                assert "--volume" not in args and "--mount" not in args
            cid = f"{len(self.created) + 1:064x}"
            self.created.append((transport, candidate))
            oci = self.root / f"{cid}.json"
            self.resources[cid] = {
                "Id": cid,
                "Name": owner,
                "Created": "2026-09-24T00:00:00Z",
                "Image": IMAGE,
                "Config": {"Labels": labels, "User": "10001:10001"},
                "HostConfig": {
                    "ReadonlyRootfs": True,
                    "NetworkMode": "none",
                    "Privileged": False,
                    "SecurityOpt": ["no-new-privileges"],
                    "Memory": 67108864,
                    "PidsLimit": 64,
                    "NanoCpus": 500000000,
                },
                "ProcessLabel": "container_t",
                "MountLabel": "container_file_t",
                "EffectiveCaps": [],
                "BoundingCaps": [],
                "Mounts": [],
                "OCIConfigPath": str(oci),
                "candidate": candidate,
            }
            if self.unknown:
                self.resources["f" * 64] = deepcopy(self.resources[cid]) | {"Id": "f" * 64}
            output = cid
        elif args[0] == "init":
            item = self.resources[args[1]]
            masks = ["/proc/kcore", POWERCAP] + ([] if item["candidate"] else [POWERCAP])
            if self.bad_oci and item["candidate"]:
                masks.remove(POWERCAP)
            Path(item["OCIConfigPath"]).write_text(
                json.dumps(
                    {
                        "linux": {"maskedPaths": masks, "readonlyPaths": ["/proc/sys"]},
                        "root": {"readonly": True},
                        "process": {
                            "noNewPrivileges": True,
                            "capabilities": {
                                "bounding": [],
                                "effective": [],
                                "inheritable": [],
                                "permitted": [],
                                "ambient": [],
                            },
                        },
                    }
                )
            )
        elif args[:2] == ["start", "--attach"]:
            self.started.append(args[2])
            if self.fail_start:
                raise self.fail_start
            item = self.resources[args[2]]
            rows = [ROOT_ROW, MASK_ROW] + ([] if item["candidate"] else [DUPLICATE_ROW])
            output = {
                "rows": rows,
                "uid": 10001,
                "gid": 10001,
                "powercapEmptyOrDenied": True,
                "capabilities": {
                    "CapInh": "0000000000000000",
                    "CapPrm": "0000000000000000",
                    "CapEff": "0000000000000000",
                    "CapBnd": "0000000000000000",
                    "CapAmb": "0000000000000000",
                },
                "parseError": None
                if item["candidate"]
                else "mountinfo contains an ambiguous mountpoint",
            }
        elif args[:2] == ["rm", "--force"]:
            if self.fail_cleanup:
                return subprocess.CompletedProcess(command, 1, "", "cleanup failed")
            self.removed.append(args[2])
            del self.resources[args[2]]
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(
            command, 0, output if isinstance(output, str) else json.dumps(output), ""
        )


@pytest.fixture
def mask_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, MaskEngine]:
    policy = policy_module()
    select_fake_engine("podman", tmp_path, monkeypatch)
    fake = MaskEngine(tmp_path)
    monkeypatch.setattr(policy, "_run", fake.run)
    monkeypatch.setattr(policy, "_api_json", fake.api)
    monkeypatch.setattr(policy, "_selinux_enforcing", lambda: True)
    monkeypatch.setattr(policy.shutil, "which", lambda name, **kwargs: str(tmp_path / name))
    monkeypatch.setattr("aegisctl.container_resources._run", fake.run)
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return policy, fake


def test_docker_policy_has_no_probe_or_image_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = policy_module()
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    monkeypatch.setattr(policy, "_run", lambda *a, **k: pytest.fail("Docker executed probe"))
    assert policy.require_podman_mask_compatibility() == ()


def test_four_profiles_complete_cleanup_on_each_admission(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    assert policy.require_podman_mask_compatibility() == (f"unmask={POWERCAP}",)
    assert fake.created == [
        ("native", False),
        ("native", True),
        ("compose", False),
        ("compose", True),
    ]
    assert len(fake.removed) == 4 and not fake.resources
    assert policy.require_podman_mask_compatibility() == (f"unmask={POWERCAP}",)
    assert len(fake.created) == len(fake.removed) == 8 and not fake.resources
    fake.api_info["host"]["kernel"] = "replacement"
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert len(fake.created) == len(fake.removed) == 8 and not fake.resources


def test_measured_build_display_pair_is_accepted_without_mutation(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    fake.api_info["version"]["BuiltTime"] = "Tue Sep 15 19:00:00 2026"
    native_before = deepcopy(fake.info)
    api_before = deepcopy(fake.api_info)

    assert policy.require_podman_mask_compatibility() == (policy.MASK_OPTION,)
    assert len(fake.created) == len(fake.started) == len(fake.removed) == 4
    assert fake.info == native_before and fake.api_info == api_before


@pytest.mark.parametrize(
    "display", ["Sun Sep  6 00:00:00 2026", "Thu Feb 29 23:59:59 2024"]
)
def test_valid_ansi_calendar_display_is_accepted(
    mask_engine: tuple[Any, MaskEngine], display: str
) -> None:
    policy, fake = mask_engine
    fake.info["version"]["BuiltTime"] = display
    fake.api_info["version"]["BuiltTime"] = display
    assert policy.require_podman_mask_compatibility() == (policy.MASK_OPTION,)
    assert len(fake.created) == len(fake.removed) == 4 and not fake.resources


@pytest.mark.parametrize("field", ["Built", "BuiltTime"])
@pytest.mark.parametrize("origin", ["native", "api"])
def test_required_build_fields_refuse_when_missing(
    mask_engine: tuple[Any, MaskEngine], field: str, origin: str
) -> None:
    policy, fake = mask_engine
    del (fake.info if origin == "native" else fake.api_info)["version"][field]
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.created


@pytest.mark.parametrize("built", [None, True, 1.0, "1789516800", -(2**63) - 1, 2**63])
def test_invalid_build_integer_refuses_before_profiles(
    mask_engine: tuple[Any, MaskEngine], built: Any
) -> None:
    policy, fake = mask_engine
    fake.info["version"]["Built"] = built
    fake.api_info["version"]["Built"] = built
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.created


@pytest.mark.parametrize(
    "display",
    [
        None,
        0,
        "",
        "Wed Sep 16 00:00:00 2026 ",
        "Wed Sep 16 00:00:00 2026" * 100,
        "Wed Sep 06 00:00:00 2026",
        "Wed Sep  0 00:00:00 2026",
        "Wed Sep 32 00:00:00 2026",
        "Wed Feb 30 00:00:00 2026",
        "Tue Sep 16 00:00:00 2026",
        "Wed Sep 16 24:00:00 2026",
        "Wed Sep 16 00:60:00 2026",
        "Wed Sep 16 00:00:60 2026",
        "Wed Sep 16 00:00:00 0000",
        "Wed Sép 16 00:00:00 2026",
    ],
)
def test_invalid_build_display_refuses_before_profiles(
    mask_engine: tuple[Any, MaskEngine], display: Any
) -> None:
    policy, fake = mask_engine
    fake.info["version"]["BuiltTime"] = display
    fake.api_info["version"]["BuiltTime"] = display
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.created


@pytest.mark.parametrize("field", ["Built", "GitCommit", "Extra", "Version", "APIVersion"])
def test_other_api_version_drift_refuses_before_profiles(
    mask_engine: tuple[Any, MaskEngine], field: str
) -> None:
    policy, fake = mask_engine
    fake.api_info["version"]["BuiltTime"] = "Tue Sep 15 19:00:00 2026"
    fake.api_info["version"][field] = {
        "Built": 1789516801,
        "GitCommit": "other",
        "Extra": "other",
        "Version": "5.8.8",
        "APIVersion": "5.8.8",
    }[field]
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.created


def test_missing_non_display_version_key_refuses_before_profiles(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    del fake.api_info["version"]["GitCommit"]
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.created


@pytest.mark.parametrize("path", ["hostname", "security", "ociRuntime", "idMappings", "store"])
def test_other_selected_identity_drift_refuses_with_display_pair(
    mask_engine: tuple[Any, MaskEngine], path: str
) -> None:
    policy, fake = mask_engine
    fake.api_info["version"]["BuiltTime"] = "Tue Sep 15 19:00:00 2026"
    if path == "store":
        fake.api_info["store"]["graphRoot"] = "other"
    elif path in ("security", "ociRuntime", "idMappings"):
        fake.api_info["host"][path]["other"] = "drift"
    else:
        fake.api_info["host"][path] = "other"
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.created


def test_native_client_display_mismatch_refuses_before_profiles(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, fake = mask_engine
    run = fake.run

    def client_drift(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = run(command, *args, **kwargs)
        if command[2:] == ["version", "--format", "json"]:
            value = json.loads(result.stdout)
            value["Client"]["BuiltTime"] = "Tue Sep 15 19:00:00 2026"
            result.stdout = json.dumps(value)
        return result

    monkeypatch.setattr(policy, "_run", client_drift)
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.created


@pytest.mark.parametrize("field", ["Built", "BuiltTime"])
def test_native_client_requires_build_fields_before_profiles(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    policy, fake = mask_engine
    run = fake.run

    def missing_client_field(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = run(command, *args, **kwargs)
        if command[2:] == ["version", "--format", "json"]:
            value = json.loads(result.stdout)
            del value["Client"][field]
            result.stdout = json.dumps(value)
        return result

    monkeypatch.setattr(policy, "_run", missing_client_field)
    with pytest.raises(policy.PodmanMaskError) as caught:
        policy.require_podman_mask_compatibility()
    assert "invalid Podman build" in str(caught.value.__cause__)
    assert not fake.created


def test_final_identity_revalidation_refuses_same_process_display_change(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, fake = mask_engine
    run = fake.run

    def changed_native(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = run(command, *args, **kwargs)
        if command[2:4] == ["rm", "--force"] and len(fake.removed) == 4:
            fake.info["version"]["BuiltTime"] = "Tue Sep 15 19:00:00 2026"
        return result

    monkeypatch.setattr(policy, "_run", changed_native)
    with pytest.raises(policy.PodmanMaskError, match="compatibility refused") as caught:
        policy.require_podman_mask_compatibility()
    assert "identity changed during behavior check" in str(caught.value.__cause__)
    assert len(fake.created) == len(fake.removed) == 4 and not fake.resources


@pytest.mark.parametrize("change", ["native", "api", "rootless", "runtime", "provider", "socket"])
def test_unsupported_identity_refuses_before_create(
    mask_engine: tuple[Any, MaskEngine],
    change: str,
) -> None:
    policy, fake = mask_engine
    if change == "native":
        fake.info["version"]["Version"] = "5.9.0"
    elif change == "api":
        fake.api_version["Components"][0]["Details"]["GitCommit"] = "other"
    elif change == "rootless":
        fake.info["host"]["security"]["rootless"] = False
    elif change == "runtime":
        fake.info["host"]["ociRuntime"]["version"] = "crun version 1.29"
    elif change == "provider":
        fake.provider_version = "2.40.0"
    else:
        Path(os.environ["AEGIS_PODMAN_SOCKET"]).unlink()
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert fake.created == []


def test_missing_candidate_mask_refuses_before_candidate_payload(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    fake.bad_oci = True
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert len(fake.started) == 1
    assert len(fake.removed) == 2 and not fake.resources


@pytest.mark.parametrize("failure", ["timeout", "interrupt", "unknown", "cleanup"])
def test_failure_never_returns_reusable_evidence(
    mask_engine: tuple[Any, MaskEngine],
    failure: str,
) -> None:
    policy, fake = mask_engine
    if failure == "timeout":
        fake.fail_start = subprocess.TimeoutExpired("synthetic", 10)
    elif failure == "interrupt":
        fake.fail_start = KeyboardInterrupt()
        fake.fail_cleanup = True
    elif failure == "unknown":
        fake.unknown = True
    else:
        fake.fail_cleanup = True
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    if failure == "unknown":
        assert not fake.removed
    reports = list(fake.root.glob("aegis-mask-check-*/evidence.jsonl"))
    assert reports and reports[0].stat().st_size > 0
    assert all(
        json.loads(line)["event"] != "completed" for line in reports[0].read_text().splitlines()
    )


def test_replaced_provider_requires_new_four_profile_evidence(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    policy.require_podman_mask_compatibility()
    Path(os.environ["PODMAN_COMPOSE_PROVIDER"]).write_text("replacement executable")
    policy.require_podman_mask_compatibility()
    assert len(fake.created) == len(fake.removed) == 8


def test_probe_diagnostics_refuse_tmpdir_inside_original_before_writing(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, fake = mask_engine
    original = fake.root / "original"
    original.mkdir()
    monkeypatch.setenv("TMPDIR", str(original))
    # Supply an explicit synthetic mount topology; the outside-originals guard remains real.
    monkeypatch.setattr("aegisctl.mounts._host_mountinfo", lambda: None)
    monkeypatch.setattr("aegisctl.mounts._host_mountpoints", lambda records: ())
    with pytest.raises(policy.PodmanMaskError) as caught:
        policy.require_podman_mask_compatibility(originals=(original,))
    assert str(caught.value.__cause__) == "generated artifacts must be outside original roots"
    assert not list(original.iterdir()) and not fake.commands


def launch_module() -> Any:
    name = "aegisctl.container_launch"
    assert importlib.util.find_spec(name), "checked launch entrypoint is missing"
    return importlib.import_module(name)


def test_native_complete_argv_places_option_before_image_and_preserves_application_args(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    _, fake = mask_engine
    launch = launch_module()
    command = [
        "podman",
        "--remote=false",
        "run",
        "--rm",
        "--security-opt",
        "no-new-privileges:true",
        "--env",
        "EXAMPLE=image-value",
        "image",
        "--security-opt",
        "unmask=ALL",
    ]
    actual = launch.controlled_container_argv(command)
    assert actual == [*command[:3], "--security-opt", f"unmask={POWERCAP}", *command[3:]]
    assert len(fake.created) == 4


@pytest.mark.parametrize(
    "option", ["unmask=ALL", "unmask=/proc", "mask=/sys", "systempaths=unconfined"]
)
def test_conflicting_native_masks_refuse_before_probe(
    mask_engine: tuple[Any, MaskEngine],
    option: str,
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    with pytest.raises(policy.PodmanMaskError):
        launch.controlled_container_argv(
            ["podman", "--remote=false", "create", "--security-opt", option, "image"]
        )
    assert not fake.created


def test_native_gate_failure_never_returns_original_bearing_command(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    fake.provider_version = "other"
    with pytest.raises(policy.PodmanMaskError):
        launch.controlled_container_argv(["podman", "--remote=false", "run", "image"])
    assert not fake.created


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_raw_cleanup_remains_available_without_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
) -> None:
    launch = launch_module()
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(
        launch, "require_podman_mask_compatibility", lambda *a, **k: pytest.fail("cleanup gated")
    )
    command = [*prefix, "rm", "--force", "owned-id"]
    assert launch.controlled_container_argv(command) == command


def test_canonical_selection_is_pure_and_does_not_expand_generic_services(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    _, fake = mask_engine
    launch = launch_module()
    base = Path(__file__).resolve().parents[4] / "compose.yaml"
    arguments = ["--profile", "tls-local", "-f", str(base), "-f", "known-override.json"]
    assert launch.canonical_compose_arguments(arguments, base) == [
        *arguments[:-1],
        str(Path("known-override.json").resolve()),
        "-f",
        str(base.with_name("compose.podman.yaml")),
    ]
    generic = ["-f", "observer.json"]
    assert launch.canonical_compose_arguments(generic, base) == generic
    assert not fake.commands


def test_docker_launch_is_byte_for_byte_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    launch = launch_module()
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    monkeypatch.setattr(
        launch, "require_podman_mask_compatibility", lambda *a, **k: pytest.fail("Docker gated")
    )
    command = ["docker", "run", "--rm", "image", "arg"]
    assert launch.controlled_container_argv(command) == command


def test_observer_gate_failure_precedes_original_bearing_compose(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegisctl import mounts

    policy, fake = mask_engine
    original = fake.root / "original"
    original.mkdir()
    slot = mounts.ValidatedSlot(
        "photos", original, "/srv/aegis/roots/photos", "read_only", 1, 2, "local:1:2"
    )
    calls: list[tuple[Path, ...]] = []

    def refuse(*args: Any, originals: tuple[Path, ...], **kwargs: Any) -> tuple[str, ...]:
        calls.append(originals)
        raise policy.PodmanMaskError("Podman mask compatibility refused")

    monkeypatch.setattr(mounts, "ensure_outputs_outside_originals", lambda *a: None)
    monkeypatch.setattr(policy, "require_podman_mask_compatibility", refuse)
    if hasattr(mounts, "require_podman_mask_compatibility"):
        monkeypatch.setattr(mounts, "require_podman_mask_compatibility", refuse)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("original-bearing launch"))
    with pytest.raises(mounts.ConfigError, match="mask compatibility"):
        mounts.observe_mount_fingerprints((slot,))
    assert calls == [(original,)]


def test_static_podman_overlay_has_exact_services_and_keeps_nnp() -> None:
    import yaml

    path = Path(__file__).resolve().parents[4] / "compose.podman.yaml"
    document = yaml.safe_load(path.read_text())
    assert set(document["services"]) == {
        "postgres",
        "migrate",
        "web",
        "operations",
        "indexer",
        "media",
        "gateway",
        "caddy",
        "caddy-local",
    }
    for service in document["services"].values():
        assert service == {"security_opt": ["no-new-privileges:true", f"unmask={POWERCAP}"]}


@pytest.mark.parametrize(
    "module_name,wrapper",
    [
        ("test_rendered_mounts", "_docker"),
        ("test_gateway_http", "docker"),
        ("test_postgres_role_init", "_docker"),
    ],
)
def test_existing_native_wrapper_demands_policy_before_create(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    wrapper: str,
) -> None:
    policy, _ = mask_engine
    module = importlib.import_module(f"tests.deployment.{module_name}")
    monkeypatch.setattr(module, "CONTAINER_COMMAND", ["podman", "--remote=false"])
    launch = launch_module()

    def refuse(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        raise policy.PodmanMaskError("synthetic gate refusal")

    monkeypatch.setattr(launch, "require_podman_mask_compatibility", refuse)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("unchecked original launch"))
    with pytest.raises(policy.PodmanMaskError, match="synthetic gate refusal"):
        getattr(module, wrapper)("create", "image")


def test_shell_support_gates_and_launches_in_same_process(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import runpy

    policy, _ = mask_engine
    support = runpy.run_path(str(Path(__file__).resolve().parents[4] / "scripts/e2e_support.py"))
    assert "controlled_compose" in support, "shell has no checked Compose execution path"
    launch = launch_module()
    monkeypatch.setattr(launch, "_checked_compose_inputs", lambda *a: ())

    def refuse(*args: Any, **kwargs: Any) -> tuple[str, ...]:
        raise policy.PodmanMaskError("synthetic gate refusal")

    monkeypatch.setattr(launch, "require_podman_mask_compatibility", refuse)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("unchecked shell launch"))
    with pytest.raises(policy.PodmanMaskError):
        support["controlled_compose"](["-f", "compose.yaml", "up"])


def test_identity_refusal_does_not_authorize_later_admission(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    policy.require_podman_mask_compatibility()
    fake.provider_version = "unavailable"
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    fake.provider_version = "2.39.4"
    policy.require_podman_mask_compatibility()
    assert len(fake.created) == 8


@pytest.mark.parametrize("failure", ["output", "deadline"])
def test_real_bounded_runner_stops_excess_output_and_deadline(failure: str) -> None:
    import sys

    policy = policy_module()
    program = "import os; os.write(1, b'x' * (4 * 1024 * 1024 + 1))"
    if failure == "deadline":
        program = "import time; time.sleep(10)"
    expected = policy.PodmanMaskError if failure == "output" else subprocess.TimeoutExpired
    with pytest.raises(expected):
        policy._run([sys.executable, "-c", program], os.environ, timeout=1)


def test_runtime_canonical_render_checks_effective_merge_before_gate(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    _, fake = mask_engine
    launch = launch_module()
    base = Path(__file__).resolve().parents[4] / "compose.yaml"
    overlay = yaml.safe_load(base.with_name("compose.podman.yaml").read_text())["services"]
    services = {
        "web": {"security_opt": ["no-new-privileges:true"], "profiles": []},
        "caddy-local": {"security_opt": ["no-new-privileges:true"], "profiles": ["tls-local"]},
    }
    renders: list[list[str]] = []

    def render(
        command: list[str], environment: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        del environment
        renders.append(command)
        merged = deepcopy(services)
        if str(base.with_name("compose.podman.yaml")) in command:
            for name, service in merged.items():
                service.update(overlay[name])
        return subprocess.CompletedProcess(command, 0, json.dumps({"services": merged}), "")

    monkeypatch.setattr(launch, "_run", render)
    result = launch.controlled_container_argv(
        ["podman", "--remote=false", "compose", "-f", str(base), "--profile", "tls-local", "up"],
        canonical_base=base,
    )
    assert result[-3:] == ["-f", str(base.with_name("compose.podman.yaml")), "up"]
    assert len(renders) == 2 and len(fake.created) == 4


@pytest.mark.parametrize("drift", ["phantom", "nnp", "mask", "profiles"])
def test_runtime_compose_refuses_effective_drift_before_gate(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = Path(__file__).resolve().parents[4] / "compose.yaml"

    def render(
        command: list[str], environment: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        del environment
        services: dict[str, Any] = {
            "web": {"security_opt": ["no-new-privileges:true"], "profiles": []}
        }
        if str(base.with_name("compose.podman.yaml")) in command:
            services["web"]["security_opt"].append(f"unmask={POWERCAP}")
            if drift == "phantom":
                services["phantom"] = {}
            elif drift == "nnp":
                services["web"]["security_opt"].remove("no-new-privileges:true")
            elif drift == "mask":
                services["web"]["security_opt"].append("unmask=ALL")
            else:
                services["web"]["profiles"] = ["other"]
        return subprocess.CompletedProcess(command, 0, json.dumps({"services": services}), "")

    monkeypatch.setattr(launch, "_run", render)
    with pytest.raises(policy.PodmanMaskError):
        launch.controlled_container_argv(
            ["podman", "--remote=false", "compose", "-f", str(base), "up"],
            canonical_base=base,
        )
    assert not fake.created


def test_failed_create_cannot_adopt_or_delete_wrong_image(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, fake = mask_engine
    original_run = fake.run

    def replaced(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = original_run(command, *args, **kwargs)
        if command[2:3] == ["create"]:
            next(iter(fake.resources.values()))["Image"] = "b" * 64
            return subprocess.CompletedProcess(command, 125, "", "creation uncertain")
        return result

    monkeypatch.setattr(policy, "_run", replaced)
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert fake.resources and not fake.removed


def test_unknown_diagnostic_file_blocks_all_cleanup(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, fake = mask_engine
    original_run = fake.run

    def mutate(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = original_run(command, *args, **kwargs)
        if command[2:3] == ["init"]:
            report = next(fake.root.glob("aegis-mask-check-*/evidence.jsonl"))
            report.with_name("unknown").write_text("preserve")
        return result

    monkeypatch.setattr(policy, "_run", mutate)
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.removed and fake.resources


@pytest.mark.parametrize("failure", ["status", "timeout", "interrupt"])
def test_create_failure_recovers_exact_container_before_propagation(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    policy, fake = mask_engine
    original_run = fake.run

    def fail(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = original_run(command, *args, **kwargs)
        if command[2:3] == ["create"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired("synthetic", 30)
            if failure == "interrupt":
                raise SystemExit(143)
            return subprocess.CompletedProcess(command, 125, "", "creation uncertain")
        return result

    monkeypatch.setattr(policy, "_run", fail)
    with pytest.raises(SystemExit if failure == "interrupt" else policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()
    assert not fake.resources and len(fake.removed) == 1 and not fake.started


def test_canonical_merge_cannot_remove_existing_security_option(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = Path(__file__).resolve().parents[4] / "compose.yaml"

    def render(options: Any, environment: Any, directory: Any = None) -> dict[str, Any]:
        del environment
        security = ["no-new-privileges:true", "seccomp=fixture.json"]
        if str(base.with_name("compose.podman.yaml")) in options:
            security = ["no-new-privileges:true", f"unmask={POWERCAP}"]
        return {"services": {"web": {"security_opt": security}}}

    monkeypatch.setattr(launch, "_render", render)
    with pytest.raises(policy.PodmanMaskError):
        launch.controlled_container_argv(
            ["podman", "--remote=false", "compose", "-f", str(base), "up"],
            canonical_base=base,
        )
    assert not fake.created


def test_cleanup_remains_available_after_socket_identity_is_lost(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    _, fake = mask_engine
    launch = launch_module()
    Path(os.environ["AEGIS_PODMAN_SOCKET"]).unlink()
    command = ["podman", "--remote=false", "rm", "--force", "owned-id"]
    assert launch.controlled_container_argv(command) == command
    assert not fake.commands


def test_compose_oneoff_bind_is_included_in_probe_output_boundary(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, fake = mask_engine
    launch = launch_module()
    base = Path(__file__).resolve().parents[4] / "compose.yaml"
    original = fake.root / "oneoff-original"
    original.mkdir()
    boundaries: list[tuple[Path, ...]] = []
    monkeypatch.setattr(launch, "_checked_compose_inputs", lambda *a: ())

    def check(*args: Any, originals: tuple[Path, ...]) -> tuple[str, ...]:
        boundaries.append(originals)
        return (f"unmask={POWERCAP}",)

    monkeypatch.setattr(launch, "require_podman_mask_compatibility", check)
    launch.controlled_container_argv(
        [
            "podman",
            "--remote=false",
            "compose",
            "-f",
            str(base),
            "run",
            "--rm",
            "--no-deps",
            "--volume",
            f"{original}:/original:ro",
            "web",
            "python",
        ],
        canonical_base=base,
    )
    assert boundaries == [(original,)]


def test_observer_preserves_original_boundary_error_before_probe(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegisctl import mounts

    _, fake = mask_engine
    slot = mounts.ValidatedSlot(
        "photos", fake.root, "/srv/aegis/roots/photos", "read_only", 1, 2, "local:1:2"
    )
    boundary = mounts.ConfigError("nested mount forbidden")

    def reject(*args: Any, **kwargs: Any) -> None:
        raise boundary

    monkeypatch.setattr(mounts, "ensure_outputs_outside_originals", reject)
    monkeypatch.setattr(
        mounts,
        "require_podman_mask_compatibility",
        lambda *a, **k: pytest.fail("probe preceded original-boundary validation"),
    )
    with pytest.raises(mounts.ConfigError) as caught:
        mounts.observe_mount_fingerprints((slot,))
    assert caught.value is boundary


def test_probe_resource_queries_and_cleanup_use_bounded_runner(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, fake = mask_engine
    monkeypatch.setattr(
        "aegisctl.container_resources._run",
        lambda *a, **k: pytest.fail("probe used unbounded resource runner"),
    )
    policy.require_podman_mask_compatibility()
    assert len(fake.removed) == 4


def test_native_executable_identity_uses_launch_environment_path(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, fake = mask_engine
    searched: list[str | None] = []

    def executable(name: str, *, path: str | None = None) -> str:
        searched.append(path)
        return str(fake.root / name)

    monkeypatch.setattr(policy.shutil, "which", executable)
    policy.require_podman_mask_compatibility(os.environ | {"PATH": "/synthetic/path"})
    assert searched == ["/synthetic/path", "/synthetic/path"]


def test_unix_api_accepts_bounded_response_that_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = policy_module()

    class Response:
        status = 200
        closed = False

        def read1(self, size: int) -> bytes:
            assert size <= 65536
            self.closed = True
            return b'{"Version":"5.8.7"}'

        def isclosed(self) -> bool:
            return self.closed

    response = Response()

    class Transport:
        def settimeout(self, remaining: float) -> None:
            assert 0 < remaining <= 10
            if response.closed:
                raise OSError("connection closed after complete body")

    class Connection:
        sock = Transport()

        def request(self, method: str, endpoint: str) -> None:
            assert (method, endpoint) == ("GET", "/version")

        def getresponse(self) -> Response:
            return response

        def close(self) -> None:
            pass

    monkeypatch.setattr(policy, "_UnixConnection", lambda path: Connection())
    assert policy._api_json(Path("/synthetic/socket"), "/version") == {"Version": "5.8.7"}


def test_relative_generic_compose_in_another_working_directory_gets_no_canonical_services(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    _, fake = mask_engine
    launch = launch_module()
    base = Path(__file__).resolve().parents[4] / "compose.yaml"
    command = ["podman", "--remote=false", "compose", "-f", "compose.yaml", "config"]
    assert (
        launch.controlled_container_argv(
            command,
            canonical_base=base,
            working_directory=fake.root,
        )
        == command
    )
    assert not fake.commands


def test_successive_admission_rechecks_behavior_when_reported_identity_is_unchanged(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    identity_before = deepcopy(fake.info)
    policy.require_podman_mask_compatibility()
    assert len(fake.created) == len(fake.removed) == 4

    # Simulate a changed effective mask configuration invisible to info/version.
    fake.bad_oci = True
    with pytest.raises(policy.PodmanMaskError):
        policy.require_podman_mask_compatibility()

    assert fake.info == identity_before
    assert len(fake.created) == len(fake.removed) == 6
    assert len(fake.started) == 5  # Refused before the new candidate payload.
    assert not fake.resources


@pytest.mark.parametrize("spelling", ["relative", "absolute", "equals", "short-equals"])
def test_canonical_overlay_spellings_all_render_and_gate(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch, spelling: str
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = fake.root / "compose.yaml"
    override = base.with_name("compose.podman.yaml")
    arguments = {
        "relative": ["-f", override.name],
        "absolute": ["-f", str(override)],
        "equals": [f"--file={override}"],
        "short-equals": [f"-f={override}"],
    }[spelling]
    renders: list[list[str]] = []

    def render(options: list[str], *args: Any) -> dict[str, Any]:
        renders.append(options)
        opts = ["no-new-privileges:true"]
        if str(override) in options:
            opts.append(policy.MASK_OPTION)
        return {"services": {"web": {"security_opt": opts}}}

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise policy.PodmanMaskError("regression gate refusal")

    monkeypatch.setattr(launch, "_render", render)
    monkeypatch.setattr(launch, "require_podman_mask_compatibility", refuse)
    with pytest.raises(policy.PodmanMaskError, match="regression gate refusal"):
        launch.controlled_container_argv(
            ["podman", "--remote=false", "compose", "--file", base.name, *arguments, "up"],
            canonical_base=base,
            working_directory=fake.root,
        )
    assert renders == [["-f", str(base)], ["-f", str(base), "-f", str(override)]]
    assert not fake.commands


@pytest.mark.parametrize("duplicate", ["base", "overlay"])
def test_repeated_canonical_files_refuse_before_render(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch, duplicate: str
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = fake.root / "compose.yaml"
    overlay = base.with_name("compose.podman.yaml")
    repeated = base if duplicate == "base" else overlay
    monkeypatch.setattr(launch, "_render", lambda *a: pytest.fail("ambiguous render"))
    with pytest.raises(policy.PodmanMaskError, match="duplicate Compose file"):
        launch.controlled_container_argv(
            [
                "podman",
                "--remote=false",
                "compose",
                "-f",
                str(base),
                "-f",
                str(overlay),
                f"--file={repeated}",
                "up",
            ],
            canonical_base=base,
        )
    assert not fake.commands


def test_native_relative_bind_uses_launch_directory_before_diagnostics(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    controller, worker = fake.root / "controller", fake.root / "worker"
    for directory in (controller, worker):
        (directory / "original").mkdir(parents=True)
    original = worker / "original"
    monkeypatch.chdir(controller)
    monkeypatch.setenv("TMPDIR", str(original))
    monkeypatch.setattr("aegisctl.mounts._host_mountinfo", lambda: None)
    monkeypatch.setattr("aegisctl.mounts._host_mountpoints", lambda records: ())
    with pytest.raises(policy.PodmanMaskError) as caught:
        launch.controlled_container_argv(
            ["podman", "--remote=false", "run", "-v", "./original:/original:ro", "image"],
            working_directory=worker,
        )
    assert str(caught.value.__cause__) == "generated artifacts must be outside original roots"
    assert not list(original.iterdir()) and not fake.commands


def test_compose_relative_oneoff_bind_refuses_before_render_or_probe(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = fake.root / "compose.yaml"
    monkeypatch.setattr(launch, "_render", lambda *a: pytest.fail("ambiguous bind rendered"))
    with pytest.raises(policy.PodmanMaskError, match="relative Compose bind"):
        launch.controlled_container_argv(
            [
                "podman",
                "--remote=false",
                "compose",
                "-f",
                str(base),
                "run",
                "-v",
                "./original:/original:ro",
                "web",
            ],
            canonical_base=base,
        )
    assert not fake.commands


@pytest.mark.parametrize("option", ["--privileged", "--privileged=true", "--privileged=false"])
@pytest.mark.parametrize("compose", [False, True])
def test_privileged_option_refuses_before_render_or_probe(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    compose: bool,
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = fake.root / "compose.yaml"
    monkeypatch.setattr(launch, "_render", lambda *a: pytest.fail("privileged rendered"))
    args = ["compose", "-f", str(base), "run"] if compose else ["run"]
    with pytest.raises(policy.PodmanMaskError, match="privileged"):
        launch.controlled_container_argv(
            ["podman", "--remote=false", *args, option, "web"],
            canonical_base=base,
        )
    assert not fake.commands


def test_effective_privileged_service_refuses_before_probe(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = fake.root / "compose.yaml"

    def render(options: list[str], *args: Any) -> dict[str, Any]:
        security = ["no-new-privileges:true"]
        if str(base.with_name("compose.podman.yaml")) in options:
            security.append(policy.MASK_OPTION)
        return {"services": {"web": {"security_opt": security, "privileged": True}}}

    monkeypatch.setattr(launch, "_render", render)
    with pytest.raises(policy.PodmanMaskError, match="privileged"):
        launch.controlled_container_argv(
            ["podman", "--remote=false", "compose", "-f", str(base), "up"],
            canonical_base=base,
        )
    assert not fake.commands


@pytest.mark.parametrize("compose", [False, True])
def test_privileged_and_relative_bind_application_arguments_remain_opaque(
    mask_engine: tuple[Any, MaskEngine], monkeypatch: pytest.MonkeyPatch, compose: bool
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = fake.root / "compose.yaml"
    monkeypatch.setattr(launch, "_checked_compose_inputs", lambda *a: ())
    monkeypatch.setattr(
        launch, "require_podman_mask_compatibility", lambda *a, **k: (policy.MASK_OPTION,)
    )
    args = ["compose", "-f", str(base), "run"] if compose else ["run"]
    opaque = ["web", "--privileged", "--privileged=true", "-v", "./original:/original:ro"]
    result = launch.controlled_container_argv(
        ["podman", "--remote=false", *args, *opaque],
        canonical_base=base,
    )
    assert result[-len(opaque) :] == opaque and not fake.commands


def test_docker_privileged_options_remain_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    command = ["docker", "run", "--privileged", "--privileged=true", "image"]
    assert launch_module().controlled_container_argv(command) == command


@pytest.mark.parametrize("stage", ["create", "attached", "spawn"])
@pytest.mark.parametrize("refuse_cleanup", [False, True])
def test_real_sigterm_recovers_exact_probe_and_preserves_interruption(
    tmp_path: Path,
    stage: str,
    refuse_cleanup: bool,
) -> None:
    import signal
    import sys

    # Entire engine/API boundary is synthetic. The only subprocesses are Python sleepers.
    program = (
        r"""
import json, os, signal, sys, time
from pathlib import Path
import pytest
from backend.tests.unit.aegisctl.test_podman_mask_compatibility import MaskEngine
from tests.support.fake_container_engine import select_fake_engine
from aegisctl import podman_mask_compatibility as policy
root = Path(sys.argv[1])
stage, refuse = sys.argv[2], sys.argv[3] == "True"
patch = pytest.MonkeyPatch()
select_fake_engine("podman", root, patch)
fake = MaskEngine(root)
real_run = policy._run
patch.setattr(policy, "_api_json", fake.api)
patch.setattr(policy, "_selinux_enforcing", lambda: True)
patch.setattr(policy.shutil, "which", lambda name, **kwargs: str(root / name))
patch.setenv("TMPDIR", str(root))
triggered = False
previous = signal.getsignal(signal.SIGTERM)
def run(command, environment=None, **kwargs):
    global triggered
    result = fake.run(command, environment, **kwargs)
    target = command[2:3] == (["start"] if stage == "attached" else ["create"])
    if target and not triggered:
        triggered = True
        if refuse:
            # A foreign diagnostic blocks ALL removals, preserving the primary signal.
            next(root.glob("aegis-mask-check-*" )).joinpath("unknown").write_text("retain")
        code = """
        + 'r"""'
        + r"""
import json, os, signal, subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"])
Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))
if sys.argv[3] != "spawn":
    os.kill(int(sys.argv[2]), signal.SIGTERM)
time.sleep(8)
"""
        + '"""'
        + r"""
        args = [sys.executable, "-c", code, str(root / "pids.json"), str(os.getpid()), stage]
        return real_run(args, os.environ)
    return result
patch.setattr(policy, "_run", run)
if stage == "spawn":
    original_popen = policy.subprocess.Popen
    def spawning(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        deadline = time.monotonic() + 3
        while not root.joinpath("pids.json").exists():
            assert time.monotonic() < deadline
            time.sleep(0.005)
        os.kill(os.getpid(), signal.SIGTERM)
        return process
    patch.setattr(policy.subprocess, "Popen", spawning)
try:
    policy.require_podman_mask_compatibility()
except BaseException as exc:
    root.joinpath("result.json").write_text(json.dumps({
        "type": type(exc).__name__, "code": getattr(exc, "code", None),
        "removed": fake.removed, "remaining": list(fake.resources),
        "cause": str(exc.__cause__), "restored": signal.getsignal(signal.SIGTERM) == previous,
    }))
    raise
"""
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path), stage, str(refuse_cleanup)],
        env=os.environ | {"PYTHONPATH": f"{Path.cwd() / 'backend'}:{Path.cwd()}"},
        capture_output=True,
        text=True,
        timeout=12,
    )
    pids = json.loads((tmp_path / "pids.json").read_text())
    try:
        assert result.returncode == 143, result.stderr
        evidence = json.loads((tmp_path / "result.json").read_text())
        assert evidence["type"] == "SystemExit" and evidence["code"] == 143
        assert evidence["restored"] is True
        exact_id = f"{1:064x}"
        assert evidence["removed"] == ([] if refuse_cleanup else [exact_id])
        assert evidence["remaining"] == ([exact_id] if refuse_cleanup else [])
        if refuse_cleanup:
            assert "diagnostic" in evidence["cause"]
        for pid in pids:
            status = Path(f"/proc/{pid}/stat")
            assert not status.exists() or status.read_text().split()[2] == "Z"
    finally:
        # Dispose only this test's recorded local Python process group even on RED.
        with suppress(ProcessLookupError):
            os.killpg(pids[0], signal.SIGKILL)


def test_signal_boundary_refuses_nonmain_thread_before_diagnostics(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    policy, fake = mask_engine
    monkeypatch.setattr(policy, "_Diagnostics", lambda *a: pytest.fail("thread wrote diagnostics"))
    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        pytest.raises(policy.PodmanMaskError, match="main thread"),
    ):
        pool.submit(policy.require_podman_mask_compatibility).result()
    assert not fake.commands


@pytest.mark.parametrize("disposition", ["handler", "ignore"])
def test_signal_boundary_refuses_caller_disposition_without_replacing_it(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    disposition: str,
) -> None:
    import signal

    policy, fake = mask_engine
    previous = signal.getsignal(signal.SIGTERM)
    caller = (lambda *a: None) if disposition == "handler" else signal.SIG_IGN
    signal.signal(signal.SIGTERM, caller)
    monkeypatch.setattr(policy, "_Diagnostics", lambda *a: pytest.fail("unsafe signal setup wrote"))
    try:
        with pytest.raises(policy.PodmanMaskError, match="SIGTERM disposition"):
            policy.require_podman_mask_compatibility()
        assert signal.getsignal(signal.SIGTERM) == caller
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert not fake.commands


@pytest.mark.parametrize("phase", ["request", "headers", "body"])
def test_api_deadline_interrupts_continuously_progressing_transport(
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    policy = policy_module()
    now = 0.0
    timer: Any = None
    reads = 0
    stopped = False
    closed = False

    class Timer:
        def __init__(self, interval: float, callback: Any) -> None:
            nonlocal timer
            assert interval == 10
            self.callback, self.deadline = callback, now + interval
            self.started = self.cancelled = self.joined = False
            timer = self

        def start(self) -> None:
            self.started = True

        def cancel(self) -> None:
            self.cancelled = True

        def join(self) -> None:
            self.joined = True

    class Transport:
        def settimeout(self, value: float) -> None:
            assert value > 0

        def shutdown(self, how: int) -> None:
            nonlocal stopped
            assert how == policy.socket.SHUT_RDWR
            stopped = True

    def progress() -> None:
        nonlocal now, reads
        # Every fragment arrives inside the inactivity timeout, but total time exceeds ten.
        now += 0.6
        reads += 1
        if timer is not None and timer.started and now >= timer.deadline:
            timer.callback()
        if stopped:
            raise OSError("local transport shut down at deadline")

    class Response:
        status = 200

        def read1(self, size: int) -> bytes:
            if phase == "body":
                progress()
                return b" " if reads < 20 else b"{}"
            return b"{}"

        def isclosed(self) -> bool:
            return phase != "body" or reads >= 20

    class Connection:
        sock = Transport()

        def request(self, *args: Any) -> None:
            if phase == "request":
                for _ in range(20):
                    progress()

        def getresponse(self) -> Response:
            if phase == "headers":
                for _ in range(20):
                    progress()
            return Response()

        def close(self) -> None:
            nonlocal closed
            closed = True

    monkeypatch.setattr(policy.time, "monotonic", lambda: now)
    monkeypatch.setattr(policy.threading, "Timer", Timer)
    monkeypatch.setattr(policy, "_UnixConnection", lambda path: Connection())
    with pytest.raises((OSError, policy.PodmanMaskError)):
        policy._api_json(Path("/synthetic/socket"), "/version")
    assert stopped and reads == 17 and now < 10.3
    assert closed and timer.cancelled and timer.joined


def test_overlay_removal_uses_parsed_span_with_following_global_options(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, fake = mask_engine
    launch = launch_module()
    base = fake.root / "compose.yaml"
    override = base.with_name("compose.podman.yaml")
    options = ["-f", str(base), f"--file={override}", "--profile", "index"]
    rendered: list[list[str]] = []

    def render(opts: list[str], *args: Any) -> dict[str, Any]:
        rendered.append(opts)
        security = ["no-new-privileges:true"]
        if str(override) in opts:
            security.append(policy.MASK_OPTION)
        return {"services": {"web": {"security_opt": security}}}

    monkeypatch.setattr(launch, "_render", render)
    monkeypatch.setattr(
        launch, "require_podman_mask_compatibility", lambda *a, **k: (policy.MASK_OPTION,)
    )
    launch.controlled_container_argv(
        ["podman", "--remote=false", "compose", *options, "up"],
        canonical_base=base,
    )
    assert rendered == [
        ["-f", str(base), "--profile", "index"],
        ["-f", str(base), "-f", str(override), "--profile", "index"],
    ]
    assert not fake.commands


def test_nonfile_option_value_cannot_select_canonical_policy(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, fake = mask_engine
    launch = launch_module()
    base = fake.root / "compose.yaml"
    command = [
        "podman",
        "--remote=false",
        "compose",
        "-f",
        "generic.json",
        "--env-file",
        str(base.with_name("compose.podman.yaml")),
        "up",
    ]
    monkeypatch.setattr(launch, "_render", lambda *a: pytest.fail("generic render"))
    assert launch.controlled_container_argv(command, canonical_base=base) == command
    assert not fake.commands


@pytest.mark.parametrize("action", ["config", "build", "run"])
def test_generic_stdin_compose_keeps_its_existing_behavior(
    mask_engine: tuple[Any, MaskEngine],
    action: str,
) -> None:
    _, fake = mask_engine
    command = ["podman", "--remote=false", "compose", "-f", "-", action]
    assert (
        launch_module().controlled_container_argv(
            command,
            canonical_base=fake.root / "compose.yaml",
        )
        == command
    )
    assert not fake.commands


def test_canonical_stdin_overlay_refuses_ambiguous_identity(
    mask_engine: tuple[Any, MaskEngine],
) -> None:
    policy, fake = mask_engine
    base = fake.root / "compose.yaml"
    with pytest.raises(policy.PodmanMaskError, match="ambiguous Compose file"):
        launch_module().controlled_container_argv(
            ["podman", "--remote=false", "compose", "-f", str(base), "-f", "-", "up"],
            canonical_base=base,
        )
    assert not fake.commands


def test_signal_boundary_refuses_blocked_sigterm_before_diagnostics(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    policy, fake = mask_engine
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    monkeypatch.setattr(policy, "_Diagnostics", lambda *a: pytest.fail("blocked TERM wrote"))
    try:
        with pytest.raises(policy.PodmanMaskError, match="unblocked SIGTERM"):
            policy.require_podman_mask_compatibility()
        assert signal.SIGTERM in signal.pthread_sigmask(signal.SIG_BLOCK, set())
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    assert not fake.commands


def test_actual_sigterm_at_handler_install_restores_caller_before_body() -> None:
    import signal
    import sys

    policy = policy_module()
    previous = signal.getsignal(signal.SIGTERM)
    prior_trace = sys.gettrace()
    body_entered = False
    delivered = False
    code = policy._termination_boundary.__wrapped__.__code__

    def trace(frame: Any, event: str, argument: Any) -> Any:
        nonlocal delivered
        if (
            frame.f_code is code
            and event == "line"
            and not delivered
            and signal.getsignal(signal.SIGTERM) is not previous
        ):
            delivered = True
            os.kill(os.getpid(), signal.SIGTERM)
        return trace

    try:
        sys.settrace(trace)
        with pytest.raises(SystemExit) as caught, policy._termination_boundary():
            body_entered = True
        assert caught.value.code == 143 and delivered and not body_entered
        assert signal.getsignal(signal.SIGTERM) is previous
    finally:
        sys.settrace(prior_trace)
        signal.signal(signal.SIGTERM, previous)


@pytest.mark.parametrize("stage", ["before-start", "after-start", "after-return"])
@pytest.mark.parametrize("cleanup_failure", [None, "cancel", "join", "close"])
def test_actual_sigterm_at_timer_start_cleans_lifetime_and_preserves_interruption(
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    cleanup_failure: str | None,
) -> None:
    import signal
    import sys
    import threading

    policy = policy_module()
    original_timer = threading.Timer
    previous = signal.getsignal(signal.SIGTERM)
    prior_trace = sys.gettrace()
    events: list[str] = []
    timers: list[Any] = []
    delivered = False

    def interrupt() -> None:
        nonlocal delivered
        delivered = True
        os.kill(os.getpid(), signal.SIGTERM)

    class Timer(threading.Timer):
        def __init__(self, interval: float, callback: Any) -> None:
            super().__init__(interval, callback)
            timers.append(self)

        def start(self) -> None:
            if stage == "before-start":
                interrupt()
            super().start()
            if stage == "after-start":
                interrupt()

        def cancel(self) -> None:
            events.append("cancel")
            super().cancel()
            if cleanup_failure == "cancel":
                raise RuntimeError("cancel refused")

        def join(self, timeout: float | None = None) -> None:
            events.append("join")
            super().join(timeout)
            if cleanup_failure == "join":
                raise RuntimeError("join refused")

    class Connection:
        sock = None

        def request(self, *args: Any) -> None:
            pytest.fail("interrupted setup reached API request")

        def close(self) -> None:
            events.append("close")
            if cleanup_failure == "close":
                raise RuntimeError("close refused")

    def trace(frame: Any, event: str, argument: Any) -> Any:
        if (
            stage == "after-return"
            and not delivered
            and event == "line"
            and frame.f_code is policy._api_json.__code__
            and timers
            and timers[0].is_alive()
        ):
            interrupt()
        return trace

    monkeypatch.setattr(policy.threading, "Timer", Timer)
    monkeypatch.setattr(policy, "_UnixConnection", lambda path: Connection())
    try:
        sys.settrace(trace)
        with pytest.raises(SystemExit) as caught, policy._termination_boundary():
            policy._api_json(Path("/synthetic/socket"), "/version")
        assert delivered and caught.value.code == 143
        assert signal.getsignal(signal.SIGTERM) is previous
        assert events == ["cancel", "join", "close"]
        assert not timers[0].is_alive() and timers[0].finished.is_set()
        if cleanup_failure is not None:
            assert str(caught.value.__cause__) == f"{cleanup_failure} refused"
    finally:
        sys.settrace(prior_trace)
        signal.signal(signal.SIGTERM, previous)
        for timer in timers:
            original_timer.cancel(timer)
            if timer.ident is not None:
                original_timer.join(timer)


@pytest.mark.parametrize("started", [False, True])
def test_timer_partial_start_failure_closes_connection_and_joins_if_started(
    monkeypatch: pytest.MonkeyPatch,
    started: bool,
) -> None:
    import threading

    policy = policy_module()
    original_timer = threading.Timer
    timers: list[Any] = []
    events: list[str] = []
    failure = RuntimeError("thread startup refused")

    class Timer(threading.Timer):
        def __init__(self, interval: float, callback: Any) -> None:
            super().__init__(interval, callback)
            timers.append(self)

        def start(self) -> None:
            if started:
                super().start()
            raise failure

        def cancel(self) -> None:
            events.append("cancel")
            super().cancel()

        def join(self, timeout: float | None = None) -> None:
            events.append("join")
            super().join(timeout)

    class Connection:
        sock = None

        def close(self) -> None:
            events.append("close")

    monkeypatch.setattr(policy.threading, "Timer", Timer)
    monkeypatch.setattr(policy, "_UnixConnection", lambda path: Connection())
    try:
        with pytest.raises(RuntimeError) as caught:
            policy._api_json(Path("/synthetic/socket"), "/version")
        assert caught.value is failure
        assert events == (["cancel", "join", "close"] if started else ["cancel", "close"])
        assert not timers[0].is_alive() and timers[0].finished.is_set()
    finally:
        for timer in timers:
            original_timer.cancel(timer)
            if timer.ident is not None:
                original_timer.join(timer)


def test_signal_restoration_failure_preserves_actual_interruption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal

    policy = policy_module()
    previous = signal.getsignal(signal.SIGTERM)
    install = signal.signal

    def refuse_after_restore(number: int, handler: Any) -> Any:
        result = install(number, handler)
        if handler is previous:
            raise RuntimeError("restoration refused")
        return result

    monkeypatch.setattr(signal, "signal", refuse_after_restore)
    try:
        with pytest.raises(SystemExit) as caught, policy._termination_boundary():
            os.kill(os.getpid(), signal.SIGTERM)
        assert caught.value.code == 143
        assert str(caught.value.__cause__) == "restoration refused"
        assert signal.getsignal(signal.SIGTERM) is previous
    finally:
        install(signal.SIGTERM, previous)


def test_measured_null_inspect_caps_require_zero_oci_and_kernel_evidence(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, fake = mask_engine

    def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = fake.run(command, *args, **kwargs)
        if command[2:3] in (["create"], ["compose"]):
            for item in fake.resources.values():
                item["EffectiveCaps"] = item["BoundingCaps"] = None
        return result

    monkeypatch.setattr(policy, "_run", run)
    assert policy.require_podman_mask_compatibility() == (policy.MASK_OPTION,)
    assert len(fake.created) == len(fake.started) == len(fake.removed) == 4
    assert not fake.resources
    records = [
        json.loads(line)
        for line in next(fake.root.glob("aegis-mask-check-*/evidence.jsonl"))
        .read_text()
        .splitlines()
    ]
    initialized = [item for item in records if item["event"] == "initialized-oci"]
    assert len(initialized) == 4
    for item in initialized:
        assert item["capabilities"] == {
            "bounding": [],
            "effective": [],
            "inheritable": [],
            "permitted": [],
            "ambient": [],
        }


@pytest.mark.parametrize("field", ["EffectiveCaps", "BoundingCaps"])
@pytest.mark.parametrize("value", ["missing", ["CAP_NET_ADMIN"], False, 0, "", {}])
def test_inspect_capability_fields_must_be_present_null_or_empty_list(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    policy, fake = mask_engine

    def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = fake.run(command, *args, **kwargs)
        if command[2:3] == ["create"]:
            item = next(iter(fake.resources.values()))
            if value == "missing":
                del item[field]
            else:
                item[field] = value
        return result

    monkeypatch.setattr(policy, "_run", run)
    with pytest.raises(policy.PodmanMaskError) as caught:
        policy.require_podman_mask_compatibility()
    assert isinstance(caught.value.__cause__, policy.PodmanMaskError)
    assert "capability" in str(caught.value.__cause__)
    assert not fake.started and len(fake.removed) == 1 and not fake.resources


@pytest.mark.parametrize("phase", ["initialized", "final"])
@pytest.mark.parametrize("field", ["bounding", "effective", "inheritable", "permitted", "ambient"])
@pytest.mark.parametrize("value", [["CAP_NET_ADMIN"], None, False, "", {}])
def test_oci_capability_set_must_be_an_empty_array(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    field: str,
    value: Any,
) -> None:
    policy, fake = mask_engine

    def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = fake.run(command, *args, **kwargs)
        if command[2:3] == (["init"] if phase == "initialized" else ["start"]):
            item = next(iter(fake.resources.values()))
            path = Path(item["OCIConfigPath"])
            spec = json.loads(path.read_text())
            spec["process"]["capabilities"][field] = value
            path.write_text(json.dumps(spec))
        return result

    monkeypatch.setattr(policy, "_run", run)
    with pytest.raises(policy.PodmanMaskError) as caught:
        policy.require_podman_mask_compatibility()
    assert "capability" in str(caught.value.__cause__)
    assert len(fake.started) == (0 if phase == "initialized" else 1)
    assert len(fake.removed) == 1 and not fake.resources


@pytest.mark.parametrize("value", ["missing", None, [], False, "", {"unknown": []}])
def test_oci_capability_object_is_required_before_payload(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    value: Any,
) -> None:
    policy, fake = mask_engine

    def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = fake.run(command, *args, **kwargs)
        if command[2:3] == ["init"]:
            path = Path(next(iter(fake.resources.values()))["OCIConfigPath"])
            spec = json.loads(path.read_text())
            if value == "missing":
                del spec["process"]["capabilities"]
            else:
                spec["process"]["capabilities"] = value
            path.write_text(json.dumps(spec))
        return result

    monkeypatch.setattr(policy, "_run", run)
    with pytest.raises(policy.PodmanMaskError) as caught:
        policy.require_podman_mask_compatibility()
    assert "capability" in str(caught.value.__cause__)
    assert not fake.started and len(fake.removed) == 1 and not fake.resources


@pytest.mark.parametrize("field", ["CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"])
@pytest.mark.parametrize("value", ["missing", "0000000000000001", "zero", "0", 0, None, []])
def test_host_requires_every_runtime_capability_bitmap_to_be_exact_zero(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: Any,
) -> None:
    policy, fake = mask_engine

    def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = fake.run(command, *args, **kwargs)
        if command[2:3] == ["start"]:
            evidence = json.loads(result.stdout)
            if value == "missing":
                del evidence["capabilities"][field]
            else:
                evidence["capabilities"][field] = value
            result.stdout = json.dumps(evidence)
        return result

    monkeypatch.setattr(policy, "_run", run)
    with pytest.raises(policy.PodmanMaskError) as caught:
        policy.require_podman_mask_compatibility()
    assert "capability" in str(caught.value.__cause__)
    assert len(fake.started) == len(fake.removed) == 1 and not fake.resources


@pytest.mark.parametrize(
    "omitted", ["bounding", "effective", "inheritable", "permitted", "ambient", "all"]
)
def test_present_oci_capability_object_allows_omitempty_empty_members(
    mask_engine: tuple[Any, MaskEngine],
    monkeypatch: pytest.MonkeyPatch,
    omitted: str,
) -> None:
    policy, fake = mask_engine

    def run(command: list[str], *args: Any, **kwargs: Any) -> Any:
        result = fake.run(command, *args, **kwargs)
        if command[2:3] == ["init"]:
            path = Path(next(iter(fake.resources.values()))["OCIConfigPath"])
            spec = json.loads(path.read_text())
            if omitted == "all":
                spec["process"]["capabilities"] = {}
            else:
                del spec["process"]["capabilities"][omitted]
            path.write_text(json.dumps(spec))
        return result

    monkeypatch.setattr(policy, "_run", run)
    assert policy.require_podman_mask_compatibility() == (policy.MASK_OPTION,)
    assert len(fake.started) == len(fake.removed) == 4 and not fake.resources


@pytest.mark.parametrize("field", ["CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"])
@pytest.mark.parametrize(
    "mutation", ["nonzero", "malformed", "missing", "duplicate", "oversized", "status-size"]
)
def test_probe_payload_rejects_nonzero_or_unproved_kernel_capability_bitmaps(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    mutation: str,
) -> None:
    import builtins
    import io

    policy = policy_module()
    rows = {
        name: f"{name}:\t0000000000000000"
        for name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
    }
    if mutation == "missing":
        del rows[field]
    elif mutation == "duplicate":
        rows[field] += "\n" + rows[field]
    elif mutation == "oversized":
        rows[field] = f"{field}:\t" + "0" * 17
    elif mutation == "status-size":
        rows[field] += "\nName:\t" + "x" * 65536
    else:
        rows[field] = f"{field}:\t" + ("0000000000000001" if mutation == "nonzero" else "invalid")
    status = ("Name:\tpython\n" + "\n".join(rows.values()) + "\n").encode()

    def read_fixture(path: str, mode: str) -> Any:
        assert mode == "rb"
        if path == "/proc/self/status":
            return io.BytesIO(status)
        assert path == "/proc/self/mountinfo"
        return io.BytesIO((ROOT_ROW + "\n" + MASK_ROW + "\n").encode())

    class EmptyDirectory:
        def __enter__(self) -> Any:
            return iter(())

        def __exit__(self, *args: Any) -> None:
            pass

    monkeypatch.setattr(os, "getuid", lambda: 10001)
    monkeypatch.setattr(os, "getgid", lambda: 10001)
    monkeypatch.setattr(os, "scandir", lambda path: EmptyDirectory())
    with pytest.raises(AssertionError):
        exec(policy._PAYLOAD, {"__builtins__": vars(builtins) | {"open": read_fixture}})


def test_probe_payload_emits_all_five_kernel_capability_bitmaps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import builtins
    import io

    policy = policy_module()
    status = (
        b"Name:\tpython\nCapInh:\t0000000000000000\nCapPrm:\t0000000000000000\n"
        b"CapEff:\t0000000000000000\nCapBnd:\t0000000000000000\nCapAmb:\t0000000000000000\n"
    )
    reads: list[str] = []

    def read_fixture(path: str, mode: str) -> Any:
        reads.append(path)
        assert mode == "rb"
        if path == "/proc/self/status":
            return io.BytesIO(status)
        assert path == "/proc/self/mountinfo"
        return io.BytesIO((ROOT_ROW + "\n" + MASK_ROW + "\n").encode())

    class EmptyDirectory:
        def __enter__(self) -> Any:
            return iter(())

        def __exit__(self, *args: Any) -> None:
            pass

    monkeypatch.setattr(os, "getuid", lambda: 10001)
    monkeypatch.setattr(os, "getgid", lambda: 10001)
    monkeypatch.setattr(os, "scandir", lambda path: EmptyDirectory())
    exec(policy._PAYLOAD, {"__builtins__": vars(builtins) | {"open": read_fixture}})
    output = json.loads(capsys.readouterr().out)
    assert "/proc/self/status" in reads
    assert output["capabilities"] == {
        "CapInh": "0000000000000000",
        "CapPrm": "0000000000000000",
        "CapEff": "0000000000000000",
        "CapBnd": "0000000000000000",
        "CapAmb": "0000000000000000",
    }
    assert output["uid"] == output["gid"] == 10001
