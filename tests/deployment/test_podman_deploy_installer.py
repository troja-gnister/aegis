"""The aegis-podman host installer, against a fake host (spec D3, D4, D7, D9, D11, D13).

Nothing here runs Podman, systemd, loginctl, chcon or ip: every program goes to FakeSystem,
which refuses any command it does not know, and every path is under tmp_path.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import textwrap
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.support.podman_deploy import (
    AEGISCTL,
    INSTALLER,
    PODMAN,
    SAMPLE_HOST_GID,
    SAMPLE_OTHER_RELEASE,
    SAMPLE_PORT,
    SAMPLE_PREFIX,
    SAMPLE_REGISTRY,
    SAMPLE_RELEASE,
    SAMPLE_THIRD_RELEASE,
    SYSTEMCTL,
    FakeClock,
    FakeSystem,
    environment_assignments,
    image_digest,
    load_installer,
    parse_unit,
    sample_certificate,
    sample_config,
)

ROLES = ("backend", "gateway", "postgres", "caddy")
SECRET_NAMES = (
    "postgres-superuser-password", "db-migrator-password", "db-web-password",
    "db-operations-password", "db-indexer-password", "db-media-password",
    "django-secret-key", "auth-throttle-hmac-key",
)
NAMES = ("aegis.example.test", "192.0.2.10")
FIRST_SUBNET = "10.213.0.0/24"
INIT = (
    "init", "--release", SAMPLE_RELEASE, "--https-port", str(SAMPLE_PORT),
    "--name", NAMES[0], "--address", NAMES[1], "--registry", SAMPLE_REGISTRY,
)
SERVICE_NAMES = ("postgres", "web", "operations", "indexer", "media", "gateway", "caddy")
LONG_RUNNING = {f"{SAMPLE_PREFIX}{name}.service" for name in SERVICE_NAMES}
SOCKET = f"{SAMPLE_PREFIX}caddy.socket"
MIGRATE = f"{SAMPLE_PREFIX}migrate.service"
VOLUME_NAMES = (
    "caddy-data", "derivatives", "indexer-coordination", "postgres-data", "quarantine", "staging",
)
NETWORK_NAMES = ("backend", "edge", "tls-hop")
VOLUMES = tuple(f"{SAMPLE_PREFIX}{name}" for name in VOLUME_NAMES)
NETWORKS = tuple(f"{SAMPLE_PREFIX}{name}" for name in NETWORK_NAMES)
# Quadlet names the services it generates for volumes and networks <unit>-volume.service and
# <unit>-network.service (confirmed with quadlet -dryrun).
GENERATED = {f"{SAMPLE_PREFIX}{name}-volume.service" for name in VOLUME_NAMES} | {
    f"{SAMPLE_PREFIX}{name}-network.service" for name in NETWORK_NAMES
}
INSTALL_LABEL = "io.aegis.install"
# A deployment-test stack (spec D12): its own prefix, images built locally and referenced by
# ID, a test release literal and plain container_t readers. Synthetic values only.
TEST_PREFIX = "aegis-test-0f3a-"
TEST_RELEASE = "test-0f3a5c7e"
TEST_OTHER_RELEASE = "test-9b2d4e6f"
LOCAL_IDS = {role: hashlib.sha256(f"local:{role}".encode()).hexdigest() for role in ROLES}
OTHER_LOCAL_IDS = {role: hashlib.sha256(f"other:{role}".encode()).hexdigest() for role in ROLES}
LOCAL_IMAGES = ",".join(f"{role}={LOCAL_IDS[role]}" for role in ROLES)
OTHER_LOCAL_IMAGES = ",".join(f"{role}={OTHER_LOCAL_IDS[role]}" for role in ROLES)
DIGEST_D = f"sha256:{'d' * 64}"
TEST_INIT = (
    "init", "--release", TEST_RELEASE, "--https-port", str(SAMPLE_PORT),
    "--name", NAMES[0], "--address", NAMES[1], "--local-images", LOCAL_IMAGES,
    "--reader-type", "container_t",
)


def expected_image(role: str, release: str = SAMPLE_RELEASE) -> str:
    return f"{SAMPLE_REGISTRY}/aegis-{role}@{image_digest(role, release)}"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Rig:
    """One operator account: a tmp_path home, a fake host and the installer's entry point."""

    def __init__(self, tmp_path: Path, home_name: str = "home") -> None:
        self.installer = load_installer()
        self.system = FakeSystem()
        self.clock = FakeClock()
        self.home = tmp_path / home_name
        self.home.mkdir()
        self.quadlet = tmp_path / "quadlet"
        self.quadlet.write_text("#!/bin/sh\n", encoding="utf-8")
        self.quadlet.chmod(0o755)
        self.enforce = tmp_path / "enforce"
        self.enforce.write_text("1\n", encoding="ascii")
        self.uid = os.getuid()
        self.environ: dict[str, str] = {}
        self.config_home = self.home / ".config"
        self.data = self.home / ".local" / "share" / "aegis"
        self.config_directory = self.config_home / "aegis"
        # Arguments every command gets, such as --state-home or --prefix.
        self.extra: tuple[str, ...] = ()
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        # Original roots live outside the home; their SELinux labels are faked here.
        self.originals = tmp_path / "originals"
        self.xattrs: dict[str, bytes] = {}
        self.xattr_reads: list[tuple[str, int]] = []
        self.system.labels = self.xattrs
        self.login = "aegis-operator"

    def getxattr(self, path: str, name: str) -> bytes:
        assert name == "security.selinux", name
        # Where the label was read, as a position in the program calls.
        self.xattr_reads.append((path, len(self.system.calls)))
        if path not in self.xattrs:
            raise OSError(errno.ENODATA, "No data available", path)
        return self.xattrs[path]

    def host(self) -> Any:
        self.system.unit_directory = self.quadlet_units
        return self.installer.Host(
            home=self.home, run=self.system, out=self.stdout, err=self.stderr, uid=self.uid,
            gid=SAMPLE_HOST_GID, environ=self.environ, monotonic=self.clock.monotonic,
            sleep=self.clock.sleep, port_in_use=lambda port: port in self.system.listeners,
            quadlet=self.quadlet, selinux_enforce=self.enforce, getxattr=self.getxattr,
            login_name=lambda uid: self.login,
        )

    def __call__(self, *argv: str) -> int:
        words = 2 if argv[:1] == ("roots",) else 1
        arguments = [*argv[:words], *self.extra, *argv[words:]] if argv else []
        return int(self.installer.main(arguments, self.host()))

    def fresh_output(self) -> None:
        self.stdout, self.stderr = io.StringIO(), io.StringIO()

    def output(self) -> str:
        return self.stdout.getvalue() + self.stderr.getvalue()

    @property
    def secrets(self) -> Path:
        return self.data / "secrets"

    @property
    def state_path(self) -> Path:
        return self.data / "state.json"

    @property
    def env_file(self) -> Path:
        return self.config_directory / "aegis.env"

    @property
    def quadlet_units(self) -> Path:
        return self.config_home / "containers" / "systemd"

    @property
    def user_units(self) -> Path:
        return self.config_home / "systemd" / "user"

    def tree(self, release: str = SAMPLE_RELEASE) -> Path:
        return self.data / "releases" / release / "podman"

    def state(self) -> dict[str, Any]:
        loaded: dict[str, Any] = json.loads(self.state_path.read_text(encoding="utf-8"))
        return loaded

    def init(self) -> None:
        assert self(*INIT) == 0, self.output()

    def install(self) -> None:
        self.init()
        assert self("install") == 0, self.output()

    def started(self) -> None:
        self.install()
        assert self("start") == 0, self.output()

    def refused(self, *argv: str, match: str) -> None:
        assert self(*argv) == 1, self.output()
        assert re.search(match, self.stderr.getvalue()), self.stderr.getvalue()


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def rendered_for(rig: Rig, release: str = SAMPLE_RELEASE, **overrides: Any) -> dict[str, str]:
    """What the repository renderer gives for the values the installer recorded."""
    images = {f"image_{role}": expected_image(role, release) for role in ROLES}
    token = rig.state()["install_token"] if rig.state_path.exists() else "0" * 32
    config = sample_config(
        release=release, names=NAMES, secrets_dir=str(rig.secrets),
        backend_subnet=FIRST_SUBNET, install_token=token, **images, **overrides,
    )
    return dict(load_installer().render_units(config))


def unit_path(rig: Rig, name: str) -> Path:
    return (rig.user_units if name.endswith(".socket") else rig.quadlet_units) / name


@contextmanager
def held_lock(rig: Rig, operation: int) -> Iterator[None]:
    descriptor = os.open(rig.data / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, operation)
        yield
    finally:
        os.close(descriptor)


# -- init: records, images and the release tree ------------------------------------------------


def test_init_records_the_release_its_digests_and_the_configuration(rig: Rig) -> None:
    rig.init()
    state = rig.state()

    assert state["phase"] == "ready"
    assert state["installed"] is False
    assert state["desired"] == "stopped"
    assert state["pending"] is None
    assert state["release"] == SAMPLE_RELEASE
    assert state["created"] == "2026-10-01T09:00:00Z"
    assert state["registry"] == SAMPLE_REGISTRY
    assert state["images"] == {role: expected_image(role) for role in ROLES}
    assert state["prefix"] == SAMPLE_PREFIX
    assert state["names"] == list(NAMES)
    assert state["https_port"] == SAMPLE_PORT
    assert state["host_gid"] == SAMPLE_HOST_GID
    assert state["backend_subnet"] == FIRST_SUBNET
    assert state["reader_type"] == "aegis_reader.process"
    assert re.fullmatch(r"[0-9a-f]{32}", state["install_token"])
    assert state["firewall_zones"] == []
    assert [entry["release"] for entry in state["history"]] == [SAMPLE_RELEASE]
    assert mode(rig.state_path) == mode(rig.env_file) == 0o600
    environment = rig.env_file.read_text(encoding="utf-8")
    assert f"AEGIS_PODMAN_RELEASE={SAMPLE_RELEASE}\n" in environment
    assert f"AEGIS_PODMAN_IMAGE_BACKEND={expected_image('backend')}\n" in environment
    assert state["files"][str(rig.env_file)]["sha256"] == digest(rig.env_file)


def test_each_install_gets_its_own_token(tmp_path: Path) -> None:
    tokens = set()
    for index in range(2):
        (tmp_path / str(index)).mkdir()
        rig = Rig(tmp_path / str(index))
        rig.init()
        tokens.add(rig.state()["install_token"])
    assert len(tokens) == 2


def test_the_default_registry_is_the_project_registry(rig: Rig) -> None:
    assert rig(*INIT[:-2]) == 0, rig.output()
    pulled = [call[-1] for call in rig.system.commands(*PODMAN, "pull")]
    assert pulled == [f"ghcr.io/troja-gnister/aegis-{role}:{SAMPLE_RELEASE}" for role in ROLES]


def test_init_pulls_each_image_by_tag_and_pins_its_digest(rig: Rig) -> None:
    rig.init()

    assert rig.system.commands(*PODMAN, "pull") == [
        (*PODMAN, "pull", "--quiet", f"{SAMPLE_REGISTRY}/aegis-{role}:{SAMPLE_RELEASE}")
        for role in ROLES
    ]
    assert rig.system.commands(*PODMAN, "image", "inspect") == [
        (*PODMAN, "image", "inspect", f"{SAMPLE_REGISTRY}/aegis-{role}:{SAMPLE_RELEASE}")
        for role in ROLES
    ]


