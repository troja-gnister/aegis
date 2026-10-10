"""The aegis-podman host installer, against a fake host (spec D3, D4, D7, D9, D11, D13).

Nothing here runs Podman, systemd, loginctl, chcon or ip: every program goes to FakeSystem,
which refuses any command it does not know, and every path is under tmp_path.
"""
from __future__ import annotations

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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.support.podman_deploy import (
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

    def host(self) -> Any:
        self.system.unit_directory = self.quadlet_units
        return self.installer.Host(
            home=self.home, run=self.system, out=self.stdout, err=self.stderr, uid=self.uid,
            gid=SAMPLE_HOST_GID, environ=self.environ, monotonic=self.clock.monotonic,
            sleep=self.clock.sleep, port_in_use=lambda port: port in self.system.listeners,
            quadlet=self.quadlet, selinux_enforce=self.enforce,
        )

    def __call__(self, *argv: str) -> int:
        arguments = [*argv[:1], *self.extra, *argv[1:]] if argv else []
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
    "test-prefix-with-install-sections", "malformed-restart-flag",
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


def test_a_relative_xdg_config_home_is_refused(rig: Rig) -> None:
    rig.environ = {"XDG_CONFIG_HOME": "relative/place"}
    rig.refused(*INIT, match="XDG_CONFIG_HOME")
    assert not rig.system.calls


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
    for path in [*rig.quadlet_units.iterdir(), *rig.user_units.iterdir()]:
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