def test_init_refuses_a_digest_the_registry_did_not_report(rig: Rig) -> None:
    def other_repository(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[2:4] == ("image", "inspect"):
            return 0, json.dumps([{
                "Id": "e" * 64, "Digest": "sha256:" + "1" * 64,
                "RepoDigests": ["registry.example.test/elsewhere@sha256:" + "1" * 64],
            }]), ""
        return None

    rig.system.intercept = other_repository
    rig.refused(*INIT, match="digest")
    assert not rig.system.commands(*PODMAN, "create")


def test_init_refuses_a_release_the_registry_does_not_have(rig: Rig) -> None:
    rig.system.published.discard(SAMPLE_RELEASE)
    rig.refused(*INIT, match="pull")
    assert not rig.system.commands(*PODMAN, "create")
    assert not rig.secrets.exists() or not any(rig.secrets.iterdir())


def test_init_extracts_the_release_tree_and_removes_the_exact_container(rig: Rig) -> None:
    rig.init()
    state = rig.state()
    tree = rig.tree()

    (create,) = rig.system.commands(*PODMAN, "create")
    assert create[2:5] == ("create", "--pull=never", "--network=none")
    assert create[5].startswith("--cidfile=")
    assert Path(create[5].removeprefix("--cidfile=")).parent == rig.data
    assert create[6:] == (expected_image("backend"), "python3")
    assert len(rig.system.created) == 1
    assert rig.system.removed == rig.system.created
    assert rig.system.commands(*PODMAN, "rm") == [(*PODMAN, "rm", rig.system.created[0])]
    assert not Path(create[5].removeprefix("--cidfile=")).exists()
    assert state["extractions"] == []
    assert (tree / "aegis-podman").read_bytes() == INSTALLER.read_bytes()
    recorded = {
        path: record for path, record in state["files"].items() if record["kind"] == "release"
    }
    on_disk = {str(path) for path in tree.rglob("*") if path.is_file()}
    assert set(recorded) == on_disk
    assert all(record["sha256"] == digest(Path(path)) for path, record in recorded.items())
    assert {record["release"] for record in recorded.values()} == {SAMPLE_RELEASE}
    assert sorted(path.name for path in (rig.data / "releases").iterdir()) == [SAMPLE_RELEASE]


def test_a_failed_copy_still_removes_the_exact_container(rig: Rig) -> None:
    rig.system.failures[(*PODMAN, "cp")] = (125, "", "copy failed")

    rig.refused(*INIT, match="copy")
    assert len(rig.system.created) == 1
    assert rig.system.removed == rig.system.created
    assert not any((rig.data / "releases").iterdir())


def test_an_interrupted_create_leaves_an_exact_id_the_next_run_removes(rig: Rig) -> None:
    real = rig.system

    def created_then_timed_out(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[2:3] == ("create",):
            real.intercept = None
            real.podman(command[2:])  # Podman created it and wrote the ID file ...
            raise subprocess.TimeoutExpired(list(command), 300)  # ... but never answered
        return None

    real.intercept = created_then_timed_out
    rig.refused(*INIT, match="did not finish")
    (leaked,) = real.created
    assert real.removed == []
    assert len(rig.state()["extractions"]) == 1

    rig.init()
    assert (*PODMAN, "rm", leaked) in real.calls
    assert real.removed[0] == leaked
    assert set(real.removed) == set(real.created)
    assert rig.state()["extractions"] == []


def test_a_tree_holding_a_symlink_is_refused_and_the_container_removed(rig: Rig) -> None:
    def plant(tree: Path) -> None:
        (tree / "quadlet" / "extra.container").symlink_to("/etc/hostname")

    rig.system.tree_hook = plant

    rig.refused(*INIT, match="symbolic link")
    assert rig.system.removed == rig.system.created
    assert len(rig.system.created) == 1
    # The read-only staging copy is gone too, without following the link.
    assert not any((rig.data / "releases").iterdir())


def test_a_tree_without_the_installer_or_templates_is_refused(rig: Rig) -> None:
    rig.system.tree_hook = lambda tree: (tree / "aegis-podman").unlink()

    rig.refused(*INIT, match="aegis-podman")
    assert rig.system.removed == rig.system.created


def test_a_tree_holding_a_special_file_is_refused(rig: Rig) -> None:
    rig.system.tree_hook = lambda tree: os.mkfifo(tree / "quadlet" / "pipe")

    rig.refused(*INIT, match="special file")
    assert rig.system.removed == rig.system.created
    assert not any((rig.data / "releases").iterdir())


def test_a_release_renderer_with_another_interface_is_refused(rig: Rig) -> None:
    def other_interface(tree: Path) -> None:
        script = tree / "aegis-podman"
        text = script.read_text(encoding="utf-8")
        script.write_text(text.replace("RENDER_API = 1", "RENDER_API = 2", 1), encoding="utf-8")

    rig.system.tree_hook = other_interface

    rig.refused(*INIT, match="does not understand")
    assert not rig.secrets.exists() or not any(rig.secrets.iterdir())


def test_the_release_renderer_validates_every_value(tmp_path: Path) -> None:
    # Unit files cannot carry a secrets directory with a space in it.
    rig = Rig(tmp_path, home_name="operator home")

    rig.refused(*INIT, match="renderer refused")
    assert not rig.secrets.exists() or not any(rig.secrets.iterdir())


def test_rendering_uses_the_verified_template_bytes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.init()
    module = rig.installer

    def no_reread(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("templates are read again after verification")

    real = module._release_renderer

    def loaded(*args: Any, **kwargs: Any) -> Any:
        release_module, templates = real(*args, **kwargs)
        monkeypatch.setattr(release_module, "load_templates", no_reread)
        return release_module, templates

    monkeypatch.setattr(module, "_release_renderer", loaded)
    assert rig("install") == 0, rig.output()


def test_a_lost_release_tree_is_extracted_again(rig: Rig) -> None:
    rig.install()
    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    shutil.rmtree(rig.data / "releases" / SAMPLE_RELEASE)

    assert rig("upgrade", "--release", SAMPLE_RELEASE, "--allow-downgrade") == 0, rig.output()
    assert len(rig.system.created) == 3
    assert rig.system.removed == rig.system.created
    assert (rig.tree() / "aegis-podman").read_bytes() == INSTALLER.read_bytes()


def test_install_brings_back_a_lost_current_release_tree(rig: Rig) -> None:
    rig.install()
    shutil.rmtree(rig.data / "releases" / SAMPLE_RELEASE)

    assert rig("install") == 0, rig.output()
    assert (rig.tree() / "aegis-podman").read_bytes() == INSTALLER.read_bytes()
    assert len(rig.system.created) == 2
    # From the pinned, already local digest: nothing is pulled again.
    assert len(rig.system.commands(*PODMAN, "pull")) == len(ROLES)


def test_an_unexpected_create_output_falls_back_to_the_id_file(rig: Rig) -> None:
    rig.system.create_output = "WARNING: something odd\n"

    rig.init()
    assert len(rig.system.created) == 1
    assert rig.system.removed == rig.system.created


def test_a_failed_create_is_reported_and_its_container_removed(rig: Rig) -> None:
    real = rig.system

    def created_but_failed(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[2:3] == ("create",):
            real.intercept = None
            real.podman(command[2:])  # the container exists and its ID file is written
            real.intercept = created_but_failed
            return 125, "", "Error: something after creation"
        return None

    real.intercept = created_but_failed
    rig.refused(*INIT, match="podman create from .* failed")
    assert real.removed == real.created and len(real.created) == 1
    assert not real.commands(*PODMAN, "cp")


def test_no_container_id_at_all_is_reported_and_nothing_is_removed(rig: Rig) -> None:
    rig.system.create_output = "WARNING: something odd\n"
    rig.system.write_cidfile = False

    rig.refused(*INIT, match="container ID")
    assert not rig.system.commands(*PODMAN, "rm")
    assert not rig.system.commands(*PODMAN, "cp")


def test_a_failed_container_removal_names_the_container(rig: Rig) -> None:
    rig.system.failures[(*PODMAN, "rm")] = (125, "", "container busy")

    assert rig(*INIT) == 1
    assert rig.system.created
    assert f"podman rm {rig.system.created[0]}" in rig.stderr.getvalue()


def test_an_unrecorded_release_directory_is_never_reused(rig: Rig) -> None:
    planted = rig.data / "releases" / SAMPLE_RELEASE
    planted.mkdir(parents=True)
    rig.data.chmod(0o700)
    (rig.data / "releases").chmod(0o700)

    rig.refused(*INIT, match="not recorded")
    assert not rig.system.commands(*PODMAN, "create")


# -- secrets (spec D9) -------------------------------------------------------------------------


def test_secret_names_are_exactly_the_files_the_units_bind(rig: Rig) -> None:
    bound = {
        Path(volume.split(":")[0]).name
        for text in rendered_for(rig).values()
        for volume in parse_unit(text).values("Container", "Volume")
        if volume.startswith("/")
    }
    assert bound == set(SECRET_NAMES)


def test_init_creates_eight_private_labelled_secrets_for_the_container_user(rig: Rig) -> None:
    rig.init()
    state = rig.state()

    assert mode(rig.secrets) == 0o700
    assert sorted(path.name for path in rig.secrets.iterdir()) == sorted(SECRET_NAMES)
    values = set()
    for name in SECRET_NAMES:
        path = rig.secrets / name
        assert stat.S_ISREG(path.lstat().st_mode) and mode(path) == 0o600, name
        content = path.read_text(encoding="ascii")
        assert re.fullmatch(r"[0-9a-f]{64}\n", content), name
        values.add(content)
        info = path.lstat()
        assert state["secrets"][name] == {
            "device": info.st_dev, "inode": info.st_ino, "ctime_ns": info.st_ctime_ns,
        }, name
    assert len(values) == len(SECRET_NAMES)

    labels = rig.system.commands("chcon")
    owners = rig.system.commands(*PODMAN, "unshare", "chown")
    assert len(labels) == len(owners) == len(SECRET_NAMES)
    for label, owner in zip(labels, owners, strict=True):
        # Neither tool may follow a symbolic link.
        assert label[:4] == ("chcon", "-h", "-t", "container_file_t")
        assert owner[:6] == (*PODMAN, "unshare", "chown", "-h", "10001:10001")
        assert label[4] == owner[6]
        assert Path(label[4]).parent == rig.secrets
        assert rig.system.calls.index(label) < rig.system.calls.index(owner)


def test_secrets_never_reach_an_argument_vector_the_output_or_the_records(rig: Rig) -> None:
    rig.started()
    assert rig("status") == 0
    values = [(rig.secrets / name).read_text(encoding="ascii").strip() for name in SECRET_NAMES]

    seen = "\n".join(" ".join(call) for call in rig.system.calls)
    seen += rig.output() + rig.state_path.read_text(encoding="utf-8")
    seen += rig.env_file.read_text(encoding="utf-8")
    seen += "".join(path.read_text(encoding="utf-8") for path in rig.quadlet_units.iterdir())
    for value in values:
        assert value not in seen


def test_init_refuses_an_existing_secret_it_did_not_record(rig: Rig) -> None:
    rig.secrets.mkdir(parents=True, mode=0o700)
    rig.data.chmod(0o700)
    planted = rig.secrets / "db-web-password"
    planted.write_text("operator-chosen\n", encoding="ascii")

    rig.refused(*INIT, match="db-web-password")
    assert planted.read_text(encoding="ascii") == "operator-chosen\n"
    assert [path.name for path in rig.secrets.iterdir()] == ["db-web-password"]
    assert not rig.system.commands("chcon")
    assert not rig.system.commands(*PODMAN, "pull")


def test_an_interrupted_init_resumes_with_the_secrets_it_recorded(rig: Rig) -> None:
    chowns: list[tuple[str, ...]] = []

    def third_chown_fails(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[2:4] == ("unshare", "chown"):
            chowns.append(command)
            if len(chowns) == 3:
                return 1, "", "chown: operation not permitted"
        return None

    rig.system.intercept = third_chown_fails
    assert rig(*INIT) == 1
    state = rig.state()
    assert state["phase"] == "initializing"
    ready = {name: record for name, record in state["secrets"].items() if "pending" not in record}
    assert len(ready) == 2
    # Only fully prepared secrets ever appear under their names.
    assert sorted(path.name for path in rig.secrets.iterdir() if not path.name.startswith(".")) \
        == sorted(ready)

    rig.system.intercept = None
    rig.init()
    state = rig.state()
    assert sorted(path.name for path in rig.secrets.iterdir()) == sorted(SECRET_NAMES)
    for name, record in ready.items():
        assert state["secrets"][name] == record, name
    assert not any("pending" in record for record in state["secrets"].values())


def test_a_secret_renamed_just_before_an_interruption_is_kept(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = rig.installer
    real = module._sync_directory

    def interrupted(path: Path) -> None:
        if path == rig.secrets:
            raise KeyboardInterrupt
        real(path)

    monkeypatch.setattr(module, "_sync_directory", interrupted)
    with pytest.raises(KeyboardInterrupt):
        rig(*INIT)
    monkeypatch.undo()
    first = sorted(path.name for path in rig.secrets.iterdir())
    assert len(first) == 1
    identity = (rig.secrets / first[0]).lstat().st_ino
    assert "pending" in rig.state()["secrets"][first[0]]

    rig.init()
    assert (rig.secrets / first[0]).lstat().st_ino == identity
    assert sorted(path.name for path in rig.secrets.iterdir()) == sorted(SECRET_NAMES)


def test_a_file_for_mapped_containers_is_private_labelled_and_in_the_operators_group(
    rig: Rig,
) -> None:
    rig.init()
    manifest = rig.data / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    manifest.chmod(0o644)
    first = len(rig.system.calls)

    rig.installer.prepare_for_mapped_containers(rig.host(), manifest)
    assert mode(manifest) == 0o600
    # Container uid 10001 and, inside podman unshare, gid 0: the operator's group, which
    # GIDMap=+g10001:@<host gid>:1 shows as 10001. Secrets instead stay 10001:10001.
    assert rig.system.calls[first:] == [
        ("chcon", "-h", "-t", "container_file_t", str(manifest)),
        (*PODMAN, "unshare", "chown", "-h", "10001:0", str(manifest)),
    ]


def test_a_link_is_never_prepared_for_mapped_containers(rig: Rig, tmp_path: Path) -> None:
    rig.init()
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    target.chmod(0o644)
    link = rig.data / "manifest.json"
    link.symlink_to(target)

    with pytest.raises(rig.installer.InstallError, match="symbolic link"):
        rig.installer.prepare_for_mapped_containers(rig.host(), link)
    assert mode(target) == 0o644
    assert not rig.system.commands(*PODMAN, "unshare", "chown", "-h", "10001:0")


def test_init_refuses_once_initialized(rig: Rig) -> None:
    rig.init()
    rig.refused(*INIT, match="already initialized")


# -- the record, its directories and its lock --------------------------------------------------


def sample_state(installer: Any) -> Any:
    state = installer.State(
        phase="ready", release=SAMPLE_RELEASE, registry=SAMPLE_REGISTRY, prefix=SAMPLE_PREFIX,
        names=list(NAMES), https_port=SAMPLE_PORT, host_gid=SAMPLE_HOST_GID,
        backend_subnet=FIRST_SUBNET, reader_type="aegis_reader.process",
        install_token="0" * 32,
    )
    state.files["/example/unit"] = {"kind": "unit", "sha256": "0" * 64}
    state.history.append({"sequence": 1, "release": SAMPLE_RELEASE})
    return state


def private_paths(installer: Any, tmp_path: Path) -> Any:
    paths = installer.Paths.resolve(tmp_path / "home", {})
    paths.data.mkdir(parents=True, mode=0o700)
    return paths


def test_the_record_round_trips_privately(tmp_path: Path) -> None:
    installer = load_installer()
    paths = private_paths(installer, tmp_path)
    state = sample_state(installer)

    installer.save_state(paths, state)
    assert installer.load_state(paths, os.getuid()) == state
    assert mode(paths.state) == 0o600
    assert [path.name for path in paths.data.iterdir()] == ["state.json"]


def test_a_failed_record_write_keeps_the_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = load_installer()
    paths = private_paths(installer, tmp_path)
    state = sample_state(installer)
    installer.save_state(paths, state)
    before = paths.state.read_bytes()

    def no_disk(descriptor: int) -> None:
        raise OSError(28, "No space left on device")

    changed = sample_state(installer)
    changed.release = SAMPLE_OTHER_RELEASE
    monkeypatch.setattr(os, "fsync", no_disk)
    with pytest.raises(OSError, match="No space"):
        installer.save_state(paths, changed)
    monkeypatch.undo()

    assert paths.state.read_bytes() == before
    assert [path.name for path in paths.data.iterdir()] == ["state.json"]


@pytest.mark.parametrize("damage", [
    "symlink", "shared", "unknown-version", "extra-key", "json", "wrong-type", "boolean-port",
    "installed-without-inventory", "malformed-file-entry", "malformed-pending", "empty-token",
    "test-prefix-with-install-sections", "malformed-restart-flag", "production-local-images",
    "production-container-t", "unknown-reader-type", "test-release-without-local-images",
    "malformed-local-images", "production-pending-local-images",
    "production-pending-test-release", "malformed-pending-local-images",
])
def test_an_unsafe_or_unknown_record_is_refused(tmp_path: Path, damage: str) -> None:
    installer = load_installer()
    paths = private_paths(installer, tmp_path)
    installer.save_state(paths, sample_state(installer))
    raw = json.loads(paths.state.read_text(encoding="utf-8"))
    changes: dict[str, dict[str, Any]] = {
        "unknown-version": {"version": 99}, "extra-key": {"other": 1},
        "wrong-type": {"names": "aegis.example.test"}, "boolean-port": {"https_port": True},
        "installed-without-inventory": {"installed": True, "units": {}},
        "malformed-file-entry": {"files": {"/x": "abc"}},
        "malformed-pending": {"pending": {"release": 7}},
        "empty-token": {"install_token": ""},
        "test-prefix-with-install-sections": {"prefix": "aegis-test-0f3a-",
                                              "install_sections": True},
        "malformed-restart-flag": {"pending": {"release": SAMPLE_OTHER_RELEASE, "images": {},
                                               "restart_queued": "yes"}},
        # The deployment test's options never apply to the production prefix.
        "production-local-images": {"local_images": True},
        "production-container-t": {"reader_type": "container_t"},
        "unknown-reader-type": {"prefix": TEST_PREFIX, "install_sections": False,
                                "reader_type": "unconfined_t"},
        "test-release-without-local-images": {"prefix": TEST_PREFIX, "install_sections": False,
                                              "release": TEST_RELEASE},
        "malformed-local-images": {"local_images": "yes"},
        "production-pending-local-images": {"pending": {
            "release": SAMPLE_OTHER_RELEASE, "images": {}, "local_images": True,
        }},
        "production-pending-test-release": {"pending": {"release": TEST_RELEASE, "images": {}}},
        "malformed-pending-local-images": {
            "prefix": TEST_PREFIX, "install_sections": False,
            "pending": {"release": SAMPLE_OTHER_RELEASE, "images": {}, "local_images": "yes"},
        },
    }
    if damage == "symlink":
        target = tmp_path / "elsewhere.json"
        paths.state.rename(target)
        paths.state.symlink_to(target)
    elif damage == "shared":
        paths.state.chmod(0o644)
    elif damage == "json":
        paths.state.write_text("{", encoding="utf-8")
    else:
        paths.state.write_text(json.dumps(raw | changes[damage]), encoding="utf-8")

    with pytest.raises(installer.InstallError):
        installer.load_state(paths, os.getuid())


def test_a_malformed_record_ends_in_an_error_not_a_traceback(rig: Rig) -> None:
    rig.install()
    raw = rig.state() | {"installed": True, "units": {"socket": SOCKET}}
    rig.state_path.write_text(json.dumps(raw), encoding="utf-8")

    rig.refused("status", match="not usable")
    rig.refused("uninstall", match="not usable")


@pytest.mark.parametrize("relative", [
    ".local/share/aegis", ".config/aegis", ".local/share/aegis/secrets",
    ".local/share/aegis/releases",
])
def test_symlinked_state_directories_are_refused(rig: Rig, tmp_path: Path, relative: str) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    link = rig.home / relative
    link.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    link.parent.chmod(0o700)
    link.symlink_to(target)

    rig.refused(*INIT, match="symbolic link")
    assert not any(target.iterdir())


def test_directories_owned_by_someone_else_are_refused(rig: Rig) -> None:
    rig.data.mkdir(parents=True, mode=0o700)
    rig.uid = os.getuid() + 1

    rig.refused(*INIT, match="not owned by you")
    assert not rig.system.calls


def test_a_file_in_place_of_a_state_directory_is_refused(rig: Rig) -> None:
    rig.data.parent.mkdir(parents=True)
    rig.data.write_text("not a directory\n", encoding="utf-8")

    rig.refused(*INIT, match="not a directory")


def test_a_shared_secrets_directory_is_refused(rig: Rig) -> None:
    rig.secrets.mkdir(parents=True, mode=0o700)
    rig.data.chmod(0o700)
    rig.secrets.chmod(0o750)

    rig.refused(*INIT, match="0700")


@pytest.mark.parametrize("command", [
    ("install",), ("start",), ("stop",), ("upgrade", "--release", SAMPLE_OTHER_RELEASE),
    ("uninstall",), ("bootstrap-password", "--username", "admin", "--email", "a@example.org"),
])
def test_a_second_command_waits_for_no_one_and_changes_nothing(
    rig: Rig, command: tuple[str, ...],
) -> None:
    rig.install()
    before = rig.state_path.read_bytes()
    calls = len(rig.system.calls)

    with held_lock(rig, fcntl.LOCK_SH):
        rig.refused(*command, match="another aegis-podman")
    assert rig.state_path.read_bytes() == before
    assert rig.system.calls[calls:] == []


def test_status_beside_a_running_command_shows_the_live_state_only(rig: Rig) -> None:
    rig.started()
    with held_lock(rig, fcntl.LOCK_SH):
        assert rig("status") == 0, rig.output()
    rig.fresh_output()
    with held_lock(rig, fcntl.LOCK_EX):
        assert rig("status") == 3
    output = rig.stdout.getvalue()
    assert "an aegis-podman command is in progress" in output
    assert re.search(rf"{SAMPLE_PREFIX}web\.service\s+active/running", output)
    # The record-dependent parts are left out while it may change.
    assert "(running)" not in output and "Recovery" not in output
    rig.fresh_output()
    with held_lock(rig, fcntl.LOCK_EX):
        assert rig("ca-cert") == 0
    assert rig.stdout.getvalue() == sample_certificate()
    assert "in progress" in rig.stderr.getvalue()


@pytest.mark.parametrize("command", [
    ("start",), ("upgrade", "--release", SAMPLE_OTHER_RELEASE),
])
def test_the_lock_is_released_while_waiting_for_systemd(
    rig: Rig, command: tuple[str, ...],
) -> None:
    rig.started()
    assert rig("stop") == 0
    if command[0] == "upgrade":
        assert rig("start") == 0
    rig.system.unhealthy = {f"{SAMPLE_PREFIX}web"}
    shared: list[bool] = []

    def try_reading() -> None:
        descriptor = os.open(rig.data / "lock", os.O_RDWR)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            shared.append(True)
        except BlockingIOError:
            shared.append(False)
        finally:
            os.close(descriptor)
        rig.system.unhealthy.clear()

    rig.clock.hook = try_reading
    assert rig(*command) == 0, rig.output()
    assert shared == [True]


def test_init_takes_the_lock_too(rig: Rig) -> None:
    rig.data.mkdir(parents=True, mode=0o700)
    with held_lock(rig, fcntl.LOCK_EX):
        rig.refused(*INIT, match="another aegis-podman")
    assert not rig.system.commands(*PODMAN, "pull")


def test_the_lock_file_is_private_and_never_followed(rig: Rig, tmp_path: Path) -> None:
    rig.init()
    assert mode(rig.data / "lock") == 0o600
    (rig.data / "lock").unlink()
    (rig.data / "lock").symlink_to(tmp_path / "elsewhere")

    rig.refused("install", match="symbolic link")
    assert not (tmp_path / "elsewhere").exists()


# -- XDG directories and the deployment-test options -------------------------------------------


def test_xdg_config_home_is_honored_and_xdg_data_home_is_not(rig: Rig, tmp_path: Path) -> None:
    # systemd and Quadlet read units under XDG_CONFIG_HOME. The record stays in one fixed
    # place, whatever a shell sets XDG_DATA_HOME to; --state-home is the only alternative.
    rig.environ = {"XDG_CONFIG_HOME": str(tmp_path / "config"),
                   "XDG_DATA_HOME": str(tmp_path / "data")}
    rig.system.manager_environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    rig.config_home = tmp_path / "config"
    rig.config_directory = rig.config_home / "aegis"

    rig.install()
    assert rig.state_path == rig.home / ".local" / "share" / "aegis" / "state.json"
    assert rig.state_path.is_file() and rig.env_file.is_file()
    assert (rig.quadlet_units / f"{SAMPLE_PREFIX}web.container").is_file()
    assert (rig.user_units / SOCKET).is_file()
    assert not (tmp_path / "data").exists() and not (rig.home / ".config").exists()
    rig.environ = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    assert rig("status") == 3, rig.output()


def test_relative_xdg_directories_are_ignored_as_the_spec_says(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A relative XDG value is invalid and ignored, as systemd itself ignores it: units go
    # where the user manager reads them, and nothing appears beside the working directory.
    working = tmp_path / "working"
    working.mkdir()
    monkeypatch.chdir(working)
    rig.environ = {"XDG_CONFIG_HOME": "relative/place", "XDG_DATA_HOME": "relative/data"}

    rig.install()
    assert (rig.home / ".config" / "containers" / "systemd" / f"{SAMPLE_PREFIX}web.container"
            ).is_file()
    assert rig.state_path == rig.home / ".local" / "share" / "aegis" / "state.json"
    assert list(working.iterdir()) == []


def test_a_user_manager_reading_units_elsewhere_is_refused(rig: Rig, tmp_path: Path) -> None:
    rig.system.manager_environment = {"XDG_CONFIG_HOME": str(tmp_path / "elsewhere")}
    rig.refused(*INIT, match="user manager reads units")
    assert not rig.system.commands(*PODMAN, "pull")


def test_a_state_home_holds_the_record_secrets_trees_and_configuration(
    rig: Rig, tmp_path: Path,
) -> None:
    state_home = tmp_path / "harness-state"
    rig.extra = ("--state-home", str(state_home), "--prefix", "aegis-test-0f3a-")
    rig.data = rig.config_directory = state_home
    rig.system.prefix = "aegis-test-0f3a-"

    rig.install()
    assert rig("start") == 0, rig.output()
    state = rig.state()
    assert state["prefix"] == "aegis-test-0f3a-"
    assert (state_home / "aegis.env").is_file()
    assert sorted(path.name for path in (state_home / "secrets").iterdir()) == sorted(SECRET_NAMES)
    # Unit files still go where systemd reads them, under the test prefix.
    assert (rig.quadlet_units / "aegis-test-0f3a-web.container").is_file()
    assert (rig.user_units / "aegis-test-0f3a-caddy.socket").is_file()
    assert not (rig.home / ".local").exists() and not (rig.home / ".config" / "aegis").exists()
    web = parse_unit((rig.quadlet_units / "aegis-test-0f3a-web.container").read_text())
    assert f"{state_home}/secrets/db-web-password:/run/secrets/db_web_password:ro" in web.values(
        "Container", "Volume",
    )


def test_a_command_with_another_prefix_is_refused(rig: Rig) -> None:
    rig.install()
    rig.refused("start", "--prefix", "aegis-test-0f3a-", match="prefix is aegis-")


@pytest.mark.parametrize("prefix", ["aegis", "Aegis-", "-x-", "a" * 32 + "-"])
def test_an_invalid_prefix_is_refused(rig: Rig, prefix: str) -> None:
    rig.refused(*INIT, f"--prefix={prefix}", match="prefix")
    assert not rig.system.calls


def test_a_relative_state_home_is_refused(rig: Rig) -> None:
    rig.refused(*INIT, "--state-home", "relative/state", match="--state-home")


def no_install_sections(rig: Rig) -> None:
    """No unit file or drop-in has an [Install] section."""
    for path in [*rig.quadlet_units.rglob("*"), *rig.user_units.iterdir()]:
        if path.is_file():
            assert "[Install]" not in path.read_text(encoding="utf-8"), path.name


def test_install_can_leave_out_every_install_section_and_the_choice_sticks(rig: Rig) -> None:
    rig.init()
    assert rig("install", "--no-install-sections") == 0, rig.output()
    no_install_sections(rig)
    assert not rig.system.commands(*SYSTEMCTL, "enable")
    assert rig.state()["install_sections"] is False
    # A plain rerun, an upgrade and an abandoned upgrade all keep the recorded choice.
    assert rig("install") == 0, rig.output()
    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    no_install_sections(rig)
    assert not rig.system.commands(*SYSTEMCTL, "enable")
    assert rig("uninstall") == 0, rig.output()
    assert not rig.system.commands(*SYSTEMCTL, "disable")


def test_a_test_stack_never_gets_install_sections(rig: Rig, tmp_path: Path) -> None:
    rig.extra = ("--state-home", str(tmp_path / "harness"), "--prefix", "aegis-test-0f3a-")
    rig.data = rig.config_directory = tmp_path / "harness"
    rig.system.prefix = "aegis-test-0f3a-"
    rig.init()
    assert rig.state()["install_sections"] is False

    for _ in range(2):  # a plain rerun keeps the test stack free of [Install] sections
        assert rig("install") == 0, rig.output()
        no_install_sections(rig)
    rig.refused("install", "--install-sections", match="aegis- prefix")
    no_install_sections(rig)
    assert not rig.system.commands(*SYSTEMCTL, "enable")


def test_switching_install_sections_needs_an_explicit_flag(rig: Rig) -> None:
    rig.init()
    assert rig("install", "--no-install-sections") == 0, rig.output()

    assert rig("install", "--install-sections") == 0, rig.output()
    assert "[Install]" in (rig.user_units / SOCKET).read_text(encoding="utf-8")
    assert rig.system.commands(*SYSTEMCTL, "enable") == [(*SYSTEMCTL, "enable", SOCKET)]
    first = len(rig.system.calls)
    assert rig("install", "--no-install-sections") == 0, rig.output()
    no_install_sections(rig)
    # The link enable made goes again.
    assert (*SYSTEMCTL, "disable", SOCKET) in rig.system.calls[first:]


# -- the deployment test's local images and container_t readers (spec D12) ---------------------


def make_test_stack(rig: Rig, tmp_path: Path) -> None:
    """Point the rig at a deployment-test stack: its own state home and prefix, and images
    that exist only in local storage."""
    rig.extra = ("--state-home", str(tmp_path / "harness"), "--prefix", TEST_PREFIX)
    rig.data = rig.config_directory = tmp_path / "harness"
    rig.system.prefix = TEST_PREFIX
    rig.system.local_images = {LOCAL_IDS[role]: (role, TEST_RELEASE) for role in ROLES} | {
        OTHER_LOCAL_IDS[role]: (role, TEST_OTHER_RELEASE) for role in ROLES
    }


def without(arguments: tuple[str, ...], flag: str) -> list[str]:
    """``arguments`` without ``flag`` and its value."""
    index = arguments.index(flag)
    return [*arguments[:index], *arguments[index + 2:]]


def replaced(arguments: tuple[str, ...], flag: str, value: str) -> list[str]:
    changed = list(arguments)
    changed[changed.index(flag) + 1] = value
    return changed


def test_a_test_stack_runs_local_images_as_container_t_without_pulling(
    rig: Rig, tmp_path: Path,
) -> None:
    make_test_stack(rig, tmp_path)

    assert rig(*TEST_INIT) == 0, rig.output()
    state = rig.state()
    assert state["release"] == TEST_RELEASE
    assert state["images"] == LOCAL_IDS
    assert state["local_images"] is True
    assert state["reader_type"] == "container_t"
    assert state["install_sections"] is False
    assert not rig.system.commands(*PODMAN, "pull")
    assert [call[4:] for call in rig.system.commands(*PODMAN, "image", "inspect")] == [
        (LOCAL_IDS[role],) for role in ROLES
    ]
    # The release tree comes out of the local backend image, by its ID.
    (create,) = rig.system.commands(*PODMAN, "create")
    assert create[-2] == LOCAL_IDS["backend"]

    assert rig("install", "--no-install-sections") == 0, rig.output()
    assert rig("start") == 0, rig.output()
    no_install_sections(rig)
    assert not rig.system.commands(*SYSTEMCTL, "enable")
    for service, role in (("web", "backend"), ("gateway", "gateway"), ("postgres", "postgres"),
                          ("caddy", "caddy")):
        unit = parse_unit((rig.quadlet_units / f"{TEST_PREFIX}{service}.container").read_text())
        assert unit.value("Container", "Image") == LOCAL_IDS[role], service
    for service in ("operations", "indexer", "media", "gateway"):
        unit = parse_unit((rig.quadlet_units / f"{TEST_PREFIX}{service}.container").read_text())
        assert unit.value("Container", "SecurityLabelType") == "container_t", service


def test_readiness_compares_image_ids_not_the_name_podman_reports(
    rig: Rig, tmp_path: Path,
) -> None:
    # Measured on the real host (deployment harness run 1): Podman reports a container created
    # from an image ID by the image's local tag, so comparing names never saw a current one.
    make_test_stack(rig, tmp_path)
    assert rig(*TEST_INIT) == 0, rig.output()
    assert rig("install", "--no-install-sections") == 0, rig.output()
    roles = {"postgres": "postgres", "web": "backend", "operations": "backend",
             "indexer": "backend", "media": "backend", "gateway": "gateway", "caddy": "caddy"}
    for service, role in roles.items():
        rig.system.reported_image_names[f"{TEST_PREFIX}{service}"] = (
            f"localhost/aegis-{role}:deploy-check"
        )

    assert rig("start") == 0, rig.output()
    assert "stale" not in rig.output()
    assert rig("status") == 0, rig.output()
    assert "image=current" in rig.output()


def test_local_images_are_refused_for_the_production_prefix(rig: Rig) -> None:
    rig.refused(*INIT, "--local-images", LOCAL_IMAGES, match="--local-images .*--prefix")
    rig.fresh_output()
    rig.refused(*INIT, "--prefix", SAMPLE_PREFIX, "--local-images", LOCAL_IMAGES,
                match="--local-images .*--prefix")
    assert not rig.system.calls


@pytest.mark.parametrize("value", [
    "",
    ",".join(f"{role}={LOCAL_IDS[role]}" for role in ROLES[:3]),
    f"{LOCAL_IMAGES},backend={LOCAL_IDS['backend']}",
    f"{LOCAL_IMAGES},worker={'0' * 64}",
    LOCAL_IMAGES.replace(LOCAL_IDS["caddy"], "A" * 64),
    LOCAL_IMAGES.replace(LOCAL_IDS["caddy"], "0" * 63),
    LOCAL_IMAGES.replace(LOCAL_IDS["caddy"], "g" * 64),
    LOCAL_IMAGES.replace(LOCAL_IDS["caddy"], f"sha256:{'0' * 64}"),
    LOCAL_IMAGES.replace(LOCAL_IDS["caddy"], f"registry.example.test/aegis-caddy@{DIGEST_D}"),
    LOCAL_IMAGES.replace(f"caddy={LOCAL_IDS['caddy']}", LOCAL_IDS["caddy"]),
], ids=["empty", "missing-role", "repeated-role", "unknown-role", "uppercase", "short",
        "not-hex", "digest-prefix", "repository-digest", "no-role"])
def test_malformed_local_images_are_refused_before_anything_runs(
    rig: Rig, tmp_path: Path, value: str,
) -> None:
    make_test_stack(rig, tmp_path)

    rig.refused(*replaced(TEST_INIT, "--local-images", value), match="--local-images")
    assert not rig.system.calls
    assert not (tmp_path / "harness").exists()


def test_a_test_release_needs_local_images(rig: Rig, tmp_path: Path) -> None:
    make_test_stack(rig, tmp_path)

    rig.refused(*without(TEST_INIT, "--local-images"), match="test release.*--local-images")
    assert not rig.system.calls


def test_a_local_image_missing_from_local_storage_is_refused(rig: Rig, tmp_path: Path) -> None:
    make_test_stack(rig, tmp_path)
    del rig.system.local_images[LOCAL_IDS["gateway"]]

    rig.refused(*TEST_INIT, match=f"{LOCAL_IDS['gateway']} .*not in local storage")
    assert not rig.system.commands(*PODMAN, "create")
    assert not (rig.data / "secrets").exists() or not any((rig.data / "secrets").iterdir())


def test_container_t_readers_are_refused_for_the_production_prefix(rig: Rig) -> None:
    rig.refused(*INIT, "--reader-type", "container_t", match="--reader-type container_t")
    assert not rig.system.calls


@pytest.mark.parametrize("reader_type", ["unconfined_t", "spc_t", "aegis_reader2.process", ""])
def test_an_unknown_reader_type_is_refused(rig: Rig, tmp_path: Path, reader_type: str) -> None:
    make_test_stack(rig, tmp_path)

    rig.refused(*replaced(TEST_INIT, "--reader-type", reader_type), match="--reader-type")
    assert not rig.system.calls


def test_a_test_stack_may_name_the_production_reader_type(rig: Rig, tmp_path: Path) -> None:
    make_test_stack(rig, tmp_path)

    assert rig(*replaced(TEST_INIT, "--reader-type", "aegis_reader.process")) == 0, rig.output()
    assert rig.state()["reader_type"] == "aegis_reader.process"


def test_the_deployment_test_options_stay_out_of_the_help(
    rig: Rig, capsys: pytest.CaptureFixture[str],
) -> None:
    for command in ("init", "upgrade"):
        with pytest.raises(SystemExit):
            rig(command, "--help")
        text = capsys.readouterr().out
        assert "--local-images" not in text and "--reader-type" not in text, command


def test_a_container_t_stack_adds_a_container_file_t_root_without_probe_or_module(
    rig: Rig, tmp_path: Path,
) -> None:
    make_test_stack(rig, tmp_path)
    assert rig(*TEST_INIT) == 0, rig.output()
    assert rig("install", "--no-install-sections") == 0, rig.output()
    root = make_root(rig, label=CONTAINER_FILE)
    first = len(rig.system.calls)

    assert add_root(rig, root) == 0, rig.output()
    state = rig.state()
    assert state["selinux_covered"] == ["container_file_t"] and state["selinux_pending"] == []
    assert state["roots"]["photos"]["selinux_type"] == "container_file_t"
    assert not rig.system.probes
    assert not (rig.data / "selinux" / "aegis_reader.cil").exists()
    (preflight,) = [call for call in aegisctl_calls(rig, first) if call[1] == "preflight"]
    assert preflight[preflight.index("--observer-image") + 1] == LOCAL_IDS["gateway"]
    assert preflight[preflight.index("--reader-type") + 1] == "container_t"
    assert "--allow-test-image-id" in preflight
    for role in ROOT_ROLES:
        assert (rig.quadlet_units / f"{TEST_PREFIX}{role}.container.d" / DROP_IN).is_file()
    no_install_sections(rig)


def test_a_container_t_stack_refuses_a_root_of_any_other_type(rig: Rig, tmp_path: Path) -> None:
    make_test_stack(rig, tmp_path)
    assert rig(*TEST_INIT) == 0, rig.output()
    assert rig("install") == 0, rig.output()
    root = make_root(rig)
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "photos", "--source", str(root),
                match="unlabeled_t.*container_t.*container_file_t")
    assert rig.system.calls[first:] == []
    assert not (rig.data / "selinux" / "aegis_reader.cil").exists()
    assert rig.state()["selinux_pending"] == [] and rig.state()["roots"] == {}


def test_a_production_stack_never_passes_the_test_image_flag(rig: Rig) -> None:
    rig.install()
    root = covered_root(rig)
    assert root.is_dir()
    preflights = [call for call in aegisctl_calls(rig) if call[1] == "preflight"]
    assert preflights and all("--allow-test-image-id" not in call for call in preflights)


def test_upgrade_moves_a_test_stack_to_other_local_images_without_ordering(
    rig: Rig, tmp_path: Path,
) -> None:
    make_test_stack(rig, tmp_path)
    assert rig(*TEST_INIT) == 0, rig.output()
    assert rig("install") == 0, rig.output()
    assert rig("start") == 0, rig.output()

    # Neither test release carries a creation label, so ordering would refuse them both.
    assert rig("upgrade", "--release", TEST_OTHER_RELEASE,
               "--local-images", OTHER_LOCAL_IMAGES) == 0, rig.output()
    state = rig.state()
    assert state["release"] == TEST_OTHER_RELEASE and state["images"] == OTHER_LOCAL_IDS
    assert state["local_images"] is True and state["pending"] is None
    assert not rig.system.commands(*PODMAN, "pull")
    web = parse_unit((rig.quadlet_units / f"{TEST_PREFIX}web.container").read_text())
    assert web.value("Container", "Image") == OTHER_LOCAL_IDS["backend"]
    no_install_sections(rig)


def test_upgrade_to_local_images_is_refused_for_the_production_prefix(rig: Rig) -> None:
    rig.install()
    first = len(rig.system.calls)

    rig.refused("upgrade", "--release", TEST_RELEASE, "--local-images", LOCAL_IMAGES,
                match="--local-images .*--prefix")
    rig.fresh_output()
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, "--local-images", LOCAL_IMAGES,
                match="--local-images .*--prefix")
    assert rig.system.calls[first:] == []
    assert rig.state()["release"] == SAMPLE_RELEASE


def test_a_test_release_upgrade_needs_local_images(rig: Rig, tmp_path: Path) -> None:
    make_test_stack(rig, tmp_path)
    assert rig(*TEST_INIT) == 0, rig.output()
    first = len(rig.system.calls)

    rig.refused("upgrade", "--release", TEST_OTHER_RELEASE, match="test release.*--local-images")
    rig.fresh_output()
    rig.refused("upgrade", "--abandon", "--local-images", OTHER_LOCAL_IMAGES,
                match="--local-images goes with --release")
    assert rig.system.calls[first:] == []


# -- preflight ---------------------------------------------------------------------------------


def _set(attribute: str, value: object) -> Callable[[Rig], None]:
    return lambda rig: setattr(rig.system, attribute, value)


@pytest.mark.parametrize(("change", "message"), [
    (_set("rootless", False), "rootless"),
    (_set("selinux_enabled", False), "SELinux"),
    (lambda rig: rig.enforce.write_text("0\n", encoding="ascii"), "enforcing"),
    (_set("systemd_version", "systemd 253 (253.1-1)"), "254"),
    (_set("systemd_version", "not systemd"), "systemd"),
    (lambda rig: rig.quadlet.unlink(), "Quadlet"),
    (lambda rig: rig.quadlet.chmod(0o644), "Quadlet"),
    (_set("linger", "no"), "loginctl enable-linger"),
    (lambda rig: setattr(rig, "uid", 0), "root"),
])
def test_preflight_refusals_change_nothing(
    rig: Rig, change: Callable[[Rig], None], message: str,
) -> None:
    change(rig)

    rig.refused(*INIT, match=message)
    assert not rig.system.commands(*PODMAN, "pull")
    assert not (rig.home / ".local").exists()
    assert not (rig.home / ".config").exists()


def test_preflight_reads_podman_systemd_linger_and_the_manager_environment(rig: Rig) -> None:
    rig.init()
    assert (*PODMAN, "info", "--format", "json") in rig.system.calls
    assert ("systemctl", "--version") in rig.system.calls
    assert ("loginctl", "show-user", str(rig.uid), "--property=Linger", "--value") \
        in rig.system.calls
    assert (*SYSTEMCTL, "show-environment") in rig.system.calls


# -- the HTTPS port (spec D7) ------------------------------------------------------------------


def init_with_port(port: str) -> tuple[str, ...]:
    arguments = list(INIT)
    arguments[arguments.index("--https-port") + 1] = port
    return tuple(arguments)


@pytest.mark.parametrize("port", ["0", "80", "443", "1023", "65536"])
def test_ports_outside_1024_to_65535_are_refused(rig: Rig, port: str) -> None:
    rig.refused(*init_with_port(port), match="1024")
    assert not rig.system.calls


def test_a_port_with_a_listener_is_refused(rig: Rig) -> None:
    rig.system.listeners.add(SAMPLE_PORT)
    rig.refused(*INIT, match="listen")
    assert not rig.system.commands(*PODMAN, "pull")


@pytest.mark.parametrize(("host_port", "count"), [(SAMPLE_PORT, 1), (SAMPLE_PORT - 3, 5)])
def test_a_port_mapped_by_any_container_is_refused(rig: Rig, host_port: int, count: int) -> None:
    rig.system.containers.append({
        "Id": "a" * 64, "Names": ["other-service"], "State": "exited",
        "Ports": [{"host_ip": "", "container_port": 443, "host_port": host_port,
                   "range": count, "protocol": "tcp"}],
    })
    rig.refused(*INIT, match="other-service")


def test_other_container_ports_do_not_block(rig: Rig) -> None:
    rig.system.containers.append({
        "Id": "a" * 64, "Names": ["other-service"], "State": "running",
        "Ports": [{"host_ip": "", "container_port": 443, "host_port": SAMPLE_PORT + 1,
                   "range": 1, "protocol": "tcp"}],
    })
    rig.system.containers.append({"Id": "b" * 64, "Names": ["quiet"], "Ports": None})
    rig.init()


def test_the_port_probe_sees_a_real_listener() -> None:
    installer = load_installer()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert installer.port_in_use(port) is True


# -- the backend subnet (spec D1; review note M8) ----------------------------------------------


def test_the_subnet_avoids_every_network_and_route(rig: Rig) -> None:
    rig.system.networks["other"] = {
        "name": "other", "id": "8" * 64, "subnets": [{"subnet": "10.213.0.0/23"}],
    }
    rig.system.routes4.append({"dst": "10.213.2.0/24", "dev": "test-vpn0", "table": "100"})
    rig.system.routes4.append({"dst": "10.213.3.7", "dev": "test-tun0"})
    rig.system.routes6.append({"dst": "::ffff:10.213.4.0/120", "dev": "test-tun1"})

    rig.init()
    assert rig.state()["backend_subnet"] == "10.213.5.0/24"
    assert (*PODMAN, "network", "inspect", "podman", "other") in rig.system.calls
    assert ("ip", "-j", "-4", "route", "show", "table", "all") in rig.system.calls
    assert ("ip", "-j", "-6", "route", "show", "table", "all") in rig.system.calls


def test_split_default_routes_do_not_take_every_subnet(rig: Rig) -> None:
    rig.system.routes4 += [{"dst": "0.0.0.0/1", "dev": "test-vpn0"},
                           {"dst": "128.0.0.0/1", "dev": "test-vpn0"}]
    rig.init()
    assert rig.state()["backend_subnet"] == FIRST_SUBNET


def test_a_host_without_ipv6_routes_is_fine(rig: Rig) -> None:
    rig.system.failures[("ip", "-j", "-6")] = (2, "", "IPv6 is disabled")
    rig.init()
    assert rig.state()["backend_subnet"] == FIRST_SUBNET


def test_the_subnet_falls_back_to_the_second_candidate_range(rig: Rig) -> None:
    rig.system.routes4.append({"dst": "10.213.0.0/20", "dev": "test-vpn0"})
    rig.init()
    assert rig.state()["backend_subnet"] == "172.29.0.0/24"


def test_no_free_subnet_is_refused_naming_what_takes_them(rig: Rig) -> None:
    rig.system.routes4 += [{"dst": "10.213.0.0/20", "dev": "test-vpn0"},
                           {"dst": "172.29.0.0/20", "dev": "test-vpn1"}]
    rig.refused(*INIT, match="free")
    assert "10.213.0.0/20" in rig.stderr.getvalue() and "test-vpn1" in rig.stderr.getvalue()
    assert "--backend-subnet" in rig.stderr.getvalue()
    assert not rig.system.commands(*PODMAN, "pull")


def test_a_chosen_backend_subnet_is_checked_like_the_candidates(rig: Rig) -> None:
    rig.system.routes4.append({"dst": "192.168.77.0/24", "dev": "test-vpn0"})
    rig.refused(*INIT, "--backend-subnet", "192.168.77.0/24", match="test-vpn0")
    rig.refused(*INIT, "--backend-subnet", "8.8.8.0/24", match="backend-subnet")

    assert rig(*INIT, "--backend-subnet", "192.168.78.0/24") == 0, rig.output()
    assert rig.state()["backend_subnet"] == "192.168.78.0/24"


@pytest.mark.parametrize("command", ["install", "upgrade"])
def test_a_backend_network_with_a_stale_subnet_is_refused(rig: Rig, command: str) -> None:
    if command == "install":
        rig.init()
        argv: tuple[str, ...] = ("install",)
    else:
        rig.install()
        argv = ("upgrade", "--release", SAMPLE_OTHER_RELEASE)
    rig.system.networks[f"{SAMPLE_PREFIX}backend"] = {
        "name": f"{SAMPLE_PREFIX}backend", "id": "7" * 64,
        "subnets": [{"subnet": "10.213.9.0/24"}],
    }
    before = rig.state()

    rig.refused(*argv, match="10.213.9.0/24")
    assert rig.state()["release"] == before["release"]
    assert rig.state()["pending"] is None
    assert not rig.system.commands(*SYSTEMCTL, "--no-block")


def test_a_backend_network_with_the_recorded_subnet_is_kept(rig: Rig) -> None:
    rig.init()
    rig.system.networks[f"{SAMPLE_PREFIX}backend"] = {
        "name": f"{SAMPLE_PREFIX}backend", "id": "7" * 64,
        "subnets": [{"subnet": FIRST_SUBNET}],
    }
    assert rig("install") == 0, rig.output()


# -- objects and files the installer does not own ----------------------------------------------


@pytest.mark.parametrize("kind", ["volume", "network", "container"])
def test_init_refuses_existing_objects_that_carry_its_names(rig: Rig, kind: str) -> None:
    if kind == "volume":
        name = f"{SAMPLE_PREFIX}postgres-data"
        rig.system.volumes[name] = {"Name": name, "Labels": {}}
    elif kind == "network":
        name = f"{SAMPLE_PREFIX}edge"
        rig.system.networks[name] = {"name": name, "id": "6" * 64, "subnets": [], "labels": {}}
    else:
        name = f"{SAMPLE_PREFIX}web"
        rig.system.containers.append({"Id": "c" * 64, "Names": [name], "Ports": None})

    rig.refused(*INIT, match=name)
    assert not rig.secrets.exists() or not any(rig.secrets.iterdir())
    assert not rig.system.commands(*PODMAN, "volume", "rm")
    assert not rig.system.commands(*PODMAN, "network", "rm")


@pytest.mark.parametrize("argv", [
    ("--release", "latest"),
    ("--release", "sha-" + "0" * 39),
    ("--release", "test-run"),
    ("--registry", "Registry.Example.Test/aegis"),
    ("--registry", "registry.example.test/aegis/"),
    ("--name", "*.example.test"),
    ("--name", "aegis.example.test,other.example.test"),
    ("--address", "aegis.example.test"),
    ("--address", "::"),
])
def test_invalid_init_arguments_are_refused_before_anything_runs(
    rig: Rig, argv: tuple[str, ...],
) -> None:
    arguments = list(INIT)
    flag, value = argv
    arguments[arguments.index(flag) + 1] = value

    assert rig(*arguments) == 1
    assert not rig.system.calls
    assert not (rig.home / ".local").exists()


def test_init_needs_a_name(rig: Rig) -> None:
    with pytest.raises(SystemExit):
        rig("init", "--release", SAMPLE_RELEASE, "--https-port", str(SAMPLE_PORT))


# -- install -----------------------------------------------------------------------------------


def test_install_writes_each_unit_where_unit_directory_puts_it(rig: Rig) -> None:
    rig.install()
    expected = rendered_for(rig)
    state = rig.state()

    assert sorted(path.name for path in rig.user_units.iterdir()) == [SOCKET]
    assert sorted(path.name for path in rig.quadlet_units.iterdir()) == sorted(
        name for name in expected if not name.endswith(".socket")
    )
    for name, text in expected.items():
        path = unit_path(rig, name)
        assert path.read_text(encoding="utf-8") == text, name
        assert mode(path) == 0o644, name
        assert state["files"][str(path)] == {"kind": "unit", "sha256": digest(path)}, name
    assert "[Install]" in (rig.user_units / SOCKET).read_text(encoding="utf-8")
    assert state["installed"] is True
    assert state["volumes"] == sorted(VOLUMES) and state["networks"] == sorted(NETWORKS)
    assert set(state["units"]["generated"]) == GENERATED
    assert state["units"]["images"][f"{SAMPLE_PREFIX}web.service"] == expected_image("backend")


def test_every_volume_and_network_carries_the_install_token(rig: Rig) -> None:
    rig.install()
    token = rig.state()["install_token"]
    for path in rig.quadlet_units.iterdir():
        unit = parse_unit(path.read_text(encoding="utf-8"))
        section = {".volume": "Volume", ".network": "Network"}.get(path.suffix)
        if section is not None:
            assert unit.values(section, "Label") == [f"{INSTALL_LABEL}={token}"], path.name


def test_install_reloads_systemd_then_enables_the_socket(rig: Rig) -> None:
    rig.install()
    reload = (*SYSTEMCTL, "daemon-reload")
    enable = (*SYSTEMCTL, "enable", SOCKET)

    assert rig.system.calls.index(reload) < rig.system.calls.index(enable)
    # Quadlet services are enabled by their [Install] sections when systemd generates them.
    assert rig.system.commands(*SYSTEMCTL, "enable") == [enable]


def test_install_refuses_a_unit_file_it_did_not_write(rig: Rig) -> None:
    rig.init()
    foreign = rig.quadlet_units / f"{SAMPLE_PREFIX}web.container"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("[Container]\nImage=elsewhere\n", encoding="utf-8")

    rig.refused("install", match=r"web\.container exists but aegis-podman did not write it")
    assert foreign.read_text(encoding="utf-8") == "[Container]\nImage=elsewhere\n"
    assert [path.name for path in rig.quadlet_units.iterdir()] == [foreign.name]
    assert not rig.user_units.exists()
    assert not rig.system.commands(*SYSTEMCTL, "daemon-reload")


def test_init_already_refuses_a_unit_file_it_would_overwrite(rig: Rig) -> None:
    foreign = rig.user_units / SOCKET
    foreign.parent.mkdir(parents=True)
    foreign.write_text("[Socket]\nListenStream=1\n", encoding="utf-8")

    rig.refused(*INIT, match=r"caddy\.socket exists but aegis-podman did not write it")
    assert not rig.secrets.exists() or not any(rig.secrets.iterdir())


def test_install_refuses_a_recorded_unit_file_changed_since(rig: Rig) -> None:
    rig.install()
    path = rig.quadlet_units / f"{SAMPLE_PREFIX}web.container"
    edited = path.read_text(encoding="utf-8") + "# local edit\n"
    path.write_text(edited, encoding="utf-8")

    rig.refused("install", match="changed")
    assert path.read_text(encoding="utf-8") == edited


def test_install_refuses_a_symlink_in_place_of_a_unit(rig: Rig, tmp_path: Path) -> None:
    rig.install()
    path = rig.quadlet_units / f"{SAMPLE_PREFIX}web.container"
    target = tmp_path / "target"
    target.write_text("kept\n", encoding="utf-8")
    path.unlink()
    path.symlink_to(target)

    rig.refused("install", match="symbolic link")
    assert target.read_text(encoding="utf-8") == "kept\n"


def test_reinstalling_unchanged_units_is_allowed(rig: Rig) -> None:
    rig.install()
    before = {path.name: path.read_bytes() for path in rig.quadlet_units.iterdir()}
    assert rig("install") == 0, rig.output()
    assert {path.name: path.read_bytes() for path in rig.quadlet_units.iterdir()} == before


def test_install_checks_the_port_again_before_the_first_install(rig: Rig) -> None:
    rig.init()
    rig.system.listeners.add(SAMPLE_PORT)

    rig.refused("install", match="listen")
    assert not rig.quadlet_units.exists()


def test_install_needs_init(rig: Rig) -> None:
    rig.refused("install", match="init")


@pytest.mark.parametrize("tamper", ["edit", "extra", "missing"])
def test_install_refuses_a_release_tree_that_differs_from_its_record(
    rig: Rig, tamper: str,
) -> None:
    rig.init()
    template = rig.tree() / "quadlet" / "web.container"
    template.parent.chmod(0o700)
    template.chmod(0o600)
    if tamper == "edit":
        template.write_text(template.read_text(encoding="utf-8") + "# x\n", encoding="utf-8")
    elif tamper == "extra":
        (template.parent / "other.container").write_text("[Container]\n", encoding="utf-8")
    else:
        template.unlink()

    rig.refused("install", match="release tree")
    assert not rig.quadlet_units.exists()


def test_install_refuses_when_quadlet_generated_no_service(rig: Rig) -> None:
    rig.init()
    rig.system.units[f"{SAMPLE_PREFIX}backend-network.service"] = {"LoadState": "not-found"}

    rig.refused("install", match="quadlet")


# -- start, stop and status (review notes I2 and R3) -------------------------------------------


def test_start_queues_without_blocking_and_waits_until_healthy(rig: Rig) -> None:
    rig.install()
    rig.system.unhealthy = {f"{SAMPLE_PREFIX}web"}
    rig.clock.hook = rig.system.unhealthy.clear

    assert rig("start") == 0, rig.output()
    objects, start = rig.system.commands(*SYSTEMCTL, "--no-block", "start")
    # Quadlet's network and volume services first, so their failures surface early.
    assert set(objects[4:]) == GENERATED
    assert start[4] == SOCKET
    assert set(start[5:]) == LONG_RUNNING
    assert rig.clock.slept == [5.0]
    shows = rig.system.commands(*SYSTEMCTL, "show")
    assert shows and all("Job" in call[3] and "Result" in call[3] for call in shows)
    # Quadlet's network and volume services are watched too.
    assert set(shows[-1][4:]) >= GENERATED
    assert (*PODMAN, "container", "inspect", f"{SAMPLE_PREFIX}web") in rig.system.calls
    assert rig.state()["desired"] == "running"


def test_a_failed_network_service_fails_the_start_at_once_and_is_named(rig: Rig) -> None:
    """As systemd reports it: the network service failed, and every unit that needs it is
    simply inactive, with Result=success and no job left."""
    rig.install()
    network = f"{SAMPLE_PREFIX}backend-network.service"
    blocked = [f"{SAMPLE_PREFIX}{name}.service"
               for name in ("postgres", "migrate", "web", "operations", "indexer", "media")]
    rig.system.after_start = {
        network: {"ActiveState": "failed", "SubState": "failed", "Result": "exit-code"},
        **{unit: {"ActiveState": "inactive", "SubState": "dead"} for unit in blocked},
    }

    assert rig("start") == 1
    output = rig.output()
    assert rig.clock.slept == []
    assert re.search(rf"Failed: {re.escape(network)}\n", output)
    assert "Not started, because a unit they need failed:" in output
    assert all(unit in output for unit in blocked)
    assert f"journalctl --user -u {network}" in output
    assert f"systemctl --user reset-failed {network}" in output
    assert "aegis-podman start" in output


def test_a_unit_inactive_with_its_job_still_queued_is_not_a_failure(rig: Rig) -> None:
    rig.install()
    unit = f"{SAMPLE_PREFIX}web.service"
    rig.system.after_start = {unit: {"ActiveState": "inactive", "SubState": "dead", "Job": "17"}}

    def run_job() -> None:
        rig.system.units[unit] |= {"ActiveState": "active", "SubState": "running", "Job": ""}

    rig.clock.hook = run_job
    assert rig("start") == 0, rig.output()
    assert rig.clock.slept == [5.0]


def test_start_reports_a_socket_that_failed(rig: Rig) -> None:
    rig.install()
    unit = SOCKET  # for example, its port was taken after install
    rig.system.after_start = {
        unit: {"ActiveState": "failed", "SubState": "failed", "Result": "resources"},
    }

    assert rig("start") == 1
    output = rig.output()
    assert f"systemctl --user reset-failed {unit}" in output
    assert f"journalctl --user -u {unit}" in output
    assert rig.clock.slept == []


def test_a_retrying_unit_keeps_the_start_waiting_and_is_named_at_the_deadline(rig: Rig) -> None:
    rig.install()
    # Restart=on-failure between attempts: activating/auto-restart, with no job queued.
    unit = f"{SAMPLE_PREFIX}web.service"
    rig.system.after_start = {
        unit: {"ActiveState": "activating", "SubState": "auto-restart", "Result": "exit-code"},
    }

    assert rig("start", "--timeout", "30") == 1
    assert 30 <= sum(rig.clock.slept) <= 35
    assert "not ready within 30s" in rig.output()
    assert f"Failed: {unit}" in rig.output()


def test_start_gives_up_at_its_own_deadline(rig: Rig) -> None:
    rig.install()
    rig.system.unhealthy = {f"{SAMPLE_PREFIX}postgres"}

    assert rig("start", "--timeout", "30") == 1
    assert 30 <= sum(rig.clock.slept) <= 35
    assert "30" in rig.output()
    assert f"{SAMPLE_PREFIX}postgres.service" in rig.output()


def test_stop_queues_without_blocking_and_waits(rig: Rig) -> None:
    rig.started()
    assert rig("stop") == 0, rig.output()

    (stop,) = rig.system.commands(*SYSTEMCTL, "--no-block", "stop")
    assert stop[4] == SOCKET
    assert set(stop[5:]) == LONG_RUNNING | {MIGRATE}
    assert all(rig.system.unit(unit)["ActiveState"] == "inactive" for unit in stop[4:])
    assert rig.state()["desired"] == "stopped"


def test_status_shows_states_health_digests_and_recovery(rig: Rig) -> None:
    rig.started()
    rig.fresh_output()
    assert rig("status") == 0
    output = rig.stdout.getvalue()
    for role in ROLES:
        assert expected_image(role) in output
    assert re.search(
        rf"{SAMPLE_PREFIX}postgres\.service\s+active/running\s.*health=healthy\s+image=current",
        output,
    )
    assert f"{SAMPLE_PREFIX}backend-network.service" in output

    unit = f"{SAMPLE_PREFIX}edge-network.service"
    rig.system.units[unit] |= {"ActiveState": "failed", "SubState": "failed",
                               "Result": "exit-code"}
    rig.fresh_output()
    assert rig("status") == 3
    assert f"systemctl --user reset-failed {unit}" in rig.stdout.getvalue()


def test_status_of_a_stopped_stack_is_not_ok(rig: Rig) -> None:
    rig.started()
    assert rig("stop") == 0
    rig.fresh_output()
    assert rig("status") == 3
    assert "(stopped)" in rig.stdout.getvalue()


def test_status_before_install_says_what_to_do(rig: Rig) -> None:
    rig.init()
    assert rig("status") == 3
    assert "aegis-podman install" in rig.stdout.getvalue()


def test_status_reports_temporary_files_left_by_an_interrupted_run(rig: Rig) -> None:
    rig.started()
    leftover = rig.data / ".state.json.0123456789abcdef.tmp"
    leftover.write_text("{}", encoding="utf-8")
    rig.fresh_output()
    rig("status")
    assert str(leftover) in rig.stdout.getvalue()


def test_units_that_never_stop_are_reported_and_nothing_is_removed(rig: Rig) -> None:
    rig.started()

    def ignore_stop(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        return (0, "", "") if command[2:4] == ("--no-block", "stop") else None

    rig.system.intercept = ignore_stop
    assert rig("stop", "--timeout", "20") == 1
    assert "not stopped within 20s" in rig.output()
    rig.refused("uninstall", "--timeout", "20", match="did not stop")
    assert len(list(rig.quadlet_units.iterdir())) == len(rendered_for(rig)) - 1
    assert not rig.system.commands(*SYSTEMCTL, "disable")
    assert rig.state()["installed"] is True


def test_lifecycle_commands_need_an_installed_stack(rig: Rig) -> None:
    rig.init()
    for command in ("start", "stop", "ca-cert"):
        rig.refused(command, match="install")


# -- upgrade -----------------------------------------------------------------------------------


def test_upgrade_pins_the_new_digests_and_restarts_in_order(rig: Rig) -> None:
    rig.started()
    first = len(rig.system.calls)

    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    state = rig.state()
    assert state["release"] == SAMPLE_OTHER_RELEASE
    assert state["created"] == "2026-10-05T12:30:00+02:00"
    assert state["pending"] is None
    assert state["images"] == {role: expected_image(role, SAMPLE_OTHER_RELEASE) for role in ROLES}
    assert [entry["release"] for entry in state["history"]] == [
        SAMPLE_RELEASE, SAMPLE_OTHER_RELEASE,
    ]
    assert [entry["sequence"] for entry in state["history"]] == [1, 2]
    for name, text in rendered_for(rig, SAMPLE_OTHER_RELEASE).items():
        assert unit_path(rig, name).read_text(encoding="utf-8") == text, name
    # The previous release tree stays for rollback.
    assert (rig.tree() / "aegis-podman").is_file()
    assert (rig.tree(SAMPLE_OTHER_RELEASE) / "aegis-podman").is_file()
    assert f"AEGIS_PODMAN_RELEASE={SAMPLE_OTHER_RELEASE}\n" in rig.env_file.read_text(
        encoding="utf-8",
    )

    later = rig.system.calls[first:]
    reload = later.index((*SYSTEMCTL, "daemon-reload"))
    (restart,) = [call for call in later if call[2:4] == ("--no-block", "restart")]
    assert reload < later.index(restart)
    assert set(restart[4:]) == LONG_RUNNING
    assert rig.system.removed == rig.system.created and len(rig.system.created) == 2
    # Every container now runs the new release's image.
    assert rig.system.running_images[f"{SAMPLE_PREFIX}web"] == expected_image(
        "backend", SAMPLE_OTHER_RELEASE,
    )


def test_containers_left_on_the_old_image_are_not_ready_after_an_upgrade(rig: Rig) -> None:
    rig.started()

    def restart_does_nothing(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        return (0, "", "") if command[2:4] == ("--no-block", "restart") else None

    rig.system.intercept = restart_does_nothing
    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE, "--timeout", "10") == 1
    assert "not ready within 10s" in rig.output()
    assert f"image=stale ({expected_image('backend')})" in rig.output()


def test_upgrade_of_a_stopped_stack_reloads_but_starts_nothing(rig: Rig) -> None:
    rig.started()
    assert rig("stop") == 0
    first = len(rig.system.calls)

    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    later = rig.system.calls[first:]
    assert (*SYSTEMCTL, "daemon-reload") in later
    assert not [call for call in later if call[2:3] == ("--no-block",)]
    assert "aegis-podman start" in rig.output()
    assert rig.state()["release"] == SAMPLE_OTHER_RELEASE


def test_upgrade_refuses_an_older_release_unless_allowed(rig: Rig) -> None:
    rig.install()
    assert rig("upgrade", "--release", SAMPLE_THIRD_RELEASE) == 0, rig.output()

    # Never installed here, but older than the current release by its commit time.
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="older")
    assert rig.state()["release"] == SAMPLE_THIRD_RELEASE
    assert SAMPLE_OTHER_RELEASE not in rig.state()["releases"]

    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE, "--allow-downgrade") == 0
    state = rig.state()
    assert state["release"] == SAMPLE_OTHER_RELEASE
    assert [entry["sequence"] for entry in state["history"]] == [1, 2, 3]
    # Moving forward again after an allowed downgrade is an upgrade.
    assert rig("upgrade", "--release", SAMPLE_THIRD_RELEASE) == 0, rig.output()
    assert [entry["release"] for entry in rig.state()["history"]] == [
        SAMPLE_RELEASE, SAMPLE_THIRD_RELEASE, SAMPLE_OTHER_RELEASE, SAMPLE_THIRD_RELEASE,
    ]


@pytest.mark.parametrize("label", [
    None, "yesterday", "2026-10-09T10:00:00", "2026-13-01T00:00:00Z", "2026-10-09T10:00:00+24:00",
])
def test_a_release_that_cannot_be_ordered_needs_the_flag(rig: Rig, label: str | None) -> None:
    rig.install()
    rig.system.created_labels[SAMPLE_OTHER_RELEASE] = label

    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="cannot be ordered")
    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE, "--allow-downgrade") == 0


def test_a_current_release_without_a_label_cannot_order_the_next(rig: Rig) -> None:
    rig.system.created_labels[SAMPLE_RELEASE] = None
    rig.install()
    assert rig.state()["created"] is None

    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="current release")


def test_upgrading_to_the_current_release_changes_nothing(rig: Rig) -> None:
    rig.install()
    before = rig.state()
    calls = len(rig.system.calls)

    assert rig("upgrade", "--release", SAMPLE_RELEASE) == 0
    assert rig.state() == before
    assert rig.system.calls[calls:] == []


def test_upgrade_refuses_changed_units_and_keeps_the_release(rig: Rig) -> None:
    rig.install()
    path = rig.quadlet_units / f"{SAMPLE_PREFIX}postgres.container"
    path.write_text(path.read_text(encoding="utf-8") + "# edit\n", encoding="utf-8")

    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="changed")
    assert rig.state()["release"] == SAMPLE_RELEASE
    assert rig.state()["pending"] is None
    assert expected_image("backend") in (
        rig.quadlet_units / f"{SAMPLE_PREFIX}web.container"
    ).read_text(encoding="utf-8")
    assert not rig.system.commands(*SYSTEMCTL, "--no-block")


def test_upgrade_before_install_changes_only_the_record(rig: Rig) -> None:
    rig.init()
    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    assert rig.state()["release"] == SAMPLE_OTHER_RELEASE
    assert not rig.quadlet_units.exists()
    assert not [call for call in rig.system.commands(*SYSTEMCTL) if call[2] != "show-environment"]


def test_a_release_whose_tag_moved_is_refused(rig: Rig) -> None:
    rig.install()
    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()

    def moved(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[2:4] == ("image", "inspect") and "aegis-backend" in command[4]:
            moved_digest = "sha256:" + "5" * 64
            return 0, json.dumps([{
                "Id": "e" * 64, "Digest": moved_digest,
                "RepoDigests": [f"{SAMPLE_REGISTRY}/aegis-backend@{moved_digest}"],
            }]), ""
        return None

    rig.system.intercept = moved
    rig.refused("upgrade", "--release", SAMPLE_RELEASE, "--allow-downgrade", match="recorded")
    assert rig.state()["release"] == SAMPLE_OTHER_RELEASE


def drop_quarantine(tree: Path) -> None:
    """A later release that no longer has the quarantine volume."""
    script = tree / "aegis-podman"
    text = script.read_text(encoding="utf-8")
    script.write_text(text.replace('"quarantine.volume", ', "", 1), encoding="utf-8")
    (tree / "quadlet" / "quarantine.volume").unlink()
    media = tree / "quadlet" / "media.container"
    media.write_text("".join(
        line for line in media.read_text(encoding="utf-8").splitlines(keepends=True)
        if "quarantine.volume" not in line
    ), encoding="utf-8")


def test_units_a_release_drops_are_stopped_before_their_files_go(rig: Rig) -> None:
    rig.started()
    rig.system.release_hooks[SAMPLE_OTHER_RELEASE] = drop_quarantine
    dropped_file = rig.quadlet_units / f"{SAMPLE_PREFIX}quarantine.volume"
    dropped_unit = f"{SAMPLE_PREFIX}quarantine-volume.service"
    first = len(rig.system.calls)

    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    later = rig.system.calls[first:]
    stop = (*SYSTEMCTL, "--no-block", "stop", dropped_unit)
    assert later.index(stop) < later.index((*SYSTEMCTL, "daemon-reload"))
    assert not dropped_file.exists()
    assert str(dropped_file) not in rig.state()["files"]
    assert dropped_unit not in rig.state()["units"]["generated"]
    # Its volume stays recorded, so --purge-data still removes it.
    assert f"{SAMPLE_PREFIX}quarantine" in rig.state()["volumes"]


# -- a pending upgrade (review note I1) ---------------------------------------------------------


def assert_pending(rig: Rig, release: str) -> None:
    state = rig.state()
    assert state["pending"]["release"] == release
    assert state["release"] == SAMPLE_RELEASE
    assert f"aegis-podman upgrade --release {release}" in rig.stderr.getvalue()


def assert_finishes(rig: Rig, release: str) -> None:
    rig.system.failures.clear()
    rig.fresh_output()
    assert rig("status") == 3
    assert f"upgrade to {release} is pending" in rig.stdout.getvalue()
    for command in (("install",), ("start",)):
        rig.refused(*command, match=f"upgrade --release {release}")
    rig.refused("upgrade", "--release", SAMPLE_THIRD_RELEASE, match="did not finish")

    assert rig("upgrade", "--release", release) == 0, rig.output()
    state = rig.state()
    assert state["release"] == release and state["pending"] is None
    assert rig.system.running_images[f"{SAMPLE_PREFIX}web"] == expected_image("backend", release)
    for name, text in rendered_for(rig, release).items():
        assert unit_path(rig, name).read_text(encoding="utf-8") == text, name
    assert all("previous" not in record for record in state["files"].values())
    assert rig("status") == 0, rig.output()


def test_a_failed_reload_leaves_a_pending_upgrade_that_upgrade_finishes(rig: Rig) -> None:
    rig.started()
    rig.system.failures[(*SYSTEMCTL, "daemon-reload")] = (1, "", "Failed to reload")

    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="daemon-reload")
    assert_pending(rig, SAMPLE_OTHER_RELEASE)
    assert_finishes(rig, SAMPLE_OTHER_RELEASE)


def test_a_failed_restart_leaves_a_pending_upgrade_that_upgrade_finishes(rig: Rig) -> None:
    rig.started()
    rig.system.failures[(*SYSTEMCTL, "--no-block", "restart")] = (1, "", "Job failed")

    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="restart")
    assert_pending(rig, SAMPLE_OTHER_RELEASE)
    rig.fresh_output()
    # The old containers still run the old images: status says so.
    assert rig("status") == 3
    assert "image=current" in rig.stdout.getvalue()
    assert_finishes(rig, SAMPLE_OTHER_RELEASE)


def test_an_interrupted_unit_write_leaves_a_pending_upgrade_that_upgrade_finishes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.started()
    module = rig.installer
    real = module._write_atomic
    written: list[Path] = []

    def interrupted(path: Path, content: bytes, file_mode: int) -> None:
        if path.suffix == ".container":
            written.append(path)
            if len(written) == 3:
                raise KeyboardInterrupt
        real(path, content, file_mode)

    monkeypatch.setattr(module, "_write_atomic", interrupted)
    with pytest.raises(KeyboardInterrupt):
        rig("upgrade", "--release", SAMPLE_OTHER_RELEASE)
    monkeypatch.undo()
    assert "upgrade --abandon" in rig.stderr.getvalue()
    assert rig.state()["pending"]["release"] == SAMPLE_OTHER_RELEASE
    assert rig.state()["release"] == SAMPLE_RELEASE
    assert_finishes(rig, SAMPLE_OTHER_RELEASE)


def test_a_pending_upgrade_whose_images_are_gone_says_how_to_fetch_them(rig: Rig) -> None:
    rig.started()
    rig.system.failures[(*SYSTEMCTL, "daemon-reload")] = (1, "", "Failed to reload")
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="daemon-reload")
    rig.system.failures.clear()
    missing = expected_image("gateway", SAMPLE_OTHER_RELEASE)
    rig.system.missing_images.add(missing)

    rig.refused(
        "upgrade", "--release", SAMPLE_OTHER_RELEASE, match=f"podman pull {re.escape(missing)}",
    )


def add_spare_volume(tree: Path) -> None:
    """A later release that adds a volume, and with it a unit file and a Quadlet service."""
    script = tree / "aegis-podman"
    text = script.read_text(encoding="utf-8")
    script.write_text(
        text.replace('"quarantine.volume", ', '"quarantine.volume", "spare.volume", ', 1),
        encoding="utf-8",
    )
    (tree / "quadlet" / "spare.volume").write_text(
        "[Volume]\nVolumeName=@@PREFIX@@spare\nLabel=io.aegis.install=@@INSTALL_TOKEN@@\n",
        encoding="utf-8",
    )


def pending_with_a_new_unit(rig: Rig) -> Path:
    """A running stack whose upgrade to a release with an extra unit stopped at the reload."""
    rig.system.release_hooks[SAMPLE_OTHER_RELEASE] = add_spare_volume
    rig.started()
    rig.system.failures[(*SYSTEMCTL, "daemon-reload")] = (1, "", "Failed to reload")
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="daemon-reload")
    rig.system.failures.clear()
    spare = rig.quadlet_units / f"{SAMPLE_PREFIX}spare.volume"
    assert spare.is_file() and str(spare) in rig.state()["files"]
    return spare


def test_uninstall_clears_a_pending_upgrade_and_everything_it_wrote(rig: Rig) -> None:
    spare = pending_with_a_new_unit(rig)
    first = len(rig.system.calls)

    assert rig("uninstall") == 0, rig.output()
    stop = next(call for call in rig.system.calls[first:] if call[2:4] == ("--no-block", "stop"))
    assert f"{SAMPLE_PREFIX}spare-volume.service" in stop
    assert not spare.exists() and not any(rig.quadlet_units.iterdir())
    assert rig.state()["pending"] is None
    # Starting over works: install renders the current release again.
    assert rig("install") == 0, rig.output()
    for name, text in rendered_for(rig).items():
        assert unit_path(rig, name).read_text(encoding="utf-8") == text, name


def test_abandoning_a_pending_upgrade_reinstalls_the_current_release(rig: Rig) -> None:
    spare = pending_with_a_new_unit(rig)
    first = len(rig.system.calls)

    assert rig("upgrade", "--abandon") == 0, rig.output()
    later = rig.system.calls[first:]
    state = rig.state()
    assert state["pending"] is None and state["release"] == SAMPLE_RELEASE
    assert state["desired"] == "running"
    for name, text in rendered_for(rig).items():
        assert unit_path(rig, name).read_text(encoding="utf-8") == text, name
    assert not spare.exists() and str(spare) not in state["files"]
    stop = (*SYSTEMCTL, "--no-block", "stop", f"{SAMPLE_PREFIX}spare-volume.service")
    assert later.index(stop) < later.index((*SYSTEMCTL, "daemon-reload"))
    # Still running, on the current release's images.
    assert rig.system.running_images[f"{SAMPLE_PREFIX}web"] == expected_image("backend")
    assert rig("status") == 0, rig.output()
    assert rig("start") == 0, rig.output()


def test_abandoning_after_the_restart_was_queued_puts_the_current_images_back(
    rig: Rig,
) -> None:
    rig.started()
    real = rig.system

    def restarted_then_interrupted(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[2:4] == ("--no-block", "restart"):
            real.intercept = None
            real.systemctl(command[2:])  # systemd recreated the containers ...
            raise KeyboardInterrupt  # ... but the commit never happened
        return None

    real.intercept = restarted_then_interrupted
    with pytest.raises(KeyboardInterrupt):
        rig("upgrade", "--release", SAMPLE_OTHER_RELEASE)
    web = f"{SAMPLE_PREFIX}web"
    assert real.running_images[web] == expected_image("backend", SAMPLE_OTHER_RELEASE)
    first = len(real.calls)

    assert rig.state()["pending"]["restart_queued"] is True
    # The new release's migrate may already have changed the schema: going back is a downgrade.
    rig.refused("upgrade", "--abandon", match="allow-downgrade")
    assert real.calls[first:] == []
    assert "backup" in rig.stderr.getvalue()
    rig.fresh_output()
    assert rig("upgrade", "--abandon", "--allow-downgrade") == 0, rig.output()
    assert "backup" in rig.output()
    assert [call for call in real.calls[first:] if call[2:4] == ("--no-block", "restart")]
    assert real.running_images[web] == expected_image("backend")
    assert rig("status") == 0, rig.output()


def test_a_pending_upgrade_records_when_its_restart_was_queued(rig: Rig) -> None:
    pending_with_a_new_unit(rig)  # it stopped at the reload, before any restart
    assert rig.state()["pending"].get("restart_queued", False) is False

    rig.system.failures[(*SYSTEMCTL, "--no-block", "restart")] = (1, "", "Job failed")
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="restart")
    assert rig.state()["pending"]["restart_queued"] is True
    rig.system.failures.clear()
    rig.refused("upgrade", "--abandon", match="allow-downgrade")


def test_a_failed_abandon_says_what_to_do_next(rig: Rig) -> None:
    pending_with_a_new_unit(rig)
    rig.system.failures[(*SYSTEMCTL, "daemon-reload")] = (1, "", "Failed to reload")
    rig.fresh_output()

    rig.refused("upgrade", "--abandon", match="daemon-reload")
    message = rig.stderr.getvalue()
    assert f"aegis-podman upgrade --release {SAMPLE_OTHER_RELEASE}" in message
    assert "aegis-podman upgrade --abandon" in message
    assert rig.state()["pending"]["release"] == SAMPLE_OTHER_RELEASE


def test_an_interrupted_abandon_says_what_to_do_next(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_with_a_new_unit(rig)
    module = rig.installer
    real = module._write_atomic

    def interrupted(path: Path, content: bytes, file_mode: int) -> None:
        if path.suffix == ".container":
            raise KeyboardInterrupt
        real(path, content, file_mode)

    monkeypatch.setattr(module, "_write_atomic", interrupted)
    rig.fresh_output()
    with pytest.raises(KeyboardInterrupt):
        rig("upgrade", "--abandon")
    monkeypatch.undo()
    assert "aegis-podman upgrade --abandon" in rig.stderr.getvalue()
    assert rig.state()["pending"]["release"] == SAMPLE_OTHER_RELEASE
    assert rig("upgrade", "--abandon") == 0, rig.output()


def test_abandoning_leaves_a_stopped_stack_stopped(rig: Rig) -> None:
    rig.started()
    assert rig("stop") == 0
    rig.system.failures[(*SYSTEMCTL, "daemon-reload")] = (1, "", "Failed to reload")
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="daemon-reload")
    rig.system.failures.clear()
    first = len(rig.system.calls)

    assert rig("upgrade", "--abandon") == 0, rig.output()
    later = rig.system.calls[first:]
    assert (*SYSTEMCTL, "daemon-reload") in later
    assert not [call for call in later if call[2:4] in (("--no-block", "start"),
                                                        ("--no-block", "restart"))]
    assert rig.state()["desired"] == "stopped" and rig.state()["pending"] is None


def test_abandon_needs_a_pending_upgrade_and_excludes_a_release(rig: Rig) -> None:
    rig.install()
    rig.refused("upgrade", "--abandon", match="no upgrade is pending")
    with pytest.raises(SystemExit):
        rig("upgrade", "--abandon", "--release", SAMPLE_OTHER_RELEASE)
    with pytest.raises(SystemExit):
        rig("upgrade")


def test_a_pending_upgrade_points_at_finishing_and_abandoning(rig: Rig) -> None:
    pending_with_a_new_unit(rig)
    rig.refused("install", match="upgrade --abandon")


def test_resuming_an_upgrade_of_uninstalled_units_clears_the_pending_record(rig: Rig) -> None:
    pending_with_a_new_unit(rig)
    raw = rig.state() | {"installed": False}
    rig.state_path.write_text(json.dumps(raw), encoding="utf-8")

    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    assert rig.state()["pending"] is None
    assert rig.state()["release"] == SAMPLE_OTHER_RELEASE


# -- ca-cert -----------------------------------------------------------------------------------


def test_ca_cert_prints_one_validated_certificate(rig: Rig) -> None:
    rig.started()
    rig.fresh_output()

    assert rig("ca-cert") == 0, rig.output()
    assert rig.stdout.getvalue() == sample_certificate()
    assert rig.system.commands(*PODMAN, "exec") == [(
        *PODMAN, "exec", f"{SAMPLE_PREFIX}caddy", "cat",
        "/data/caddy/pki/authorities/local/root.crt",
    )]


@pytest.mark.parametrize("output", [
    "",
    "not a certificate\n",
    sample_certificate() * 2,
    sample_certificate() + "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n",
    "-----BEGIN CERTIFICATE-----\n!!!!\n-----END CERTIFICATE-----\n",
    "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n",
])
def test_ca_cert_refuses_anything_but_one_certificate(rig: Rig, output: str) -> None:
    rig.started()
    rig.system.ca_output = output
    rig.fresh_output()

    rig.refused("ca-cert", match="certificate")
    assert rig.stdout.getvalue() == ""


# -- bootstrap-password ------------------------------------------------------------------------


BOOTSTRAP = ("bootstrap-password", "--username", "admin", "--email", "admin@example.invalid")


def test_bootstrap_password_mirrors_the_web_unit_and_shows_a_new_password_once(
    rig: Rig,
) -> None:
    rig.started()
    rig.fresh_output()

    assert rig(*BOOTSTRAP) == 0, rig.output()
    password_file = rig.secrets / "bootstrap-password"
    assert mode(password_file) == 0o600
    password = password_file.read_text(encoding="ascii").strip()
    assert re.fullmatch(r"[0-9a-f]{64}", password)
    assert rig.stdout.getvalue().count(password) == 1
    assert "only" in rig.stdout.getvalue()
    assert password not in rig.stderr.getvalue()
    assert rig.state()["secrets"]["bootstrap-password"] == {
        "device": password_file.lstat().st_dev, "inode": password_file.lstat().st_ino,
        "ctime_ns": password_file.lstat().st_ctime_ns,
    }

    web = parse_unit(rendered_for(rig)[f"{SAMPLE_PREFIX}web.container"])
    (run,) = rig.system.commands(*PODMAN, "run")
    image = run.index(expected_image("backend"))
    options, command = run[4:image], run[image + 1:]
    assert run[2:4] == ("run", "--rm")
    assert command == (
        "python", "manage.py", "bootstrap_admin", "--username", "admin",
        "--email", "admin@example.invalid", "--password-file", "/run/secrets/bootstrap-password",
    )
    secret_binds = {
        volume for volume in web.values("Container", "Volume") if volume.startswith("/")
    }
    assert sorted(options) == sorted([
        "--pull=never",
        f"--network={SAMPLE_PREFIX}backend",
        "--user=10001:10001",
        *(f"--env={key}={value}" for key, value in environment_assignments(web).items()),
        *(f"--volume={volume}" for volume in secret_binds),
        f"--volume={password_file}:/run/secrets/bootstrap-password:ro",
        "--read-only",
        *(f"--tmpfs={value}" for value in web.values("Container", "Tmpfs")),
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--security-opt=unmask=/sys/devices/virtual/powercap",
        f"--gidmap=+g10001:@{SAMPLE_HOST_GID}:1",
    ])
    assert web.values("Container", "Tmpfs") == ["/tmp:size=64m,noexec,nosuid,nodev"]
    assert all(password not in word for word in run)
    assert (*PODMAN, "unshare", "chown", "-h", "10001:10001") in {
        call[:6] for call in rig.system.calls
    }

    identity = password_file.lstat().st_ino
    rig.fresh_output()
    assert rig(*BOOTSTRAP) == 0, rig.output()
    assert password not in rig.output()
    assert password_file.lstat().st_ino == identity


def test_bootstrap_password_refuses_a_web_unit_key_it_cannot_copy(rig: Rig) -> None:
    def add_mask(tree: Path) -> None:
        template = tree / "quadlet" / "web.container"
        text = template.read_text(encoding="utf-8")
        template.write_text(text.replace("ReadOnly=true\n", "ReadOnly=true\nMask=/proc/acpi\n", 1),
                            encoding="utf-8")

    rig.system.tree_hook = add_mask
    rig.started()

    rig.refused(*BOOTSTRAP, match="Mask")
    assert not rig.system.commands(*PODMAN, "run")
    assert not (rig.secrets / "bootstrap-password").exists()


def test_bootstrap_password_refuses_a_file_it_did_not_create(rig: Rig) -> None:
    rig.started()
    planted = rig.secrets / "bootstrap-password"
    planted.write_text("chosen\n", encoding="ascii")
    planted.chmod(0o600)

    rig.refused(*BOOTSTRAP, match="bootstrap-password")
    assert not rig.system.commands(*PODMAN, "run")
    assert planted.read_text(encoding="ascii") == "chosen\n"


def test_bootstrap_password_refuses_an_edited_web_unit(rig: Rig) -> None:
    rig.started()
    web = rig.quadlet_units / f"{SAMPLE_PREFIX}web.container"
    web.write_text(web.read_text(encoding="utf-8") + "# edited\n", encoding="utf-8")

    rig.refused(*BOOTSTRAP, match="changed")
    assert not rig.system.commands(*PODMAN, "run")
    assert not (rig.secrets / "bootstrap-password").exists()


def test_a_failed_bootstrap_is_reported(rig: Rig) -> None:
    rig.started()
    rig.system.failures[(*PODMAN, "run")] = (1, "", "CommandError: administrator bootstrap failed")

    rig.refused(*BOOTSTRAP, match="bootstrap failed")


@pytest.mark.parametrize(("flag", "value"), [
    ("--username", "-admin"), ("--username", "ad min"), ("--username", ""),
    ("--email", "admin"), ("--email", "-a@example.invalid"), ("--email", "a b@example.invalid"),
])
def test_bootstrap_password_validates_its_arguments(rig: Rig, flag: str, value: str) -> None:
    rig.started()
    values = {"--username": "admin", "--email": "admin@example.invalid", flag: value}
    # The joined form reaches the installer even for values that start with a dash.
    arguments = ["bootstrap-password", *(f"{key}={text}" for key, text in values.items())]

    assert rig(*arguments) == 1
    assert not rig.system.commands(*PODMAN, "run")
    assert not (rig.secrets / "bootstrap-password").exists()


def test_bootstrap_password_needs_an_installed_stack(rig: Rig) -> None:
    rig.init()
    rig.refused(*BOOTSTRAP, match="install")


# -- uninstall ---------------------------------------------------------------------------------


def test_uninstall_removes_only_recorded_unit_files(rig: Rig) -> None:
    rig.started()
    neighbour = rig.quadlet_units / "other-app.container"
    neighbour.write_text("[Container]\nImage=x\n", encoding="utf-8")
    socket_neighbour = rig.user_units / "other.socket"
    socket_neighbour.write_text("[Socket]\nListenStream=1\n", encoding="utf-8")
    first = len(rig.system.calls)

    assert rig("uninstall") == 0, rig.output()
    assert [path.name for path in rig.quadlet_units.iterdir()] == [neighbour.name]
    assert [path.name for path in rig.user_units.iterdir()] == [socket_neighbour.name]
    later = rig.system.calls[first:]
    stop = next(call for call in later if call[2:4] == ("--no-block", "stop"))
    assert set(stop[4:]) == LONG_RUNNING | {SOCKET, MIGRATE} | GENERATED
    disable = (*SYSTEMCTL, "disable", SOCKET)
    reload = (*SYSTEMCTL, "daemon-reload")
    assert later.index(stop) < later.index(disable) < later.index(reload)
    state = rig.state()
    assert state["installed"] is False
    assert not any(record["kind"] == "unit" for record in state["files"].values())
    assert sorted(path.name for path in rig.secrets.iterdir()) == sorted(SECRET_NAMES)
    assert not rig.system.commands(*PODMAN, "volume", "rm")
    assert not rig.system.commands(*PODMAN, "network", "rm")
    assert set(VOLUMES) <= set(rig.system.volumes)


def test_uninstall_refuses_a_changed_unit_unless_forced(rig: Rig) -> None:
    rig.started()
    path = rig.quadlet_units / f"{SAMPLE_PREFIX}web.container"
    path.write_text("# replaced\n", encoding="utf-8")

    rig.refused("uninstall", match="changed")
    assert len(list(rig.quadlet_units.iterdir())) == len(rendered_for(rig)) - 1

    assert rig("uninstall", "--force-recorded") == 0, rig.output()
    assert not any(rig.quadlet_units.iterdir())


def test_uninstall_finishes_when_the_socket_file_is_already_gone(rig: Rig) -> None:
    rig.started()
    socket_file = rig.user_units / SOCKET
    wants = rig.user_units / "sockets.target.wants"
    wants.mkdir()
    (wants / SOCKET).symlink_to(socket_file)
    (wants / "other.socket").symlink_to(rig.user_units / "other.socket")
    socket_file.unlink()

    assert rig("uninstall") == 0, rig.output()
    assert not rig.system.commands(*SYSTEMCTL, "disable")
    assert not any(rig.quadlet_units.iterdir())
    # Exactly the link enable made goes; anything else there stays.
    assert [path.name for path in wants.iterdir()] == ["other.socket"]


def test_a_failed_reload_after_uninstall_says_what_to_run(rig: Rig) -> None:
    rig.started()
    rig.system.failures[(*SYSTEMCTL, "daemon-reload")] = (1, "", "Failed to reload")

    rig.refused("uninstall", match="run systemctl --user daemon-reload yourself")
    assert not any(rig.quadlet_units.iterdir())


def test_uninstall_needs_a_record(rig: Rig) -> None:
    rig.refused("uninstall", match="nothing is recorded")


def test_purge_removes_labelled_objects_by_exact_name_and_every_record(rig: Rig) -> None:
    rig.started()
    rig.system.volumes[f"{SAMPLE_PREFIX}postgres-data-copy"] = {
        "Name": f"{SAMPLE_PREFIX}postgres-data-copy",
        "Labels": {INSTALL_LABEL: rig.state()["install_token"]},
    }
    rig.system.networks[f"{SAMPLE_PREFIX}backend-old"] = {
        "name": f"{SAMPLE_PREFIX}backend-old", "id": "4" * 64, "subnets": [], "labels": {},
    }

    assert rig("uninstall", "--purge-data") == 0, rig.output()
    removed_volumes = [call[-1] for call in rig.system.commands(*PODMAN, "volume", "rm")]
    removed_networks = [call[-1] for call in rig.system.commands(*PODMAN, "network", "rm")]
    assert sorted(removed_volumes) == sorted(VOLUMES)
    assert sorted(removed_networks) == sorted(NETWORKS)
    assert all(len(call) == 5 for call in rig.system.commands(*PODMAN, "volume", "rm"))
    assert set(rig.system.volumes) == {f"{SAMPLE_PREFIX}postgres-data-copy"}
    assert set(rig.system.networks) == {"podman", f"{SAMPLE_PREFIX}backend-old"}
    assert not rig.secrets.exists()
    assert not rig.state_path.exists()
    assert not rig.env_file.exists()
    assert not (rig.data / "releases").exists()
    assert not rig.data.exists()


def test_purge_works_after_a_stack_that_never_became_healthy(rig: Rig) -> None:
    rig.install()
    rig.system.unhealthy = {f"{SAMPLE_PREFIX}web"}
    assert rig("start", "--timeout", "10") == 1
    assert set(VOLUMES) <= set(rig.system.volumes)

    assert rig("uninstall", "--purge-data") == 0, rig.output()
    assert not set(VOLUMES) & set(rig.system.volumes)
    assert not set(NETWORKS) & set(rig.system.networks)


def test_purge_refuses_an_object_without_this_installs_label(rig: Rig) -> None:
    rig.started()
    rig.system.volumes[f"{SAMPLE_PREFIX}postgres-data"]["Labels"] = {INSTALL_LABEL: "f" * 32}

    rig.refused("uninstall", "--purge-data", match="postgres-data does not carry")
    assert f"podman volume rm {SAMPLE_PREFIX}postgres-data" in rig.stderr.getvalue()
    assert not rig.system.commands(*PODMAN, "volume", "rm")
    assert not rig.system.commands(*PODMAN, "network", "rm")
    assert sorted(path.name for path in rig.secrets.iterdir()) == sorted(SECRET_NAMES)
    assert rig.state_path.exists()


def test_a_replacement_secret_reusing_the_inode_is_not_the_recorded_one() -> None:
    # ext4 hands a deleted file's inode straight to the next new file, so device and inode
    # alone cannot tell a replaced secret from the original; the change time can.
    installer = load_installer()
    record = {"device": 7, "inode": 42, "ctime_ns": 1_000}
    original = os.stat_result((0o100600, 42, 7, 1, 0, 0, 65, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0, 1_000))
    replaced = os.stat_result((0o100600, 42, 7, 1, 0, 0, 8, 0, 0, 0, 0.0, 0.0, 0.0, 0, 0, 2_000))

    assert installer._same_file(original, record)
    assert not installer._same_file(replaced, record)


def test_purge_refuses_a_secret_that_was_replaced_unless_forced(rig: Rig) -> None:
    rig.started()
    path = rig.secrets / "db-web-password"
    path.unlink()
    path.write_text("rotated\n", encoding="ascii")

    rig.refused("uninstall", "--purge-data", match="db-web-password")
    assert path.exists()
    assert not rig.system.commands(*PODMAN, "volume", "rm")
    assert rig("uninstall", "--purge-data", "--force-recorded") == 0, rig.output()
    assert not path.exists()


def test_removals_stay_inside_the_installers_own_directories(rig: Rig, tmp_path: Path) -> None:
    rig.started()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep\n", encoding="utf-8")
    raw = rig.state()
    raw["files"][str(outside)] = {"kind": "config",
                                  "sha256": hashlib.sha256(b"keep\n").hexdigest()}
    rig.state_path.write_text(json.dumps(raw), encoding="utf-8")

    rig.refused("uninstall", "--purge-data", "--force-recorded", match="outside")
    assert outside.read_text(encoding="utf-8") == "keep\n"


def test_purge_removes_an_extraction_container_an_interrupted_run_left(rig: Rig) -> None:
    rig.started()
    real = rig.system

    def created_then_timed_out(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[2:3] == ("create",):
            real.intercept = None
            real.podman(command[2:])
            raise subprocess.TimeoutExpired(list(command), 300)
        return None

    real.intercept = created_then_timed_out
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="did not finish")
    leaked = real.created[-1]
    assert leaked not in real.removed

    assert rig("uninstall", "--purge-data") == 0, rig.output()
    assert leaked in real.removed


# -- roots (spec D5, D11; review notes for Task 5b) --------------------------------------------


UNLABELED = b"system_u:object_r:unlabeled_t:s0\x00"
# A deployment test's synthetic root: labelled for containers, never an original.
CONTAINER_FILE = b"unconfined_u:object_r:container_file_t:s0\x00"
HOME_LABEL = b"unconfined_u:object_r:user_home_t:s0:c1,c2\x00"
ROOT_ROLES = ("migrate", "web", "operations", "indexer", "media", "gateway")
READER_ROLES = ("operations", "indexer", "media", "gateway")
ROOT_SERVICES = tuple(f"{SAMPLE_PREFIX}{role}.service" for role in ROOT_ROLES)
DROP_IN = "50-aegis-roots.conf"
UDICA = "/usr/share/udica/templates"
PRIVATE_SOURCES = (
    ("ipv4", "10.0.0.0/8"), ("ipv4", "172.16.0.0/12"), ("ipv4", "192.168.0.0/16"),
    ("ipv4", "100.64.0.0/10"), ("ipv6", "fc00::/7"), ("ipv6", "fe80::/10"),
)
LABEL = ("chcon", "-h", "-t", "container_file_t")
RELOAD = (*SYSTEMCTL, "daemon-reload")


def make_root(rig: Rig, name: str = "photos", label: bytes = UNLABELED) -> Path:
    """An original root as the operator has it: group-only and labelled by the host."""
    root = rig.originals / name
    root.mkdir(parents=True)
    (root / "picture.jpg").write_bytes(b"jpeg")
    root.chmod(0o750)
    rig.xattrs[str(root)] = label
    rig.xattrs[str(root / "picture.jpg")] = label
    return root


def add_root(rig: Rig, root: Path | str, slot: str = "photos", *extra: str) -> int:
    return rig("roots", "add", "--slot", slot, "--source", str(root), *extra)


def run_host_setup(rig: Rig) -> None:
    """The host-setup script is written, and the operator runs it with sudo."""
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()
    rig.system.run_host_setup(module_path(rig))


def covered_root(rig: Rig, name: str = "photos") -> Path:
    """One root whose type the module covers: added, host setup run, added again."""
    root = make_root(rig, name)
    assert add_root(rig, root, name) == 4, rig.output()
    run_host_setup(rig)
    assert add_root(rig, root, name) == 0, rig.output()
    return root


def drop_in(rig: Rig, role: str) -> Path:
    return rig.quadlet_units / f"{SAMPLE_PREFIX}{role}.container.d" / DROP_IN


def module_path(rig: Rig) -> Path:
    return rig.data / "selinux" / "aegis_reader.cil"


def script_path(rig: Rig) -> Path:
    return rig.data / "host-setup.sh"


def mounts_config(rig: Rig) -> Path:
    return rig.config_directory / "mounts.toml"


def library(rig: Rig, release: str = SAMPLE_RELEASE) -> Path:
    return rig.data / "releases" / release / "lib"


def generation_files(rig: Rig, generation: str) -> tuple[Path, Path]:
    mounts = rig.data / "mounts"
    return mounts / f"manifest.{generation}.json", mounts / f"gateway.{generation}.attestation"


def aegisctl_calls(rig: Rig, since: int = 0) -> list[tuple[str, ...]]:
    return [call[len(AEGISCTL):] for call in rig.system.calls[since:]
            if call[:len(AEGISCTL)] == AEGISCTL]


def restarts(rig: Rig, since: int = 0) -> list[tuple[str, ...]]:
    return [call[4:] for call in rig.system.calls[since:]
            if call[:4] == (*SYSTEMCTL, "--no-block", "restart")]


def test_roots_add_reads_the_label_then_regenerates_the_module_and_stops_with_4(
    rig: Rig,
) -> None:
    rig.install()
    root = make_root(rig)
    first = len(rig.system.calls)

    assert add_root(rig, root) == 4, rig.output()
    state = rig.state()
    # Only the probe ran: no aegisctl, no extraction, no systemd, before the type is covered.
    assert [call[2:4] for call in rig.system.calls[first:]] == [
        ("run", "--rm"), ("container", "exists"),
    ]
    assert rig.xattr_reads[0] == (str(root), first)
    assert state["selinux_pending"] == ["unlabeled_t"] and state["selinux_covered"] == []
    assert state["roots"] == {}
    assert not drop_in(rig, "indexer").exists() and not mounts_config(rig).exists()
    module = module_path(rig)
    assert mode(module) == 0o644
    assert state["files"][str(module)] == {"kind": "selinux", "sha256": digest(module)}
    assert "(allow process unlabeled_t (dir (getattr open read search ioctl lock)))" in (
        module.read_text(encoding="utf-8")
    )
    output = rig.stdout.getvalue()
    assert "unlabeled_t" in output and str(module) in output
    assert "aegis-podman host-setup --zone" in output and "sudo sh" in output
    assert f"aegis-podman roots add --slot photos --source {root}\n" in output
    assert len(rig.system.probes) == 1

    # Rerunning before the host setup probes, stops the same way and records the type once.
    rig.fresh_output()
    assert add_root(rig, root) == 4
    assert len(rig.system.probes) == 2
    assert rig.state()["selinux_pending"] == ["unlabeled_t"]
    assert f"sudo sh {script_path(rig)}" in rig.stdout.getvalue()


def test_a_passing_probe_covers_the_type_and_aegisctl_runs_in_order(rig: Rig) -> None:
    rig.started()
    root = make_root(rig)
    assert add_root(rig, root) == 4
    run_host_setup(rig)
    first = len(rig.system.calls)
    rig.xattr_reads.clear()

    assert add_root(rig, root) == 0, rig.output()
    state = rig.state()
    assert state["selinux_covered"] == ["unlabeled_t"] and state["selinux_pending"] == []
    (generation,) = state["mounts"]["generations"]
    assert state["mounts"]["current"] == generation
    manifest, attestation = generation_files(rig, generation)
    config = str(mounts_config(rig))
    calls = aegisctl_calls(rig, first)
    output_dir = calls[3][calls[3].index("--output-dir") + 1]
    gid = str(SAMPLE_HOST_GID)
    assert calls == [
        ("mounts", "inspect", "--source", str(root)),
        ("mounts", "validate", "--config", config),
        ("mounts", "preflight", "--config", config, "--manifest", str(manifest),
         "--observer", "podman-run", "--observer-image", expected_image("gateway"),
         "--host-gid", gid, "--reader-type", "aegis_reader.process"),
        ("mounts", "render", "--format", "quadlet", "--config", config,
         "--manifest", str(manifest), "--gateway-attestation", str(attestation),
         "--unit-prefix", SAMPLE_PREFIX, "--output-dir", output_dir, "--host-gid", gid),
    ]
    assert Path(output_dir).parent == rig.data / "mounts" and not Path(output_dir).exists()
    later = rig.system.calls[first:]
    # The label is read first, then the probe runs, then the release's aegisctl is copied
    # out, then it runs.
    label_read = min(position for path, position in rig.xattr_reads if path == str(root))
    (probe,) = [index for index, call in enumerate(later) if call[2:4] == ("run", "--rm")]
    (create,) = [index for index, call in enumerate(later) if call[2:3] == ("create",)]
    assert label_read <= first + probe < first + create
    assert create < later.index((*AEGISCTL, *calls[0]))
    # After a fresh render: the manifest goes to the mapped container user, both artifacts
    # are labelled, then systemd reloads and the units the drop-ins changed restart.
    steps = [call for call in later if call[:4] == LABEL or call[2:4] == ("unshare", "chown")
             or call == RELOAD or call[2:4] == ("--no-block", "restart")]
    assert steps == [
        (*LABEL, str(manifest)),
        (*PODMAN, "unshare", "chown", "-h", "10001:0", str(manifest)),
        (*LABEL, str(attestation)),
        RELOAD,
        (*SYSTEMCTL, "--no-block", "restart", *ROOT_SERVICES),
    ]
    assert later.index((*AEGISCTL, *calls[3])) < later.index(steps[0])
    assert mode(manifest) == 0o600
    assert "ready" not in rig.stderr.getvalue()
    assert re.search("every unit is active", rig.stdout.getvalue())


def test_aegisctl_runs_on_the_host_python_with_only_the_release_library(rig: Rig) -> None:
    rig.environ = {"PATH": "/usr/bin", "PYTHONSTARTUP": "/tmp/evil.py", "PYTHONHOME": "/x",
                   "PYTHONPATH": "/elsewhere", "AEGIS_CONTAINER_ENGINE": "docker"}
    rig.install()
    covered_root(rig)

    runs = rig.system.aegisctl_runs
    assert [command[len(AEGISCTL) + 1] for command, _ in runs] == [
        "inspect", "validate", "preflight", "render",
    ]
    for command, environment in runs:
        # -I would drop PYTHONPATH; -P, -S and no other PYTHON* variable isolate the same way,
        # with no site-packages or .pth files either.
        assert command[:len(AEGISCTL)] == (sys.executable, "-B", "-P", "-S", "-m", "aegisctl")
        assert environment == {"PATH": "/usr/bin", "PYTHONPATH": str(library(rig)),
                               "AEGIS_CONTAINER_ENGINE": "podman"}
    # -B: running it wrote no bytecode into the recorded copy.
    assert not list(library(rig).rglob("__pycache__"))


def test_roots_add_copies_the_release_aegisctl_out_by_digest_and_records_it(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    state = rig.state()

    creates = rig.system.commands(*PODMAN, "create")
    assert len(creates) == 2 and creates[1][6:] == (expected_image("backend"), "python3")
    copy = rig.system.commands(*PODMAN, "cp")[-1]
    assert copy[3] == f"{rig.system.created[1]}:/app/backend/aegisctl"
    assert rig.system.removed == rig.system.created
    assert rig.system.commands(*PODMAN, "rm")[-1] == (*PODMAN, "rm", rig.system.created[1])
    recorded = {path: record for path, record in state["files"].items()
                if record["kind"] == "lib"}
    on_disk = {str(path) for path in library(rig).rglob("*") if path.is_file()}
    assert set(recorded) == on_disk
    assert str(library(rig) / "aegisctl" / "mounts.py") in on_disk
    assert all(record == {"kind": "lib", "release": SAMPLE_RELEASE, "sha256": digest(Path(path))}
               for path, record in recorded.items())
    assert mode(library(rig) / "aegisctl") == 0o700
    assert mode(library(rig) / "aegisctl" / "cli.py") == 0o600

    # A second root verifies the recorded copy instead of copying it again.
    assert add_root(rig, make_root(rig, "music"), "music") == 0, rig.output()
    assert len(rig.system.commands(*PODMAN, "create")) == 2


def test_a_tampered_library_is_refused(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    script = library(rig) / "aegisctl" / "mounts.py"
    script.write_text(script.read_text(encoding="utf-8") + "# changed\n", encoding="utf-8")
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "music", "--source", str(make_root(rig, "music")),
                match="differs from its record")
    assert aegisctl_calls(rig, first) == []


def test_the_drop_ins_are_recorded_and_bind_exactly_what_each_role_needs(rig: Rig) -> None:
    rig.install()
    root = covered_root(rig)
    state = rig.state()
    (generation,) = state["mounts"]["generations"]
    manifest, attestation = generation_files(rig, generation)

    assert sorted(path.parent.name for path in rig.quadlet_units.glob("*.container.d/*")) == \
        sorted(f"{SAMPLE_PREFIX}{role}.container.d" for role in ROOT_ROLES)
    for role in ROOT_ROLES:
        path = drop_in(rig, role)
        assert mode(path) == 0o644, role
        assert state["files"][str(path)] == {"kind": "drop-in", "sha256": digest(path)}, role
        unit = parse_unit(path.read_text(encoding="ascii"))
        volumes = unit.values("Container", "Volume")
        environment = environment_assignments(unit)
        if role == "gateway":
            assert f"{attestation}:/run/aegis/mounts.gateway.attestation:ro" in volumes
            assert environment["AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256"] == digest(attestation)
        else:
            assert f"{manifest}:/run/aegis/mounts.manifest.json:ro" in volumes
            assert environment["AEGIS_MOUNT_MANIFEST_SHA256"] == digest(manifest)
        assert (f"{root}:/srv/aegis/roots/photos:ro" in volumes) == (role in READER_ROLES)
        assert ("aegisctl mounts attest" in (unit.value("Container", "Exec") or "")) == (
            role in ("operations", "indexer", "media")
        ), role
    info = manifest.lstat()
    assert state["mounts"]["generations"][generation] == {
        "manifest": {"device": info.st_dev, "inode": info.st_ino, "ctime_ns": info.st_ctime_ns},
        "attestation": digest(attestation),
    }
    assert mode(attestation) == 0o644
    assert str(attestation) in rig.system.labelled
    # Handed over: the eight secrets (under their temporary names) and this manifest only.
    others = {path: owner for path, owner in rig.system.chowned.items()
              if Path(path).parent != rig.secrets}
    assert others == {str(manifest): "10001:0"}
    assert len(rig.system.chowned) == len(SECRET_NAMES) + 1
    assert not [path for path in [*rig.system.labelled, *rig.system.chowned]
                if Path(path).is_relative_to(rig.originals)]


def test_the_mounts_config_is_private_and_written_from_the_record(rig: Rig) -> None:
    rig.install()
    root = covered_root(rig)
    state = rig.state()
    identity = f"local:{root.stat().st_dev}:{root.stat().st_ino}"

    config = mounts_config(rig)
    assert mode(config) == 0o600
    assert state["files"][str(config)] == {"kind": "mounts-config", "sha256": digest(config)}
    assert tomllib.loads(config.read_text(encoding="ascii")) == {"version": 1, "slots": [{
        "slot_id": "photos", "source": str(root), "container_path": "/srv/aegis/roots/photos",
        "mode": "read_only", "expected_identity": identity,
    }]}
    assert state["roots"] == {
        "photos": {"source": str(root), "identity": identity, "selinux_type": "unlabeled_t"},
    }


def test_a_remote_identity_is_kept_as_given(rig: Rig) -> None:
    rig.install()
    root = make_root(rig)
    identity = "remote:nas.example.test:/volume1/photos"
    assert add_root(rig, root, "photos", "--identity", identity) == 4
    run_host_setup(rig)
    assert add_root(rig, root, "photos", "--identity", identity) == 0, rig.output()
    assert rig.state()["roots"]["photos"]["identity"] == identity
    assert identity in mounts_config(rig).read_text(encoding="ascii")


def test_a_local_identity_other_than_the_source_is_refused(rig: Rig) -> None:
    rig.install()
    root = covered_root(rig)
    other = make_root(rig, "music")
    first = len(rig.system.calls)
    rig.refused("roots", "add", "--slot", "music", "--source", str(other),
                "--identity", f"local:{root.stat().st_dev}:{root.stat().st_ino}",
                match=f"{other} has the identity local:")
    assert "music" not in rig.state()["roots"]
    # Refused before any observation.
    assert aegisctl_calls(rig, first) == []


@pytest.mark.parametrize("source", [
    "relative/photos", "/srv/a/../photos", "/srv/photos/", "/srv/pho,tos", "/srv/pho tos",
    "/srv/pho:tos", '/srv/pho"tos', "/srv/pho'tos", "/srv/pho\\tos", "/srv/pho%tos",
    "/srv/pho$tos", "/srv/pho\ntos", "/srv/pho\ttos", "/srv/phötos",
])
def test_roots_add_refuses_an_unsafe_source_before_anything_runs(rig: Rig, source: str) -> None:
    rig.install()
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "photos", "--source", source, match="--source")
    assert rig.system.calls[first:] == [] and rig.xattr_reads == []
    assert rig.state()["roots"] == {} and not module_path(rig).exists()


def test_roots_add_refuses_a_missing_source_and_never_creates_it(rig: Rig) -> None:
    rig.install()
    missing = rig.originals / "missing"

    rig.refused("roots", "add", "--slot", "photos", "--source", str(missing),
                match="does not exist")
    assert not missing.exists() and not rig.originals.exists()


@pytest.mark.parametrize(("kind", "message"), [
    ("file", "not a directory"), ("symlink", "symbolic link"),
])
def test_roots_add_refuses_a_file_or_a_link(rig: Rig, kind: str, message: str) -> None:
    rig.install()
    target = make_root(rig)
    source = rig.originals / "other"
    if kind == "file":
        source.write_text("x", encoding="utf-8")
    else:
        source.symlink_to(target)
    rig.xattrs[str(source)] = UNLABELED

    rig.refused("roots", "add", "--slot", "other", "--source", str(source), match=message)
    assert rig.xattr_reads == []


@pytest.mark.parametrize("where", ["data", "secrets", "config", "units", "home"])
def test_roots_add_refuses_a_source_that_overlaps_the_installers_directories(
    rig: Rig, where: str,
) -> None:
    rig.install()
    source = {
        "data": rig.data / "photos", "secrets": rig.secrets, "config": rig.config_directory,
        "units": rig.quadlet_units / "photos", "home": rig.home,
    }[where]
    source.mkdir(exist_ok=True)
    rig.xattrs[str(source)] = UNLABELED

    rig.refused("roots", "add", "--slot", "photos", "--source", str(source), match="overlaps")
    assert rig.state()["selinux_pending"] == []


def test_roots_add_refuses_a_source_that_overlaps_another_root(rig: Rig) -> None:
    rig.install()
    root = covered_root(rig)
    inner = root / "inner"
    inner.mkdir()
    rig.xattrs[str(inner)] = UNLABELED

    rig.refused("roots", "add", "--slot", "inner", "--source", str(inner),
                match="overlaps the root photos")
    rig.refused("roots", "add", "--slot", "again", "--source", str(root),
                match="overlaps the root photos")


def test_a_slot_keeps_its_source(rig: Rig) -> None:
    rig.install()
    root = covered_root(rig)
    rig.refused("roots", "add", "--slot", "photos", "--source", str(make_root(rig, "music")),
                match="already uses")

    # The same slot and source again renders afresh, which also recovers a failed run.
    first = len(rig.system.calls)
    assert add_root(rig, root) == 0, rig.output()
    assert [call[1] for call in aegisctl_calls(rig, first)] == [
        "inspect", "validate", "preflight", "render",
    ]
    assert len(rig.state()["mounts"]["generations"]) == 1


@pytest.mark.parametrize("change", [
    ("--slot", "Photos"), ("--slot", "1photos"), ("--slot", "p" * 64), ("--slot", "pho_tos"),
    ("--identity", "local:1:0"), ("--identity", "local:x:1"), ("--identity", "elsewhere"),
    ("--identity", 'remote:nas.example.test:/a"b'),
])
def test_roots_add_refuses_an_invalid_slot_or_identity(
    rig: Rig, change: tuple[str, str],
) -> None:
    rig.install()
    root = make_root(rig)
    values = {"--slot": "photos", "--source": str(root), change[0]: change[1]}
    first = len(rig.system.calls)

    assert rig("roots", "add", *(f"{key}={value}" for key, value in values.items())) == 1
    assert change[0] in rig.stderr.getvalue()
    assert rig.system.calls[first:] == [] and rig.xattr_reads == []


@pytest.mark.parametrize(("label", "message"), [
    (b"unconfined_u:object_r:unconfined_t:s0\x00", "unconfined_t"),
    (b"system_u:system_r:spc_t:s0\x00", "spc_t"),
    (b"system_u:object_r:shell_exec_t:s0\x00", "shell_exec_t"),
    (b"system_u:object_r:shadow_t:s0\x00", "shadow_t"),
    (b"system_u:object_r:security_t:s0\x00", "security_t"),
    (b"system_u:object_r:Media-Type_t:s0\x00", "not an SELinux type"),
    (b"system_u:object_r:unlabeled:s0\x00", "not an SELinux type"),
    (b"system_u:object_r\x00", "label"),
    (b"system_u:object_r:a_t\x00:s0\x00", "label"),
    (b"\xff\xfe:object_r:media_t:s0", "label"),
    (None, "cannot read the SELinux label"),
])
def test_unusable_or_refused_labels_change_nothing(
    rig: Rig, label: bytes | None, message: str,
) -> None:
    rig.install()
    root = make_root(rig)
    if label is None:
        del rig.xattrs[str(root)]
    else:
        rig.xattrs[str(root)] = label
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match=message)
    assert rig.state()["selinux_pending"] == [] and not module_path(rig).exists()
    assert rig.system.calls[first:] == []


PROBE_NAME = re.compile(r"aegis-probe-[0-9a-f]{16}")
PROBE_LABEL = re.compile(r"aegis\.probe=[0-9a-f]{32}")


def pending_root(rig: Rig, name: str = "photos", label: bytes = UNLABELED) -> Path:
    """A root whose type the module file has, as the host setup has yet to load."""
    root = make_root(rig, name, label)
    assert add_root(rig, root, name) == 4, rig.output()
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()
    return root


def test_the_reader_probe_reads_the_root_as_the_reader_type_without_relabelling(
    rig: Rig,
) -> None:
    rig.install()
    root = pending_root(rig)
    rig.system.run_host_setup(module_path(rig))

    assert add_root(rig, root) == 0, rig.output()
    probe = rig.system.commands(*PODMAN, "run", "--rm", "--pull")[-1]
    name, label = probe[9], probe[11]
    assert PROBE_NAME.fullmatch(name) and PROBE_LABEL.fullmatch(label)
    assert probe == (
        *PODMAN, "run", "--rm", "--pull", "never", "--network", "none", "--name", name,
        "--label", label, "--security-opt", "label=type:aegis_reader.process",
        "--read-only", "--cap-drop", "all", "--security-opt", "no-new-privileges",
        "--user", "10001:10001", "--gidmap", f"+g10001:@{SAMPLE_HOST_GID}:1",
        "-v", f"{root}:/srv/aegis/probe:ro", "--entrypoint", "/bin/sh", expected_image("backend"),
        "-c", rig.installer.PROBE_SCRIPT, "aegis-probe", "picture.jpg",
    )
    state = rig.state()
    assert state["selinux_covered"] == ["unlabeled_t"] and state["selinux_pending"] == []
    # It ran to completion with --rm: nothing is left to clean up.
    assert not rig.system.commands(*PODMAN, "rm", "--force")


def test_a_failed_probe_keeps_the_type_pending_and_points_at_the_script(rig: Rig) -> None:
    rig.install()
    root = pending_root(rig)  # the module file covers it, but nothing loaded it
    first = len(rig.system.calls)
    rig.fresh_output()

    assert add_root(rig, root) == 4, rig.output()
    state = rig.state()
    assert state["selinux_pending"] == ["unlabeled_t"] and state["selinux_covered"] == []
    assert state["roots"] == {} and aegisctl_calls(rig, first) == []
    assert not rig.system.commands(*PODMAN, "create")[1:]
    output = rig.stdout.getvalue()
    assert "could not read" in output and f"sudo sh {script_path(rig)}" in output
    # The run left nothing behind, and the probe looked for anything it might have left.
    assert rig.system.commands(*PODMAN, "container", "exists")
    assert not rig.system.commands(*PODMAN, "rm", "--force")


def test_an_older_loaded_module_does_not_pass_the_probe_for_a_new_type(rig: Rig) -> None:
    rig.install()
    covered_root(rig)  # the loaded module grants unlabeled_t only
    music = pending_root(rig, "music", HOME_LABEL)

    # The reader type exists, but the module that grants user_home_t is not loaded yet.
    assert add_root(rig, music, "music") == 4, rig.output()
    assert rig.state()["selinux_pending"] == ["user_home_t"]
    rig.system.run_host_setup(module_path(rig))
    assert add_root(rig, music, "music") == 0, rig.output()
    assert rig.state()["selinux_covered"] == ["unlabeled_t", "user_home_t"]


def test_only_the_probed_type_moves_into_covered(rig: Rig) -> None:
    rig.install()
    photos = pending_root(rig)
    rig.system.run_host_setup(module_path(rig))  # a module with unlabeled_t only
    music = make_root(rig, "music", HOME_LABEL)
    assert add_root(rig, music, "music") == 4  # user_home_t joins the module file

    assert add_root(rig, photos) == 0, rig.output()
    state = rig.state()
    assert state["selinux_covered"] == ["unlabeled_t"]
    assert state["selinux_pending"] == ["user_home_t"]


def probe_left_behind(rig: Rig, labels: dict[str, str] | None = None) -> None:
    """A probe whose podman run never answered, with its container still there."""
    real = rig.system

    def timed_out(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[2:6] == ("run", "--rm", "--pull", "never"):
            name, label = command[9], command[11]
            key, _, token = label.partition("=")
            real.probe_containers[name] = {
                "Id": real.new_id(), "Name": name,
                "Config": {"Labels": labels if labels is not None else {key: token}},
            }
            raise subprocess.TimeoutExpired(list(command), 120)
        return None

    real.intercept = timed_out


def test_a_probe_that_times_out_is_removed_by_name_label_and_id(rig: Rig) -> None:
    rig.install()
    root = pending_root(rig)
    probe_left_behind(rig)
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match="did not finish")
    (probe,) = [call for call in rig.system.calls[first:] if call[2:4] == ("run", "--rm")]
    name = probe[9]
    identifier = rig.system.removed[-1]
    assert rig.system.calls[first + rig.system.calls[first:].index(probe) + 1:] == [
        (*PODMAN, "container", "exists", name),
        (*PODMAN, "container", "inspect", name),
        (*PODMAN, "rm", "--force", "--ignore", identifier),
    ]
    assert rig.system.probe_containers == {}
    assert rig.state()["selinux_pending"] == ["unlabeled_t"]


def test_a_container_without_the_probe_label_is_never_removed(rig: Rig) -> None:
    rig.install()
    root = pending_root(rig)
    probe_left_behind(rig, labels={"aegis.probe": "f" * 32})

    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match="label")
    assert len(rig.system.probe_containers) == 1
    assert not rig.system.commands(*PODMAN, "rm", "--force")


def test_the_module_installed_flag_is_gone(rig: Rig) -> None:
    rig.install()
    root = make_root(rig)
    with pytest.raises(SystemExit):
        add_root(rig, root, "photos", "--module-installed")


def test_the_type_comes_from_the_label_whatever_its_level(rig: Rig) -> None:
    rig.install()
    root = make_root(rig, label=HOME_LABEL)
    assert add_root(rig, root) == 4
    assert rig.state()["selinux_pending"] == ["user_home_t"]


def test_a_new_type_the_loaded_module_lacks_fails_the_probe_and_stops(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    music = make_root(rig, "music", HOME_LABEL)
    probes = len(rig.system.probes)

    assert add_root(rig, music, "music") == 4, rig.output()
    assert len(rig.system.probes) == probes + 1
    state = rig.state()
    assert state["selinux_covered"] == ["unlabeled_t"]
    assert state["selinux_pending"] == ["user_home_t"]
    assert "music" not in state["roots"]
    text = module_path(rig).read_text(encoding="utf-8")
    assert "unlabeled_t (dir" in text and "user_home_t (dir" in text


def test_a_root_relabelled_since_it_was_added_stops_a_rerender(rig: Rig) -> None:
    rig.install()
    root = covered_root(rig)
    music = make_root(rig, "music")
    rig.xattrs[str(root)] = HOME_LABEL
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "music", "--source", str(music),
                match="photos .* user_home_t")
    assert [call[1] for call in aegisctl_calls(rig, first)] == ["inspect"]
    assert "music" not in rig.state()["roots"]


def test_roots_need_an_installed_stack_and_no_pending_upgrade(rig: Rig) -> None:
    rig.init()
    root = make_root(rig)
    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match="install")
    assert rig("install") == 0, rig.output()
    rig.system.failures[RELOAD] = (1, "", "Failed to reload")
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="daemon-reload")
    rig.system.failures.clear()
    rig.refused("roots", "add", "--slot", "photos", "--source", str(root),
                match="upgrade --release")


def test_roots_add_refuses_a_drop_in_it_did_not_write(rig: Rig) -> None:
    rig.install()
    root = make_root(rig)
    assert add_root(rig, root) == 4
    run_host_setup(rig)
    foreign = drop_in(rig, "indexer")
    foreign.parent.mkdir()
    foreign.write_text("[Container]\nVolume=/elsewhere:/srv/x:ro\n", encoding="utf-8")
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match="did not write")
    assert foreign.read_text(encoding="utf-8") == "[Container]\nVolume=/elsewhere:/srv/x:ro\n"
    assert "preflight" not in [call[1] for call in aegisctl_calls(rig, first)]
    assert rig.state()["roots"] == {}


@pytest.mark.parametrize("damage", [
    "key", "extra-file", "missing-file", "relabel", "read-write", "other-source", "other-target",
    "environment", "exec-on-web",
])
def test_drop_ins_beyond_what_aegisctl_renders_are_refused(rig: Rig, damage: str) -> None:
    rig.install()
    root = make_root(rig)
    assert add_root(rig, root) == 4
    run_host_setup(rig)
    real = rig.system

    def tampered_render(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[:len(AEGISCTL)] != AEGISCTL or command[len(AEGISCTL) + 1] != "render":
            return None
        answer = real.aegisctl(command[len(AEGISCTL):], real.aegisctl_runs[-1][1])
        output = Path(command[command.index("--output-dir") + 1])
        unit = output / f"{SAMPLE_PREFIX}indexer.container.d" / DROP_IN
        text = unit.read_text(encoding="ascii")
        bind = f"Volume={root}:/srv/aegis/roots/photos:ro"
        changed = {
            "key": text + "PodmanArgs=--privileged\n",
            "relabel": text.replace(bind, bind + ",z"),
            "read-write": text.replace(bind, bind.removesuffix(":ro") + ":rw"),
            "other-source": text + "Volume=/etc:/srv/aegis/roots/etc:ro\n",
            "other-target": text.replace(bind, f"Volume={root}:/srv/elsewhere:ro"),
            "environment": text + "Environment=AEGIS_OTHER=1\n",
        }
        if damage in changed:
            assert changed[damage] != text, damage
            unit.write_text(changed[damage], encoding="ascii")
        elif damage == "exec-on-web":
            web = output / f"{SAMPLE_PREFIX}web.container.d" / DROP_IN
            web.write_text(web.read_text(encoding="ascii") + "Exec=/bin/sh\n", encoding="ascii")
        elif damage == "extra-file":
            (unit.parent / "60-more.conf").write_text("[Container]\n", encoding="ascii")
        else:
            unit.unlink()
        return answer

    real.intercept = tampered_render
    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match="drop-in")
    assert not drop_in(rig, "indexer").exists() and rig.state()["roots"] == {}
    assert not list((rig.data / "mounts").glob(".render.*"))


def refuse_preflight(command: tuple[str, ...]) -> tuple[int, str, str] | None:
    if command[:len(AEGISCTL)] == AEGISCTL and command[len(AEGISCTL) + 1] == "preflight":
        message = "container mount observation failed; diagnostics retained at /tmp/test-x"
        return 2, "", json.dumps({"status": "error", "message": message}) + "\n"
    return None


def test_a_failed_preflight_leaves_the_installed_roots_as_they_were(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    before = {role: drop_in(rig, role).read_bytes() for role in ROOT_ROLES}
    (generation,) = rig.state()["mounts"]["generations"]
    music = make_root(rig, "music")
    rig.system.intercept = refuse_preflight
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "music", "--source", str(music),
                match="observation failed")
    assert {role: drop_in(rig, role).read_bytes() for role in ROOT_ROLES} == before
    state = rig.state()
    assert list(state["roots"]) == ["photos"] and state["mounts"]["current"] == generation
    assert all(path.exists() for path in generation_files(rig, generation))
    assert RELOAD not in rig.system.calls[first:] and restarts(rig, first) == []

    rig.system.intercept = None
    assert add_root(rig, music, "music") == 0, rig.output()
    # The failed attempt's generation, and the one before, are gone.
    (current,) = rig.state()["mounts"]["generations"]
    assert sorted(path.name for path in (rig.data / "mounts").iterdir()) == sorted(
        path.name for path in generation_files(rig, current)
    )


def test_another_root_rerenders_every_drop_in_and_prunes_the_previous_artifacts(
    rig: Rig,
) -> None:
    rig.install()
    photos = covered_root(rig)
    (old,) = rig.state()["mounts"]["generations"]
    music = make_root(rig, "music")

    assert add_root(rig, music, "music") == 0, rig.output()
    (new,) = rig.state()["mounts"]["generations"]
    assert new != old
    assert not any(path.exists() for path in generation_files(rig, old))
    volumes = parse_unit(drop_in(rig, "indexer").read_text(encoding="ascii")).values(
        "Container", "Volume",
    )
    assert f"{photos}:/srv/aegis/roots/photos:ro" in volumes
    assert f"{music}:/srv/aegis/roots/music:ro" in volumes
    assert sorted(rig.state()["roots"]) == ["music", "photos"]
    assert all("previous" not in record for record in rig.state()["files"].values())


def test_a_stopped_stack_restarts_nothing_and_says_when_roots_apply(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    assert restarts(rig) == []
    assert "next aegis-podman start" in rig.stdout.getvalue()


def test_roots_remove_rerenders_without_the_slot(rig: Rig) -> None:
    rig.started()
    photos = covered_root(rig)
    music = make_root(rig, "music")
    assert add_root(rig, music, "music") == 0, rig.output()
    first = len(rig.system.calls)

    assert rig("roots", "remove", "--slot", "music") == 0, rig.output()
    assert [call[1] for call in aegisctl_calls(rig, first)] == ["validate", "preflight", "render"]
    assert list(rig.state()["roots"]) == ["photos"]
    volumes = parse_unit(drop_in(rig, "media").read_text(encoding="ascii")).values(
        "Container", "Volume",
    )
    assert f"{photos}:/srv/aegis/roots/photos:ro" in volumes
    assert not any("/srv/aegis/roots/music" in volume for volume in volumes)
    assert "music" not in mounts_config(rig).read_text(encoding="ascii")
    later = rig.system.calls[first:]
    assert later.index(RELOAD) < later.index((*SYSTEMCTL, "--no-block", "restart", *ROOT_SERVICES))


def test_removing_the_last_root_deletes_its_files_instead_of_rendering(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    (generation,) = rig.state()["mounts"]["generations"]
    first = len(rig.system.calls)

    assert rig("roots", "remove", "--slot", "photos") == 0, rig.output()
    assert aegisctl_calls(rig, first) == []
    state = rig.state()
    for role in ROOT_ROLES:
        assert not drop_in(rig, role).parent.exists(), role
        assert str(drop_in(rig, role)) not in state["files"], role
    assert not any(path.exists() for path in generation_files(rig, generation))
    assert not mounts_config(rig).exists() and str(mounts_config(rig)) not in state["files"]
    assert state["roots"] == {}
    assert state["mounts"] == {"current": None, "installing": None, "generations": {}}
    # No root uses the type any more, so the next host setup loads a module without it.
    assert state["selinux_covered"] == []
    later = rig.system.calls[first:]
    assert later.index(RELOAD) < later.index((*SYSTEMCTL, "--no-block", "restart", *ROOT_SERVICES))
    assert rig("status") == 0, rig.output()


def test_roots_remove_refuses_an_unknown_slot(rig: Rig) -> None:
    rig.install()
    rig.refused("roots", "remove", "--slot", "photos", match="no root")
    rig.refused("roots", "remove", "--slot", "Photos", match="--slot")


def test_upgrade_rerenders_the_drop_ins_with_the_new_release_aegisctl(rig: Rig) -> None:
    rig.started()
    root = covered_root(rig)
    (old,) = rig.state()["mounts"]["generations"]
    runs = len(rig.system.aegisctl_runs)
    first = len(rig.system.calls)

    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    new_runs = rig.system.aegisctl_runs[runs:]
    assert [command[len(AEGISCTL) + 1] for command, _ in new_runs] == [
        "validate", "preflight", "render",
    ]
    assert {environment["PYTHONPATH"] for _, environment in new_runs} == {
        str(library(rig, SAMPLE_OTHER_RELEASE)),
    }
    preflight = new_runs[1][0]
    assert preflight[preflight.index("--observer-image") + 1] == expected_image(
        "gateway", SAMPLE_OTHER_RELEASE,
    )
    copied = rig.system.created_from[rig.system.created[-1]]
    assert copied == expected_image("backend", SAMPLE_OTHER_RELEASE)
    assert rig.system.commands(*PODMAN, "cp")[-1][3].endswith(":/app/backend/aegisctl")
    state = rig.state()
    (new,) = state["mounts"]["generations"]
    assert new != old and state["mounts"]["current"] == new
    assert not any(path.exists() for path in generation_files(rig, old))
    manifest, _ = generation_files(rig, new)
    assert f"{manifest}:/run/aegis/mounts.manifest.json:ro" in parse_unit(
        drop_in(rig, "web").read_text(encoding="ascii"),
    ).values("Container", "Volume")
    assert f"{root}:/srv/aegis/roots/photos:ro" in drop_in(rig, "gateway").read_text("ascii")
    assert all("previous" not in record for record in state["files"].values())
    later = rig.system.calls[first:]
    render = later.index(new_runs[2][0])
    assert render < later.index(RELOAD) < later.index(next(
        call for call in later if call[2:4] == ("--no-block", "restart")
    ))


def test_an_upgrade_whose_roots_do_not_render_changes_nothing(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    web = rig.quadlet_units / f"{SAMPLE_PREFIX}web.container"
    before = web.read_bytes()
    rig.system.intercept = refuse_preflight

    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="observation failed")
    assert rig.state()["pending"] is None and rig.state()["release"] == SAMPLE_RELEASE
    assert web.read_bytes() == before


def test_abandoning_an_upgrade_rerenders_with_the_current_release_aegisctl(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    rig.system.failures[RELOAD] = (1, "", "Failed to reload")
    rig.refused("upgrade", "--release", SAMPLE_OTHER_RELEASE, match="daemon-reload")
    rig.system.failures.clear()
    runs = len(rig.system.aegisctl_runs)

    assert rig("upgrade", "--abandon") == 0, rig.output()
    assert {environment["PYTHONPATH"] for _, environment in rig.system.aegisctl_runs[runs:]} == {
        str(library(rig)),
    }
    assert len(rig.state()["mounts"]["generations"]) == 1
    assert rig("status") == 0, rig.output()


def test_uninstall_removes_the_drop_ins_and_install_renders_them_again(rig: Rig) -> None:
    rig.started()
    root = covered_root(rig)
    first = len(rig.system.calls)

    assert rig("uninstall") == 0, rig.output()
    assert not any(DROP_IN in word for call in rig.system.calls[first:] for word in call)
    for role in ROOT_ROLES:
        assert not drop_in(rig, role).parent.exists(), role
    assert list(rig.state()["roots"]) == ["photos"]

    first = len(rig.system.calls)
    assert rig("install") == 0, rig.output()
    assert [call[1] for call in aegisctl_calls(rig, first)] == ["validate", "preflight", "render"]
    assert f"{root}:/srv/aegis/roots/photos:ro" in drop_in(rig, "indexer").read_text("ascii")
    assert len(rig.state()["mounts"]["generations"]) == 1
    # A plain reinstall keeps drop-ins that are in place.
    first = len(rig.system.calls)
    assert rig("install") == 0, rig.output()
    assert aegisctl_calls(rig, first) == []


def test_a_root_that_blocks_install_can_still_be_removed(rig: Rig) -> None:
    rig.started()
    photos = covered_root(rig)
    music = make_root(rig, "music")
    assert add_root(rig, music, "music") == 0, rig.output()
    assert rig("uninstall") == 0, rig.output()
    shutil.rmtree(music)  # for example, its disk is gone

    rig.refused("install", match="music")
    first = len(rig.system.calls)
    assert rig("roots", "remove", "--slot", "music") == 0, rig.output()
    # Nothing is installed, so only the record changes; install renders the rest.
    assert rig.system.calls[first:] == []
    assert list(rig.state()["roots"]) == ["photos"]
    assert rig("install") == 0, rig.output()
    assert f"{photos}:/srv/aegis/roots/photos:ro" in drop_in(rig, "indexer").read_text("ascii")

    assert rig("uninstall") == 0, rig.output()
    assert rig("roots", "remove", "--slot", "photos") == 0, rig.output()
    assert rig.state()["roots"] == {}
    assert rig.state()["mounts"] == {"current": None, "installing": None, "generations": {}}
    assert not any((rig.data / "mounts").iterdir()) and not mounts_config(rig).exists()


def test_purge_removes_every_root_artifact(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    assert script_path(rig).exists() and module_path(rig).exists()

    assert rig("uninstall", "--purge-data") == 0, rig.output()
    assert not rig.data.exists()
    assert not mounts_config(rig).exists()
    assert not list(rig.quadlet_units.glob("*.container.d"))


def test_status_shows_the_roots_and_types_waiting_for_the_host_setup(rig: Rig) -> None:
    rig.started()
    root = covered_root(rig)
    assert add_root(rig, make_root(rig, "music", HOME_LABEL), "music") == 4
    rig.fresh_output()

    rig("status")
    output = rig.stdout.getvalue()
    assert f"root photos: {root} (unlabeled_t)" in output
    assert "user_home_t" in output and "host-setup" in output


@pytest.mark.parametrize("damage", ["root", "type", "generation", "current"])
def test_a_record_with_malformed_roots_is_refused(tmp_path: Path, damage: str) -> None:
    installer = load_installer()
    paths = private_paths(installer, tmp_path)
    installer.save_state(paths, sample_state(installer))
    raw = json.loads(paths.state.read_text(encoding="utf-8"))
    raw |= {
        "root": {"roots": {"Bad Slot": {"source": "/x", "identity": "y", "selinux_type": "z_t"}}},
        "type": {"selinux_pending": ["Not A Type"]},
        "generation": {"mounts": {"current": None, "generations": {"../x": {
            "manifest": None, "attestation": None}}}},
        "current": {"mounts": {"current": "0" * 16, "generations": {}}},
    }[damage]
    paths.state.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(installer.InstallError, match="not usable"):
        installer.load_state(paths, os.getuid())


def bound_files(rig: Rig) -> set[str]:
    """Every file an installed drop-in binds."""
    return {
        volume.split(":")[0] for role in ROOT_ROLES if drop_in(rig, role).exists()
        for volume in parse_unit(drop_in(rig, role).read_text(encoding="ascii")).values(
            "Container", "Volume",
        )
        if volume.split(":")[1].startswith("/run/aegis/")
    }


def test_an_interrupted_drop_in_install_is_flagged_and_rendered_again_by_install(
    rig: Rig, monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig.started()
    covered_root(rig)
    music = make_root(rig, "music")
    module = rig.installer
    real = module._write_atomic
    written: list[Path] = []

    def no_space(path: Path, content: bytes, file_mode: int) -> None:
        if path.name == DROP_IN:
            written.append(path)
            if len(written) == 3:
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
        real(path, content, file_mode)

    monkeypatch.setattr(module, "_write_atomic", no_space)
    rig.refused("roots", "add", "--slot", "music", "--source", str(music), match="No space")
    monkeypatch.undo()
    state = rig.state()
    installing = state["mounts"]["installing"]
    assert installing is not None and installing != state["mounts"]["current"]
    # The record lists only what every reader mounts; the half-switched drop-ins are flagged.
    assert list(state["roots"]) == ["photos"]
    rig.fresh_output()
    assert rig("status") == 3
    assert "interrupted" in rig.stdout.getvalue()
    assert all(Path(path).exists() for path in bound_files(rig))

    first = len(rig.system.calls)
    assert rig("install") == 0, rig.output()
    assert [call[1] for call in aegisctl_calls(rig, first)] == ["validate", "preflight", "render"]
    state = rig.state()
    assert state["mounts"]["installing"] is None
    (generation,) = state["mounts"]["generations"]
    manifest, attestation = generation_files(rig, generation)
    assert bound_files(rig) == {str(manifest), str(attestation)}
    assert all(Path(path).exists() for path in bound_files(rig))
    assert "music" not in drop_in(rig, "indexer").read_text(encoding="ascii")
    # Adding it again finishes the job.
    assert add_root(rig, music, "music") == 0, rig.output()
    assert "/srv/aegis/roots/music" in drop_in(rig, "indexer").read_text(encoding="ascii")


def test_a_switch_systemd_never_loaded_is_rendered_again(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    assert rig("uninstall") == 0, rig.output()
    rig.system.failures[RELOAD] = (1, "", "Failed to reload")
    rig.refused("install", match="daemon-reload")
    rig.system.failures.clear()
    # Every drop-in is written, settled and binds the current generation; only the reload
    # is missing, which the installing marker alone records.
    state = rig.state()
    assert state["mounts"]["installing"] == state["mounts"]["current"] is not None
    assert all("previous" not in record for record in state["files"].values())

    first = len(rig.system.calls)
    assert rig("install") == 0, rig.output()
    assert [call[1] for call in aegisctl_calls(rig, first)] == ["validate", "preflight", "render"]
    assert rig.state()["mounts"]["installing"] is None


def test_drop_ins_binding_another_generation_than_the_record_are_rendered_again(
    rig: Rig,
) -> None:
    rig.install()
    covered_root(rig)
    raw = rig.state()
    (bound,) = raw["mounts"]["generations"]
    other = "0" * 16
    for source, target in zip(generation_files(rig, bound), generation_files(rig, other),
                              strict=True):
        shutil.copyfile(source, target)
    raw["mounts"]["generations"][other] = {"manifest": None, "attestation": None}
    raw["mounts"]["current"] = other
    rig.state_path.write_text(json.dumps(raw), encoding="utf-8")

    first = len(rig.system.calls)
    assert rig("install") == 0, rig.output()
    assert [call[1] for call in aegisctl_calls(rig, first)] == ["validate", "preflight", "render"]


def test_init_names_take_hostnames_or_ip_addresses() -> None:
    parser = load_installer()._parser()
    init = next(action for action in parser._subparsers._group_actions
                if action.dest == "command").choices["init"]
    (name,) = [action for action in init._actions if "--name" in action.option_strings]
    assert name.help == "hostname or IP address (repeatable; the first is canonical)"


@pytest.mark.parametrize("command", ["install", "upgrade"])
def test_an_interrupted_first_root_never_loses_a_file_its_drop_ins_bind(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, command: str,
) -> None:
    rig.started()
    root = pending_root(rig)
    rig.system.run_host_setup(module_path(rig))
    module = rig.installer
    real = module._write_atomic
    written: list[Path] = []

    def no_space(path: Path, content: bytes, file_mode: int) -> None:
        if path.name == DROP_IN:
            written.append(path)
            if len(written) == 3:
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
        real(path, content, file_mode)

    monkeypatch.setattr(module, "_write_atomic", no_space)
    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match="No space")
    monkeypatch.undo()
    state = rig.state()
    assert state["roots"] == {} and state["mounts"]["installing"] is not None
    assert drop_in(rig, "web").exists()
    rig.fresh_output()
    assert rig("status") == 3 and "interrupted" in rig.stdout.getvalue()

    # With no root recorded, nothing is rendered: the half-written drop-ins go, before
    # systemd reloads, so no unit ever binds a file that is pruned.
    at_reload: list[list[bool]] = []

    def watch(call: tuple[str, ...]) -> tuple[int, str, str] | None:
        if call == RELOAD:
            at_reload.append([drop_in(rig, role).exists() for role in ROOT_ROLES])
        return None

    rig.system.intercept = watch
    argv = ("install",) if command == "install" else ("upgrade", "--release", SAMPLE_OTHER_RELEASE)
    assert rig(*argv) == 0, rig.output()
    rig.system.intercept = None
    assert at_reload and not any(at_reload[0])
    assert not list(rig.quadlet_units.glob("*.container.d"))
    assert rig.state()["mounts"] == {"current": None, "installing": None, "generations": {}}
    assert rig("status") == 0, rig.output()
    assert add_root(rig, root) == 0, rig.output()


def test_pruning_keeps_every_generation_a_drop_in_on_disk_binds(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    raw = rig.state()
    (bound,) = raw["mounts"]["generations"]
    for other in ("1" * 16, "2" * 16):
        for source, target in zip(generation_files(rig, bound), generation_files(rig, other),
                                  strict=True):
            shutil.copyfile(source, target)
        raw["mounts"]["generations"][other] = {"manifest": None, "attestation": None}
    # The record claims "1" is current; the drop-ins on disk still bind the first one.
    raw["mounts"]["current"] = "1" * 16
    rig.state_path.write_text(json.dumps(raw), encoding="utf-8")
    installer = rig.installer
    paths = installer.Paths.resolve(rig.home, {})
    state = installer.load_state(paths, rig.uid)

    installer._prune_generations(rig.host(), paths, state)
    assert sorted(state.mounts["generations"]) == sorted([bound, "1" * 16])
    assert all(path.exists() for path in generation_files(rig, bound))
    assert not any(path.exists() for path in generation_files(rig, "2" * 16))


def run_probe(
    installer: Any, root: Path, *arguments: str,
) -> subprocess.CompletedProcess[str]:
    script = installer.PROBE_SCRIPT.replace(installer.PROBE_TARGET, str(root))
    return subprocess.run(["/bin/sh", "-c", script, "aegis-probe", *arguments], check=False,
                          capture_output=True, text=True, timeout=30,
                          stdin=subprocess.DEVNULL)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable files")
def test_the_probe_script_fails_unless_it_can_read_the_root(tmp_path: Path) -> None:
    installer = load_installer()
    root = tmp_path / "root"
    root.mkdir()
    (root / "picture.jpg").write_bytes(b"jpeg")
    (root / "locked.jpg").write_bytes(b"jpeg")
    (root / "locked.jpg").chmod(0o000)
    empty = tmp_path / "empty"
    empty.mkdir()
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir(mode=0o000)
    try:
        assert run_probe(installer, root).returncode == 0
        assert run_probe(installer, root, "picture.jpg").returncode == 0
        assert run_probe(installer, empty).returncode == 0
        assert run_probe(installer, tmp_path / "missing").returncode != 0
        assert run_probe(installer, unreadable).returncode != 0
        assert run_probe(installer, root, "locked.jpg").returncode != 0
    finally:
        unreadable.chmod(0o700)


def test_the_probe_opens_one_top_level_file_of_the_roots_own_type(rig: Rig) -> None:
    rig.install()
    root = pending_root(rig)
    (root / "picture.jpg").unlink()
    nested = root / "album"
    nested.mkdir()
    (nested / "deep.jpg").write_bytes(b"jpeg")
    rig.xattrs[str(nested / "deep.jpg")] = UNLABELED
    moved = root / "moved.jpg"  # mv keeps a file's label: another type, never a failure
    moved.write_bytes(b"jpeg")
    rig.xattrs[str(moved)] = HOME_LABEL
    rig.system.run_host_setup(module_path(rig))

    assert add_root(rig, root) == 0, rig.output()
    probe = rig.system.probes[-1]
    assert probe[-1] == "aegis-probe"  # no file of the root's type at the top: none opened

    other = make_root(rig, "music")
    same = other / "same.jpg"
    same.write_bytes(b"jpeg")
    rig.xattrs[str(same)] = UNLABELED
    (other / "picture.jpg").unlink()
    assert add_root(rig, other, "music") == 0, rig.output()
    # music's type is covered now, so it needs no probe; check the choice directly.
    assert rig.installer._probe_file(rig.host(), str(other), "unlabeled_t") == "same.jpg"


def test_the_probe_file_must_be_readable_through_the_group_bits_that_apply(
    tmp_path: Path,
) -> None:
    installer = load_installer()
    root = tmp_path / "root"
    root.mkdir()
    label = b"system_u:object_r:unlabeled_t:s0\x00"
    host = installer.Host(home=tmp_path, gid=os.getegid(),
                          getxattr=lambda path, name: label)
    mapped = root / "a-mapped-group.jpg"
    mapped.write_bytes(b"x")
    mapped.chmod(0o604)  # the mapped group's bits apply to it: no g+r, so never chosen

    assert installer._probe_file(host, str(root), "unlabeled_t") is None
    mapped.chmod(0o640)
    assert installer._probe_file(host, str(root), "unlabeled_t") == "a-mapped-group.jpg"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads unreadable directories")
def test_a_root_the_probe_cannot_read_stays_pending_even_with_the_module_loaded(
    rig: Rig,
) -> None:
    rig.install()
    root = pending_root(rig)
    rig.system.run_host_setup(module_path(rig))
    root.chmod(0o000)
    try:
        assert add_root(rig, root) == 4, rig.output()
        assert rig.state()["selinux_pending"] == ["unlabeled_t"]
    finally:
        root.chmod(0o750)


def test_a_type_forgotten_by_a_removal_is_proven_by_the_probe_again(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    music = pending_root(rig, "music", HOME_LABEL)
    rig.system.run_host_setup(module_path(rig))
    assert add_root(rig, music, "music") == 0, rig.output()
    assert rig("roots", "remove", "--slot", "music") == 0, rig.output()
    assert rig.state()["selinux_covered"] == ["unlabeled_t"]

    # The loaded module still grants it, so no second host setup is needed.
    assert add_root(rig, music, "music") == 0, rig.output()
    assert rig.state()["selinux_covered"] == ["unlabeled_t", "user_home_t"]


def test_a_type_blocked_since_it_was_recorded_is_dropped_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    installer = load_installer()
    paths = private_paths(installer, tmp_path)
    state = sample_state(installer)
    installer.save_state(paths, state)
    raw = json.loads(paths.state.read_text(encoding="utf-8"))
    raw |= {"selinux_covered": ["unlabeled_t", "etc_t"], "selinux_pending": ["proc_t"]}
    paths.state.write_text(json.dumps(raw), encoding="utf-8")

    loaded = installer.load_state(paths, os.getuid())
    assert loaded.selinux_covered == ["unlabeled_t"] and loaded.selinux_pending == []
    assert "etc_t" in capsys.readouterr().err


def test_install_refuses_a_drop_in_changed_since_it_was_written(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    path = drop_in(rig, "indexer")
    edited = path.read_text(encoding="ascii") + "Volume=/elsewhere:/srv/x:ro\n"
    path.write_text(edited, encoding="ascii")

    rig.refused("install", match="changed")
    assert path.read_text(encoding="ascii") == edited


@pytest.mark.parametrize("last", [False, True])
def test_a_failed_reload_leaves_roots_remove_to_be_run_again(rig: Rig, last: bool) -> None:
    rig.started()
    covered_root(rig)
    if not last:
        assert add_root(rig, make_root(rig, "music"), "music") == 0, rig.output()
    slot = "photos" if last else "music"
    before = rig.state()["mounts"]["generations"]
    rig.system.failures[RELOAD] = (1, "", "Failed to reload")
    first = len(rig.system.calls)

    rig.refused("roots", "remove", "--slot", slot, match=f"roots remove --slot {slot}")
    assert restarts(rig, first) == []
    state = rig.state()
    assert slot in state["roots"]
    # Nothing systemd may still bind is gone.
    for generation in before:
        assert all(path.exists() for path in generation_files(rig, generation))
    rig.fresh_output()
    assert rig("status") == 3 and "interrupted" in rig.stdout.getvalue()

    rig.system.failures.clear()
    first = len(rig.system.calls)
    assert rig("roots", "remove", "--slot", slot) == 0, rig.output()
    assert slot not in rig.state()["roots"]
    assert restarts(rig, first) == [ROOT_SERVICES]
    generations = rig.state()["mounts"]["generations"]
    assert len(generations) == (0 if last else 1)
    assert sorted(path.name for path in (rig.data / "mounts").iterdir()) == sorted(
        path.name for generation in generations for path in generation_files(rig, generation)
    )
    assert rig("status") == 0, rig.output()


def test_a_failed_restart_keeps_the_slot_too(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    assert add_root(rig, make_root(rig, "music"), "music") == 0, rig.output()
    rig.system.failures[(*SYSTEMCTL, "--no-block", "restart")] = (1, "", "Job failed")

    rig.refused("roots", "remove", "--slot", "music", match="roots remove --slot music")
    assert "music" in rig.state()["roots"]
    rig.system.failures.clear()
    assert rig("roots", "remove", "--slot", "music") == 0, rig.output()


def originals_snapshot(rig: Rig) -> dict[str, Any]:
    """Everything about the originals a change could touch."""
    entries: dict[str, Any] = {}
    for path in sorted([rig.originals, *rig.originals.rglob("*")]):
        info = path.lstat()
        attributes = {name: os.getxattr(path, name, follow_symlinks=False)
                      for name in os.listxattr(path, follow_symlinks=False)}
        entries[str(path)] = (
            info.st_mode, info.st_uid, info.st_gid, info.st_mtime_ns, info.st_ctime_ns,
            path.read_bytes() if path.is_file() else None, attributes,
        )
    return entries


def test_the_originals_stay_untouched_through_the_whole_lifecycle(rig: Rig) -> None:
    rig.started()
    photos = make_root(rig)
    music = make_root(rig, "music", HOME_LABEL)
    (music / "album").mkdir()
    (music / "album" / "track.flac").write_bytes(b"flac")
    music.chmod(0o750)
    labels = dict(rig.xattrs)
    before = originals_snapshot(rig)

    assert add_root(rig, photos) == 4 and add_root(rig, music, "music") == 4
    run_host_setup(rig)
    assert add_root(rig, photos) == 0 and add_root(rig, music, "music") == 0, rig.output()
    assert rig("upgrade", "--release", SAMPLE_OTHER_RELEASE) == 0, rig.output()
    assert rig("roots", "remove", "--slot", "music") == 0, rig.output()
    assert rig("roots", "remove", "--slot", "photos") == 0, rig.output()
    assert rig("uninstall", "--purge-data") == 0, rig.output()

    assert originals_snapshot(rig) == before
    assert rig.xattrs == labels
    touched = [*rig.system.labelled, *rig.system.chowned]
    assert not [path for path in touched if Path(path).is_relative_to(rig.originals)]


def write_fake(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    path.chmod(0o755)


def fake_host_tools(tmp_path: Path) -> tuple[Path, Path]:
    """A PATH of stand-ins for the privileged tools; each logs its argument vector."""
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "tools.log"
    prelude = f"""
        import json, os, sys
        with open({str(log)!r}, "a", encoding="utf-8") as handle:
            handle.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
    """
    write_fake(tools, "id", prelude + """
        assert sys.argv[1:] == ["-u"]
        print(os.environ["FAKE_UID"])
    """)
    write_fake(tools, "rpm", prelude + """
        sys.exit(1 if os.environ.get("FAKE_NO_UDICA") else 0)
    """)
    write_fake(tools, "dnf", prelude)
    write_fake(tools, "semodule", prelude)
    write_fake(tools, "firewall-cmd", prelude + """
        state_file = os.environ["FAKE_FIREWALL"]
        state = json.load(open(state_file)) if os.path.exists(state_file) else {}
        arguments = sys.argv[1:]
        scope = "permanent" if arguments[:1] == ["--permanent"] else "runtime"
        arguments = arguments[1:] if scope == "permanent" else arguments
        zone = arguments[0].removeprefix("--zone=")
        if zone not in os.environ["FAKE_ZONES"].split(","):
            print("Error: INVALID_ZONE: " + zone, file=sys.stderr)
            sys.exit(112)
        option, _, value = arguments[1].partition("=")
        rules = state.setdefault(f"{scope}/{zone}", [])
        if option == "--get-target" and scope == "permanent":
            print(os.environ.get("FAKE_TARGET", "default"))
        elif option == "--query-port":
            sys.exit(1)
        elif option == "--query-rich-rule":
            sys.exit(0 if value in rules else 1)
        elif option == "--add-rich-rule":
            rules.append(value)
            json.dump(state, open(state_file, "w"))
            print("success")
        else:
            sys.exit(2)
    """)
    os.symlink(shutil.which("sha256sum") or "/usr/bin/sha256sum", tools / "sha256sum")
    return tools, log


def run_script(rig: Rig, tmp_path: Path, **environment: str) -> tuple[int, str, list[list[str]]]:
    tools, log = tmp_path / "tools", tmp_path / "tools.log"
    log.unlink(missing_ok=True)
    result = subprocess.run(
        ["/bin/sh", str(script_path(rig))], check=False, capture_output=True, text=True,
        timeout=60, stdin=subprocess.DEVNULL,
        env={"PATH": str(tools), "SUDO_USER": rig.login, "FAKE_UID": "0",
             "FAKE_FIREWALL": str(tmp_path / "firewall.json"), "FAKE_ZONES": "test-lan",
             **environment},
    )
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] \
        if log.exists() else []
    return result.returncode, result.stdout + result.stderr, calls


def test_the_host_setup_script_behaves_against_stand_in_tools(rig: Rig, tmp_path: Path) -> None:
    fake_host_tools(tmp_path)
    rig.init()
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()

    code, output, calls = run_script(rig, tmp_path, FAKE_NO_UDICA="1")
    assert code == 0, output
    assert ["dnf", "install", "-y", "udica"] in calls
    assert ["semodule", "-i", str(module_path(rig)), f"{UDICA}/base_container.cil",
            f"{UDICA}/net_container.cil"] in calls
    added = [call for call in calls if any(word.startswith("--add-rich-rule") for word in call)]
    assert len(added) == 2 * len(PRIVATE_SOURCES)
    assert output.count("added (") == 2 * len(PRIVATE_SOURCES)
    assert "warning" not in output

    # Idempotent: a second run adds nothing.
    code, output, calls = run_script(rig, tmp_path)
    assert code == 0, output
    assert not [call for call in calls if any(word.startswith("--add-rich-rule") for word in call)]
    assert output.count("already present (") == 2 * len(PRIVATE_SOURCES)
    assert not [call for call in calls if call[0] == "dnf"]


@pytest.mark.parametrize("environment", [{"SUDO_USER": "someone-else"}, {"FAKE_UID": "1000"}])
def test_the_host_setup_script_refuses_before_any_privileged_step(
    rig: Rig, tmp_path: Path, environment: dict[str, str],
) -> None:
    fake_host_tools(tmp_path)
    rig.init()
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()

    code, output, calls = run_script(rig, tmp_path, **environment)
    assert code == 1 and "sudo" in output
    assert [call[0] for call in calls] in ([], ["id"])


def test_the_host_setup_script_stops_at_an_unknown_zone(rig: Rig, tmp_path: Path) -> None:
    fake_host_tools(tmp_path)
    rig.init()
    assert rig("host-setup", "--zone", "test-missing", "--zone", "test-lan") == 0, rig.output()

    code, output, calls = run_script(rig, tmp_path)
    assert code != 0 and "INVALID_ZONE" in output
    assert not [call for call in calls if "--zone=test-lan" in call]
    assert not [call for call in calls if any(word.startswith("--add") for word in call)]


def test_the_host_setup_script_warns_when_a_zone_already_admits_everything(
    rig: Rig, tmp_path: Path,
) -> None:
    fake_host_tools(tmp_path)
    rig.init()
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()

    code, output, calls = run_script(rig, tmp_path, FAKE_TARGET="ACCEPT")
    assert code == 0, output
    assert "warning: zone test-lan accepts every connection" in output
    # It warns only; the zone itself is never changed.
    assert not [call for call in calls if any("target" in word and "get" not in word
                                              for word in call)]


def test_the_host_setup_script_refuses_a_module_changed_since_it_was_written(
    rig: Rig, tmp_path: Path,
) -> None:
    fake_host_tools(tmp_path)
    rig.install()
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()
    assert add_root(rig, make_root(rig), "photos") == 4  # the module changes, the script not

    code, output, calls = run_script(rig, tmp_path)
    assert code == 1 and "aegis-podman host-setup" in output
    assert not [call for call in calls if call[0] in ("semodule", "firewall-cmd")]


def test_a_lost_library_is_copied_out_again(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    shutil.rmtree(library(rig))
    creates = len(rig.system.commands(*PODMAN, "create"))

    assert add_root(rig, make_root(rig, "music"), "music") == 0, rig.output()
    assert len(rig.system.commands(*PODMAN, "create")) == creates + 1
    assert (library(rig) / "aegisctl" / "cli.py").is_file()


def test_an_unrecorded_library_is_refused(rig: Rig) -> None:
    rig.install()
    root = pending_root(rig)
    rig.system.run_host_setup(module_path(rig))
    library(rig).mkdir()

    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match="not recorded")
    assert not rig.system.commands(*PODMAN, "create")[1:]


def test_a_file_that_would_shadow_the_standard_library_is_refused(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    (library(rig) / "json.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    first = len(rig.system.calls)

    rig.refused("roots", "add", "--slot", "music", "--source", str(make_root(rig, "music")),
                match="differs from its record")
    assert aegisctl_calls(rig, first) == []


def test_advice_quotes_a_source_the_shell_would_read_otherwise(rig: Rig) -> None:
    rig.install()
    root = make_root(rig, "a;id")

    assert add_root(rig, root) == 4
    assert f"--source '{root}'" in rig.stdout.getvalue()


def test_removing_a_root_drops_a_type_no_other_root_uses(rig: Rig) -> None:
    rig.install()
    covered_root(rig)
    music = pending_root(rig, "music", HOME_LABEL)
    rig.system.run_host_setup(module_path(rig))
    assert add_root(rig, music, "music") == 0, rig.output()
    assert rig.state()["selinux_covered"] == ["unlabeled_t", "user_home_t"]

    assert rig("roots", "remove", "--slot", "music") == 0, rig.output()
    assert rig.state()["selinux_covered"] == ["unlabeled_t"]
    # The next host setup loads a module without it.
    assert rig("host-setup") == 0, rig.output()
    assert "user_home_t" not in module_path(rig).read_text(encoding="utf-8")


def test_a_source_reached_through_a_symlink_is_refused(rig: Rig, tmp_path: Path) -> None:
    rig.install()
    root = make_root(rig)
    alias = tmp_path / "alias"
    alias.symlink_to(rig.originals)
    source = alias / "photos"
    rig.xattrs[str(source)] = UNLABELED

    rig.refused("roots", "add", "--slot", "photos", "--source", str(source),
                match=f"give its real path, {root}")


def test_a_source_replaced_before_inspect_is_refused(rig: Rig) -> None:
    rig.install()
    root = pending_root(rig)
    rig.system.run_host_setup(module_path(rig))

    def other_identity(command: tuple[str, ...]) -> tuple[int, str, str] | None:
        if command[:len(AEGISCTL)] == AEGISCTL and command[len(AEGISCTL) + 1] == "inspect":
            return 0, "local:1:2\n", ""
        return None

    rig.system.intercept = other_identity
    rig.refused("roots", "add", "--slot", "photos", "--source", str(root), match="changed")
    assert rig.state()["roots"] == {}


def test_purge_says_how_to_undo_the_host_setup(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    rig.fresh_output()

    assert rig("uninstall", "--purge-data") == 0, rig.output()
    output = rig.stdout.getvalue()
    for zone, rule in rich_rules(("test-lan",)):
        assert f"firewall-cmd --permanent --zone={zone} --remove-rich-rule='{rule}'" in output
        assert f"firewall-cmd --zone={zone} --remove-rich-rule='{rule}'" in output
    assert "semodule -r aegis_reader" in output
    # A rule the script found already present predates Aegis and must stay.
    assert 'only the rules the script reported as "added"' in output
    # Printed for the operator only: the installer ran none of it.
    assert not [call for call in rig.system.calls if call[0] in ("firewall-cmd", "semodule")]


def test_a_record_from_before_roots_is_read_with_empty_roots(tmp_path: Path) -> None:
    installer = load_installer()
    paths = private_paths(installer, tmp_path)
    installer.save_state(paths, sample_state(installer))
    raw = json.loads(paths.state.read_text(encoding="utf-8"))
    for key in ("roots", "selinux_covered", "selinux_pending", "mounts"):
        del raw[key]
    paths.state.write_text(json.dumps(raw), encoding="utf-8")

    state = installer.load_state(paths, os.getuid())
    assert state.roots == {} and state.selinux_covered == [] and state.selinux_pending == []
    assert state.mounts == {"current": None, "installing": None, "generations": {}}


def test_an_unrenderable_root_on_an_uninstalled_stack_points_at_roots_remove(rig: Rig) -> None:
    rig.started()
    root = covered_root(rig)
    assert rig("uninstall") == 0, rig.output()
    rig.xattrs[str(root)] = HOME_LABEL

    rig.refused("install", match="aegis-podman roots remove --slot photos")


# -- the SELinux module (spec D6) -------------------------------------------------------------


MODULE_TEMPLATE = INSTALLER.parent / "selinux" / "aegis_reader.cil.in"


def test_the_module_grants_read_only_rules_for_each_type() -> None:
    installer = load_installer()
    template = MODULE_TEMPLATE.read_text(encoding="utf-8")

    text = installer._module_text(template, ["user_home_t", "unlabeled_t", "unlabeled_t"])
    rules = [
        f"(allow process {selinux_type} ({kind}))"
        for selinux_type in ("unlabeled_t", "user_home_t")
        for kind in ("dir (getattr open read search ioctl lock)",
                     "file (getattr open read map ioctl lock)", "lnk_file (getattr read)")
    ]
    assert text == template.replace("@@READ_RULES@@", "\n    ".join(rules))
    block = text[text.index("(block aegis_reader"):]
    # The roles listen and connect on ports that are not http_port_t: the full network
    # template, as container_t has, not the restricted one.
    assert block.startswith(
        "(block aegis_reader\n    (blockinherit container)\n"
        "    (blockinherit net_container)\n"
    )
    assert "restricted_net_container)" not in block
    assert block.rstrip().endswith(")")
    assert not re.search(r"\b(write|append|create|setattr|unlink|rename|link|relabel\w*)\b",
                         block)
    # Why the full network template, written down where the module is.
    assert "restricted_net_container" in template and "http_port_t" in template


def test_a_module_with_no_root_type_yet_is_still_valid() -> None:
    installer = load_installer()
    template = MODULE_TEMPLATE.read_text(encoding="utf-8")
    text = installer._module_text(template, [])
    assert "(allow" not in text and "@@" not in text
    assert "    ; no root type is covered yet\n)" in text


@pytest.mark.parametrize("selinux_type", [
    "unconfined_t", "spc_t", "bin_exec_t", "shadow_t", "security_t", "Bad_t", "media",
    "etc_t", "proc_t", "sysfs_t", "init_t", "kernel_t", "ssh_home_t", "passwd_file_t", "cert_t",
    "gpg_secret_t", "ssh_key_t",
    "a_t)(allow process self (file (write)))", "",
])
def test_the_module_refuses_an_unsafe_type(selinux_type: str) -> None:
    installer = load_installer()
    template = MODULE_TEMPLATE.read_text(encoding="utf-8")
    with pytest.raises(installer.InstallError):
        installer._module_text(template, ["unlabeled_t", selinux_type])


def test_the_module_template_takes_no_other_token() -> None:
    installer = load_installer()
    template = MODULE_TEMPLATE.read_text(encoding="utf-8") + "; @@OTHER@@\n"
    with pytest.raises((installer.InstallError, installer.RenderError), match="OTHER"):
        installer._module_text(template, ["unlabeled_t"])


# -- the host-setup script (spec D7) ----------------------------------------------------------


def rich_rules(zones: tuple[str, ...]) -> list[tuple[str, str]]:
    return [
        (zone, f"rule family={family} source address={source} port port={SAMPLE_PORT} "
               "protocol=tcp accept")
        for zone in zones for family, source in PRIVATE_SOURCES
    ]


def firewall_commands(zones: tuple[str, ...]) -> list[str]:
    """Each rule, permanently and in the runtime configuration, each only when missing."""
    commands = []
    for zone, rule in rich_rules(zones):
        for scope in ("--permanent ", ""):
            commands += [
                f"if firewall-cmd {scope}--zone={zone} --query-rich-rule='{rule}' >/dev/null; "
                "then",
                "else",
                f"firewall-cmd {scope}--zone={zone} --add-rich-rule='{rule}' >/dev/null",
                "fi",
            ]
    return commands


def test_host_setup_writes_a_script_for_review_with_exactly_these_commands(rig: Rig) -> None:
    rig.init()
    zones = ("test-lan", "test_vpn")

    assert rig("host-setup", "--zone", zones[0], "--zone", zones[1]) == 0, rig.output()
    script = script_path(rig)
    text = script.read_text(encoding="utf-8")
    state = rig.state()
    assert mode(script) == 0o700
    assert state["files"][str(script)] == {"kind": "host-setup", "sha256": digest(script)}
    assert state["firewall_zones"] == list(zones)
    output = rig.stdout.getvalue()
    assert str(script) in output and text in output and f"sudo sh {script}" in output
    # The module it loads is shown too, for the same review.
    assert module_path(rig).read_text(encoding="utf-8") in output
    lines = text.splitlines()
    commands = [line.strip() for line in lines
                if line.strip() and not line.lstrip().startswith(("#", "echo "))]
    assert lines[0] == "#!/bin/sh"
    assert commands == [
        "set -eu",
        'if [ "$(id -u)" -ne 0 ]; then', "exit 1", "fi",
        f'if [ "${{SUDO_USER:-}}" != {rig.login} ]; then', "exit 1", "fi",
        f"if ! printf '%s  %s\\n' {digest(module_path(rig))} {module_path(rig)} "
        "| sha256sum --check --status -; then", "exit 1", "fi",
        "if ! rpm -q udica >/dev/null 2>&1; then", "dnf install -y udica", "fi",
        f"semodule -i {module_path(rig)} {UDICA}/base_container.cil {UDICA}/net_container.cil",
        *(command for zone in zones for command in (
            f"target=$(firewall-cmd --permanent --zone={zone} --get-target)",
            'if [ "$target" = ACCEPT ]; then', "fi",
            f"if firewall-cmd --zone={zone} --query-port={SAMPLE_PORT}/tcp >/dev/null; then", "fi",
            *firewall_commands((zone,)),
        )),
    ]
    # It says, rule by rule and for each configuration, what it added or found present.
    stripped = [line.strip() for line in lines]
    for zone, rule in rich_rules(zones):
        for scope in ("permanent", "runtime"):
            assert f"echo 'added ({scope}) to zone {zone}: {rule}'" in stripped
            assert f"echo 'already present ({scope}) in zone {zone}: {rule}'" in stripped
    syntax = subprocess.run(["sh", "-n", str(script)], check=False, capture_output=True,
                            text=True, timeout=30)
    assert syntax.returncode == 0, syntax.stderr


def test_the_host_setup_script_never_touches_other_rules_or_settings(rig: Rig) -> None:
    rig.init()
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()
    text = script_path(rig).read_text(encoding="utf-8")

    # A reload would drop the runtime-only rules other services added.
    for forbidden in ("--reload", "--complete-reload", "--set-target", "--add-service",
                      "--add-port", "--remove", "--delete", "--new-zone", "--set-",
                      "--add-interface", "--add-source", "--add-masquerade", "--add-forward",
                      "--panic", "--runtime-to-permanent", "setenforce", "setsebool", "chcon",
                      "restorecon", "semanage"):
        assert forbidden not in text, forbidden
    # It is run with sudo; it never calls sudo itself.
    assert not [line for line in text.splitlines() if line.lstrip().startswith("sudo")]
    assert text.count("--add-rich-rule") == text.count("--query-rich-rule") == 2 * len(
        PRIVATE_SOURCES,
    )
    for line in text.splitlines():
        if "firewall-cmd" in line:
            assert re.fullmatch(
                r"(if |    )firewall-cmd (--permanent )?--zone=test-lan "
                r"--(query|add)-rich-rule='rule family=ipv[46] source address=\S+ port port=9443 "
                r"protocol=tcp accept' >/dev/null(; then)?"
                # Read only: whether the zone already admits everything.
                r"|target=\$\(firewall-cmd --permanent --zone=test-lan --get-target\)"
                r"|if firewall-cmd --zone=test-lan --query-port=9443/tcp >/dev/null; then", line,
            ), line


def test_host_setup_writes_the_module_before_any_root(rig: Rig) -> None:
    rig.init()
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()
    module = module_path(rig)
    assert "; no root type is covered yet" in module.read_text(encoding="utf-8")
    assert rig.state()["files"][str(module)]["kind"] == "selinux"
    # Writing the script runs nothing privileged or otherwise.
    assert not [call for call in rig.system.calls
                if call[0] in ("semodule", "firewall-cmd", "dnf", "sudo")]


def test_host_setup_reuses_the_recorded_zones(rig: Rig) -> None:
    rig.init()
    rig.refused("host-setup", match="--zone")
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()
    assert rig("host-setup") == 0, rig.output()
    assert "--zone=test-lan " in script_path(rig).read_text(encoding="utf-8")


@pytest.mark.parametrize("zone", ["", "has space", "a;b", "z" * 33, "zone/1", "zone'1"])
def test_host_setup_refuses_an_invalid_zone(rig: Rig, zone: str) -> None:
    rig.init()
    rig.refused("host-setup", f"--zone={zone}", match="zone")
    assert not script_path(rig).exists()
    assert rig.state()["firewall_zones"] == []


@pytest.mark.parametrize("login", ["bad name", "-x", "a'b", ""])
def test_host_setup_refuses_a_login_name_it_cannot_embed(rig: Rig, login: str) -> None:
    rig.init()
    rig.login = login
    rig.refused("host-setup", "--zone", "test-lan", match="account")
    assert not script_path(rig).exists()


def test_the_script_loads_exactly_the_udica_templates_the_module_inherits(rig: Rig) -> None:
    def both_networks(tree: Path) -> None:
        # Two blocks from one template file: the file is loaded once.
        template = tree / "selinux" / "aegis_reader.cil.in"
        template.write_text(template.read_text(encoding="utf-8").replace(
            "(blockinherit net_container)",
            "(blockinherit net_container)\n    (blockinherit restricted_net_container)",
        ), encoding="utf-8")

    rig.system.tree_hook = both_networks
    rig.init()
    assert rig("host-setup", "--zone", "test-lan") == 0, rig.output()
    assert f"semodule -i {module_path(rig)} {UDICA}/base_container.cil {UDICA}/net_container.cil" \
        in script_path(rig).read_text(encoding="utf-8").splitlines()


def test_a_module_inheriting_an_unknown_template_is_refused(rig: Rig) -> None:
    def home_access(tree: Path) -> None:
        template = tree / "selinux" / "aegis_reader.cil.in"
        template.write_text(template.read_text(encoding="utf-8").replace(
            "(blockinherit container)",
            "(blockinherit container)\n    (blockinherit home_container)",
        ), encoding="utf-8")

    rig.system.tree_hook = home_access
    rig.init()
    rig.refused("host-setup", "--zone", "test-lan", match="home_container")
    assert not script_path(rig).exists()


def test_the_roots_flow_runs_only_its_programs_and_never_a_privileged_one(rig: Rig) -> None:
    rig.started()
    covered_root(rig)
    assert rig("roots", "remove", "--slot", "photos") == 0, rig.output()

    assert {call[0] for call in rig.system.calls} <= {
        "podman", "systemctl", "loginctl", "chcon", "ip", sys.executable,
    }
    assert not any(word in ("sudo", "semodule", "firewall-cmd", "dnf")
                   for call in rig.system.calls for word in call)


# -- how programs run --------------------------------------------------------------------------


def test_programs_run_only_as_argument_vectors_through_the_runner(rig: Rig) -> None:
    rig.started()
    assert rig("status") == 0
    assert rig(*BOOTSTRAP) == 0
    assert rig("uninstall", "--purge-data") == 0, rig.output()

    assert {call[0] for call in rig.system.calls} <= {
        "podman", "systemctl", "loginctl", "chcon", "ip",
    }
    assert all(call[:2] == PODMAN for call in rig.system.calls if call[0] == "podman")
    assert not any("sudo" in call for call in rig.system.calls)
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.count("subprocess.run(") == 1
    assert "shell=True" not in source
    assert "os.system" not in source and "Popen" not in source


def test_main_needs_a_command(rig: Rig) -> None:
    with pytest.raises(SystemExit):
        rig()
