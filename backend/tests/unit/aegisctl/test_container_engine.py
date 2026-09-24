from __future__ import annotations

import importlib
import json
import os
import stat
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def engine_module() -> ModuleType:
    return importlib.import_module("aegisctl.container_engine")


def podman_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, str], set[Path]]:
    provider = tmp_path / "provider"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)
    socket_parent = tmp_path / "podman-service"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "podman.sock"
    socket_path.touch(mode=0o600)
    accepted_sockets = {socket_path}
    real_lstat = Path.lstat

    def fake_lstat(path: Path) -> os.stat_result:
        metadata = real_lstat(path)
        if path == Path("/tmp"):
            values = list(metadata)
            values[stat.ST_UID] = 0
            return os.stat_result(values)
        if path in accepted_sockets:
            values = list(metadata)
            values[stat.ST_MODE] = stat.S_IFSOCK | stat.S_IMODE(metadata.st_mode)
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    return ({
        "AEGIS_CONTAINER_ENGINE": "podman",
        "PODMAN_COMPOSE_PROVIDER": str(provider),
        "AEGIS_PODMAN_SOCKET": str(socket_path),
    }, accepted_sockets)


def test_engine_defaults_to_docker_and_builds_argument_arrays() -> None:
    engine = engine_module()

    assert engine.selected_engine({}) == "docker"
    assert engine.container_command("inspect", "owned-id", environment={}) == [
        "docker", "inspect", "owned-id",
    ]


@pytest.mark.parametrize(
    "value",
    ("", " docker", "docker ", "Docker", "docker --host=tcp://remote", "/usr/bin/podman"),
)
def test_engine_rejects_every_value_outside_the_exact_allowlist(value: str) -> None:
    engine = engine_module()

    with pytest.raises(engine.ContainerEngineError, match="AEGIS_CONTAINER_ENGINE"):
        engine.selected_engine({"AEGIS_CONTAINER_ENGINE": value})


def test_podman_compose_requires_an_owned_absolute_executable_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_module()
    environment, _ = podman_environment(tmp_path, monkeypatch)
    provider = Path(environment["PODMAN_COMPOSE_PROVIDER"])

    assert engine.selected_engine(environment) == "podman"
    assert engine.compose_command(
        "config", "--format", "json", environment=environment,
    ) == ["podman", "--remote=false", "compose", "config", "--format", "json"]
    assert engine.container_command(
        "inspect", "owned-id", environment=environment,
    ) == ["podman", "--remote=false", "inspect", "owned-id"]

    for invalid in (
        environment | {"PODMAN_COMPOSE_PROVIDER": ""},
        environment | {"PODMAN_COMPOSE_PROVIDER": "docker-compose"},
    ):
        with pytest.raises(engine.ContainerEngineError, match="PODMAN_COMPOSE_PROVIDER"):
            engine.selected_engine(invalid)

    provider.chmod(0o600)
    with pytest.raises(engine.ContainerEngineError, match="PODMAN_COMPOSE_PROVIDER"):
        engine.compose_command("config", environment=environment)


def test_podman_provider_rejects_symlinks_and_writable_by_other_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_module()
    environment, _ = podman_environment(tmp_path, monkeypatch)
    provider = tmp_path / "provider"
    provider.chmod(0o755)
    link = tmp_path / "provider-link"
    link.symlink_to(provider)
    with pytest.raises(engine.ContainerEngineError, match="PODMAN_COMPOSE_PROVIDER"):
        engine.compose_command(
            "config", environment=environment | {"PODMAN_COMPOSE_PROVIDER": str(link)},
        )

    provider.chmod(0o777)
    with pytest.raises(engine.ContainerEngineError, match="PODMAN_COMPOSE_PROVIDER"):
        engine.compose_command("config", environment=environment)


def test_podman_requires_owned_local_socket_in_private_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_module()
    environment, accepted_sockets = podman_environment(tmp_path, monkeypatch)

    assert engine.podman_socket(environment) == Path(environment["AEGIS_PODMAN_SOCKET"])

    accepted_sockets.clear()
    with pytest.raises(engine.ContainerEngineError, match="AEGIS_PODMAN_SOCKET"):
        engine.selected_engine(environment)


def test_podman_socket_rejects_symlink_and_non_private_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_module()
    environment, _ = podman_environment(tmp_path, monkeypatch)
    socket_path = Path(environment["AEGIS_PODMAN_SOCKET"])

    socket_link = tmp_path / "socket-link"
    socket_link.symlink_to(socket_path)
    with pytest.raises(engine.ContainerEngineError, match="AEGIS_PODMAN_SOCKET"):
        engine.selected_engine(environment | {"AEGIS_PODMAN_SOCKET": str(socket_link)})

    noncanonical = socket_path.parent / ".." / socket_path.parent.name / socket_path.name
    with pytest.raises(engine.ContainerEngineError, match="AEGIS_PODMAN_SOCKET"):
        engine.selected_engine(environment | {"AEGIS_PODMAN_SOCKET": str(noncanonical)})

    socket_path.parent.chmod(0o750)
    with pytest.raises(engine.ContainerEngineError, match="AEGIS_PODMAN_SOCKET"):
        engine.selected_engine(environment)


def test_compose_environment_translates_only_validated_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_module()
    environment, _ = podman_environment(tmp_path, monkeypatch)
    environment |= {
        "PATH": os.environ["PATH"],
        "DOCKER_HOST": "tcp://remote.invalid:2375",
        "DOCKER_CONTEXT": "remote",
        "CONTAINER_HOST": "ssh://remote.invalid/run/podman.sock",
        "CONTAINER_CONNECTION": "remote",
    }

    direct = engine.sanitized_environment(environment)
    compose = engine.compose_environment(environment)

    assert "DOCKER_HOST" not in direct
    assert compose["DOCKER_HOST"] == f"unix://{environment['AEGIS_PODMAN_SOCKET']}"
    assert all(key not in compose for key in (
        "DOCKER_CONTEXT", "CONTAINER_HOST", "CONTAINER_CONNECTION",
    ))


def test_podman_labels_only_exact_inventoried_owned_test_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_module()
    environment, _ = podman_environment(tmp_path, monkeypatch)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    from tests.support.container_runtime import (
        map_inventory_files_to_container_user,
        prepare_owned_test_inventory,
        record_created_test_path,
        record_fresh_test_tree,
        record_test_tree_inventory,
    )

    root = tmp_path / "synthetic"
    root.mkdir(mode=0o700)
    tree = record_fresh_test_tree(root)
    nested = root / "nested"
    nested.mkdir(mode=0o700)
    source = nested / "source"
    source.write_text("test input", encoding="ascii")
    link = root / "link"
    link.symlink_to(source)
    fifo = root / "fifo"
    os.mkfifo(fifo)
    for path in (nested, source, link, fifo):
        record_created_test_path(tree, path)
    commands: list[list[str]] = []

    def completed(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(arguments)
        output = "1000:1000\n" if arguments[2:4] == ["unshare", "stat"] else ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(subprocess, "run", completed)
    inventory = record_test_tree_inventory(tree)
    prepare_owned_test_inventory(inventory)
    map_inventory_files_to_container_user(
        inventory, (source,), uid=1000, gid=1000,
    )

    assert commands == [
        ["chcon", "--no-dereference", "--type", "container_file_t", "--",
         str(root), str(fifo), str(link), str(nested), str(source)],
        ["podman", "--remote=false", "unshare", "chown", "--no-dereference", "1000:1000", "--",
         str(source)],
        ["podman", "--remote=false", "unshare", "stat", "--format=%u:%g", "--", str(source)],
    ]

    def add_unknown(
        arguments: list[str], **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        (root / "unknown").write_text("replacement", encoding="ascii")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", add_unknown)
    with pytest.raises(engine.ContainerEngineError, match="inventory changed"):
        prepare_owned_test_inventory(record_test_tree_inventory(tree))


def test_synthetic_inventory_rejects_hardlinked_external_file_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.support.container_runtime import (
        record_created_test_path,
        record_fresh_test_tree,
    )

    root = tmp_path / "synthetic"
    root.mkdir(mode=0o700)
    tree = record_fresh_test_tree(root)
    outside = tmp_path / "user-original"
    outside.write_text("preserve", encoding="ascii")
    linked = root / "linked"
    os.link(outside, linked)
    called = False

    def unexpected(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True
        del args, kwargs

    monkeypatch.setattr(subprocess, "run", unexpected)
    with pytest.raises(ValueError, match="hardlink"):
        record_created_test_path(tree, linked)

    assert called is False
    assert outside.read_text(encoding="ascii") == "preserve"


@pytest.mark.parametrize("kind", ("file", "directory"))
def test_ownership_mapping_refuses_added_entry_without_updating_ledgers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str,
) -> None:
    engine = engine_module()
    environment, _ = podman_environment(tmp_path, monkeypatch)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    from tests.support.container_runtime import (
        map_inventory_files_to_container_user,
        record_created_test_path,
        record_fresh_test_tree,
        record_test_tree_inventory,
    )

    root = tmp_path / "synthetic"
    root.mkdir(mode=0o700)
    tree = record_fresh_test_tree(root)
    manifest = root / "manifest.json"
    manifest.write_text("{}\n", encoding="ascii")
    record_created_test_path(tree, manifest)
    inventory = record_test_tree_inventory(tree)
    previous_inventory = dict(inventory.entries)
    previous_ledger = dict(tree.entries)

    def completed(
        arguments: list[str], **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        if arguments[2:4] == ["unshare", "chown"]:
            unknown = root / "unknown"
            unknown.mkdir() if kind == "directory" else unknown.write_text("unknown")
            return subprocess.CompletedProcess(arguments, 0, "", "")
        return subprocess.CompletedProcess(
            arguments, 0, f"{os.geteuid()}:{os.getegid()}\n", "",
        )

    monkeypatch.setattr(subprocess, "run", completed)
    with pytest.raises(engine.ContainerEngineError, match="inventory changed"):
        map_inventory_files_to_container_user(
            inventory, (manifest,), uid=os.geteuid(), gid=os.getegid(),
        )
    assert inventory.entries == previous_inventory
    assert tree.entries == previous_ledger


def test_synthetic_inventory_refuses_replaced_recorded_ancestor_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.support.container_runtime import (
        record_fresh_test_tree,
        record_test_tree_inventory,
    )

    holder = tmp_path / "holder"
    root = holder / "root"
    root.mkdir(parents=True)
    tree = record_fresh_test_tree(root)
    saved = tmp_path / "saved-holder"
    holder.rename(saved)
    root.mkdir(parents=True)
    called = False

    def unexpected(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(subprocess, "run", unexpected)
    with pytest.raises(ValueError, match="ancestor changed"):
        record_test_tree_inventory(tree)
    assert called is False


@pytest.mark.parametrize("field", (stat.ST_UID, stat.ST_GID))
def test_synthetic_inventory_refuses_owner_drift_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: int,
) -> None:
    from tests.support.container_runtime import (
        prepare_owned_test_inventory,
        record_created_test_path,
        record_fresh_test_tree,
        record_test_tree_inventory,
    )

    root = tmp_path / "synthetic"
    root.mkdir(mode=0o700)
    tree = record_fresh_test_tree(root)
    source = root / "source"
    source.write_text("input", encoding="ascii")
    record_created_test_path(tree, source)
    inventory = record_test_tree_inventory(tree)
    original_lstat = Path.lstat
    commands: list[list[str]] = []

    def changed(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if path == source:
            values = list(metadata)
            values[field] += 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "lstat", changed)
    monkeypatch.setattr(subprocess, "run", lambda arguments, **kwargs: commands.append(arguments))
    with pytest.raises(ValueError, match="inventory changed"):
        prepare_owned_test_inventory(inventory)
    assert commands == []


def test_parent_inventory_is_validated_before_copied_child_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.support.container_runtime import copy_bind_inputs, record_fresh_test_tree

    root = tmp_path / "synthetic"
    root.mkdir(mode=0o700)
    tree = record_fresh_test_tree(root)
    unknown = root / "unknown"
    unknown.write_text("preserve", encoding="ascii")
    source = tmp_path / "tracked-source"
    source.write_text("copy", encoding="ascii")
    commands: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda arguments, **kwargs: commands.append(arguments))

    with pytest.raises(ValueError, match="inventory changed"):
        copy_bind_inputs(root, {"copy": source}, parent_tree=tree)
    assert commands == []
    assert unknown.read_text(encoding="ascii") == "preserve"


def test_direct_cleanup_prevalidates_every_identity_before_first_delete() -> None:
    from tests.support.container_runtime import (
        OwnedDirectResource,
        OwnedDirectScope,
        cleanup_owned_resources,
    )

    first = OwnedDirectResource("container", "first", "owner", "scope")
    second = OwnedDirectResource("container", "second", "owner", "scope")
    commands: list[tuple[str, ...]] = []

    def runner(*arguments: str) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, "first\nsecond\n", "")
        if arguments[-2:] == ("inspect", "first"):
            payload = [{"Id": "first", "Config": {"Labels": {"owner": "scope"}}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[-2:] == ("inspect", "second"):
            payload = [{"Id": "replacement", "Config": {"Labels": {"owner": "scope"}}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    with pytest.raises(ValueError, match="changed"):
        cleanup_owned_resources(
            (first, second), runner,
            scopes=(OwnedDirectScope("container", "owner", "scope"),),
        )
    assert not any(arguments[0] == "rm" for arguments in commands)


def test_direct_cleanup_refuses_unknown_scope_member_before_deleting_known_resource() -> None:
    from tests.support.container_runtime import (
        OwnedDirectResource,
        OwnedDirectScope,
        cleanup_owned_resources,
    )

    known = OwnedDirectResource("container", "known", "owner", "scope")
    commands: list[tuple[str, ...]] = []

    def runner(*arguments: str) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, "known\nunknown\n", "")
        identity = arguments[-1]
        payload = [{"Id": identity, "Config": {"Labels": {"owner": "scope"}}}]
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(ValueError, match="inventory changed"):
        cleanup_owned_resources(
            (known,), runner,
            scopes=(OwnedDirectScope("container", "owner", "scope"),),
        )
    assert not any(arguments[0] == "rm" for arguments in commands)


def test_direct_cleanup_queries_attempted_empty_scope_before_other_cleanup() -> None:
    from tests.support.container_runtime import OwnedDirectScope, cleanup_owned_resources

    commands: list[tuple[str, ...]] = []

    def runner(*arguments: str) -> subprocess.CompletedProcess[str]:
        commands.append(arguments)
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, "unrecorded\n", "")
        payload = [{"Id": "unrecorded", "Config": {"Labels": {"owner": "scope"}}}]
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")

    with pytest.raises(ValueError, match="inventory changed"):
        cleanup_owned_resources(
            (), runner, scopes=(OwnedDirectScope("container", "owner", "scope"),),
        )
    assert not any(arguments[0] == "rm" for arguments in commands)


def test_direct_recovery_canonicalizes_unique_short_handle_and_rejects_query_error() -> None:
    from tests.support.container_runtime import OwnedDirectResource, recover_owned_resource

    identity = "a" * 64

    def runner(*arguments: str) -> subprocess.CompletedProcess[str]:
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, identity[:12] + "\n", "")
        if arguments == ("container", "inspect", identity[:12]):
            payload = [{
                "Id": identity,
                "Config": {"Labels": {"owner": "scope", "resource": "fixture"}},
            }]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    assert recover_owned_resource(
        "container", "", "owner", "scope", "resource", "fixture", runner,
    ) == OwnedDirectResource("container", identity, "owner", "scope")

    def failed(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(arguments, 125, "", "query failed")

    with pytest.raises(ValueError, match="query"):
        recover_owned_resource(
            "container", "", "owner", "scope", "resource", "fixture", failed,
        )


def test_exact_project_cleanup_refuses_unknown_or_replaced_resource_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = importlib.import_module("aegisctl.container_resources")
    owned = resources.ProjectResource("volume", "owned_data", "owned_data", "created=one")
    expected = resources.ProjectInventory("owned", (owned,))
    changed = resources.ProjectInventory("owned", (
        owned,
        resources.ProjectResource("container", "extra", "extra-id", "created=extra"),
    ))
    removed: list[list[str]] = []
    monkeypatch.setattr(resources, "capture_project_inventory", lambda *args, runner=None: changed)
    monkeypatch.setattr(resources, "_run", lambda arguments, environment: removed.append(arguments))

    with pytest.raises(resources.ProjectResourceError, match="unknown"):
        resources.cleanup_project_inventory(expected, {})
    assert removed == []

    replacement = resources.ProjectInventory("owned", (
        resources.ProjectResource("volume", "owned_data", "owned_data", "created=two"),
    ))
    monkeypatch.setattr(
        resources, "capture_project_inventory", lambda *args, runner=None: replacement
    )
    with pytest.raises(resources.ProjectResourceError, match="changed"):
        resources.cleanup_project_inventory(expected, {})
    assert removed == []


def test_project_transition_admits_only_one_explicit_labeled_resource() -> None:
    resources = importlib.import_module("aegisctl.container_resources")
    before = resources.ProjectInventory("owned", ())
    intended = resources.ProjectResource(
        "container", "new-id", "new-id",
        json.dumps({
            "Id": "new-id", "Created": "now", "Name": "owned-web-1",
            "Image": "image", "Labels": {
                "com.docker.compose.project": "owned",
                "com.docker.compose.service": "web",
                "com.docker.compose.oneoff": "False",
            },
        }, sort_keys=True, separators=(",", ":")),
    )
    rule = resources.ProjectResourceRule(
        "container", (("com.docker.compose.service", "web"),
                      ("com.docker.compose.oneoff", "False")),
    )

    assert resources.admit_project_transition(
        before, resources.ProjectInventory("owned", (intended,)), (rule,),
    ) == resources.ProjectInventory("owned", (intended,))

    unknown = resources.ProjectResource(
        "container", "unknown-id", "unknown-id",
        intended.fingerprint.replace('"web"', '"unknown"'),
    )
    with pytest.raises(resources.ProjectResourceError, match="unexpected"):
        resources.admit_project_transition(
            before, resources.ProjectInventory("owned", (unknown,)), (rule,),
        )
    with pytest.raises(resources.ProjectResourceError, match="ambiguous"):
        resources.admit_project_transition(
            before, resources.ProjectInventory("owned", (intended, intended)), (rule,),
        )


def test_project_transition_refuses_unrecorded_removal_and_replacement() -> None:
    resources = importlib.import_module("aegisctl.container_resources")
    original = resources.ProjectResource(
        "network", "network-id", "network-id",
        '{"Created":"one","Id":"network-id","Labels":'
        '{"com.docker.compose.network":"default",'
        '"com.docker.compose.project":"owned"},"Name":"owned_default"}',
    )
    before = resources.ProjectInventory("owned", (original,))
    with pytest.raises(resources.ProjectResourceError, match="removed"):
        resources.admit_project_transition(before, resources.ProjectInventory("owned", ()), ())

    replacement = resources.ProjectResource(
        "network", "replacement-id", "replacement-id",
        original.fingerprint.replace("network-id", "replacement-id"),
    )
    rule = resources.ProjectResourceRule(
        "network", (("com.docker.compose.network", "default"),),
    )
    with pytest.raises(resources.ProjectResourceError, match=r"removed|replacement"):
        resources.admit_project_transition(
            before, resources.ProjectInventory("owned", (replacement,)), (rule,),
        )


def test_project_inventory_rejects_volume_without_replacement_sensitive_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = importlib.import_module("aegisctl.container_resources")
    results = iter((
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 0, "", ""),
        subprocess.CompletedProcess([], 0, "owned_data\n", ""),
        subprocess.CompletedProcess([], 0, json.dumps([{
            "Name": "owned_data", "Driver": "local", "Mountpoint": "/volume",
            "Scope": "local", "Labels": {"com.docker.compose.project": "owned"},
        }]), ""),
    ))
    monkeypatch.setattr(resources, "_run", lambda arguments, environment: next(results))

    with pytest.raises(resources.ProjectResourceError, match="identity is invalid"):
        resources.capture_project_inventory("owned", {})


def test_exact_project_cleanup_removes_only_recorded_immutable_identities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = importlib.import_module("aegisctl.container_resources")
    expected = resources.ProjectInventory("owned", (
        resources.ProjectResource("container", "name", "container-id", "c"),
        resources.ProjectResource("network", "network-id", "network-id", "n"),
        resources.ProjectResource("volume", "owned_data", "owned_data", "v"),
    ))
    empty = resources.ProjectInventory("owned", ())
    inventories = iter((expected, empty))
    removed: list[list[str]] = []

    def completed(arguments: list[str], environment: object) -> subprocess.CompletedProcess[str]:
        del environment
        removed.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(
        resources, "capture_project_inventory", lambda *args, runner=None: next(inventories)
    )
    monkeypatch.setattr(resources, "_run", completed)
    resources.cleanup_project_inventory(expected, {})

    assert removed == [
        ["docker", "rm", "--force", "container-id"],
        ["docker", "network", "rm", "network-id"],
        ["docker", "volume", "rm", "owned_data"],
    ]


def test_project_inventory_query_failure_is_not_treated_as_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = importlib.import_module("aegisctl.container_resources")

    def failed(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(arguments, 125, "", "connection refused")

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(resources.ProjectResourceError, match="query failed"):
        resources.require_empty_project("owned", {})


def test_sanitized_environment_propagates_only_validated_podman_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = engine_module()
    source, _ = podman_environment(tmp_path, monkeypatch)
    source |= {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "AEGIS_CONTAINER_ENGINE": "podman",
        "AEGIS_DB_PASSWORD": "operator-secret",
        "PGPASSWORD": "operator-secret",
        "COMPOSE_FILE": "/operator/compose.yaml",
        "DOCKER_HOST": "tcp://remote.invalid:2375",
        "DOCKER_CONTEXT": "remote",
        "CONTAINER_HOST": "ssh://remote.invalid/run/podman.sock",
        "CONTAINER_CONNECTION": "remote",
    }

    sanitized = engine.sanitized_environment(source)

    assert sanitized == {
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "AEGIS_CONTAINER_ENGINE": "podman",
        "PODMAN_COMPOSE_PROVIDER": source["PODMAN_COMPOSE_PROVIDER"],
        "AEGIS_PODMAN_SOCKET": source["AEGIS_PODMAN_SOCKET"],
    }


def test_docker_environment_does_not_propagate_podman_provider(tmp_path: Path) -> None:
    engine = engine_module()
    provider = tmp_path / "provider"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)

    sanitized = engine.sanitized_environment({
        "PATH": os.environ["PATH"],
        "AEGIS_CONTAINER_ENGINE": "docker",
        "PODMAN_COMPOSE_PROVIDER": str(provider),
    })

    assert sanitized == {
        "PATH": os.environ["PATH"],
        "AEGIS_CONTAINER_ENGINE": "docker",
    }


def test_mount_observer_uses_exact_inventory_cleanup_without_compose_down() -> None:
    mounts = importlib.import_module("aegisctl.mounts")
    assert mounts.__file__ is not None
    source = Path(mounts.__file__).read_text(encoding="utf-8")

    observer = source[source.index("def observe_mount_fingerprints"):]
    assert "expected_resources = require_empty_project(project_name)" in observer
    assert "_cleanup_observer_project(project_name, expected_resources)" in observer
    assert '"down"' not in observer


def test_mount_observer_refuses_unknown_transition_before_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mounts = importlib.import_module("aegisctl.mounts")
    resources = importlib.import_module("aegisctl.container_resources")
    before = resources.ProjectInventory("observer", ())
    unknown = resources.ProjectResource(
        "container", "unknown", "unknown",
        json.dumps({
            "Id": "unknown", "Created": "now", "Name": "unknown", "Image": "image",
            "Labels": {
                "com.docker.compose.project": "observer",
                "com.docker.compose.service": "unknown",
                "com.docker.compose.oneoff": "True",
            },
        }, sort_keys=True, separators=(",", ":")),
    )
    cleaned: list[object] = []
    monkeypatch.setattr(
        mounts, "capture_project_inventory",
        lambda project: resources.ProjectInventory(project, (unknown,)),
    )
    monkeypatch.setattr(mounts, "cleanup_project_inventory", cleaned.append)

    with pytest.raises(resources.ProjectResourceError, match="unexpected"):
        mounts._cleanup_observer_project("observer", before)
    assert cleaned == []


def _network_fixture(engine: str) -> dict[str, Any]:
    labels = {
        "com.docker.compose.project": "owned", "com.docker.compose.network": "backend",
        "owner": "scope", "resource": "fixture",
    }
    if engine == "podman":
        return {
            "id": "a" * 64, "name": "owned_backend", "created": "2026-09-23T00:00:00Z",
            "driver": "bridge", "labels": labels, "internal": True,
            "subnets": [{"subnet": "10.253.0.0/24", "gateway": "10.253.0.1"}],
            "network_interface": "podman1", "ipv6_enabled": False,
            "dns_enabled": True, "ipam_options": {"driver": "host-local"},
        }
    return {
        "Id": "a" * 64, "Name": "owned_backend", "Created": "2026-09-23T00:00:00Z",
        "Driver": "bridge", "Labels": labels, "Internal": True,
        "IPAM": {"Config": [{"Subnet": "10.253.0.0/24", "Gateway": "10.253.0.1"}]},
        "Scope": "local", "EnableIPv6": False, "Containers": {}, "Options": {},
    }


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_native_network_project_capture_transition_and_exact_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str,
) -> None:
    from aegisctl import container_resources as resources

    fixture = _network_fixture(engine)
    present = True
    deleted: list[str] = []
    environment = (podman_environment(tmp_path, monkeypatch)[0]
                   if engine == "podman" else {"AEGIS_CONTAINER_ENGINE": engine})

    def runner(arguments: list[str], environment: object) -> subprocess.CompletedProcess[str]:
        nonlocal present
        prefix = 2 if engine == "podman" else 1
        args = arguments[prefix:]
        if args[:2] == ["network", "ls"]:
            output = "short-handle\n" if present else ""
        elif args[:2] == ["network", "inspect"]:
            output = json.dumps([fixture])
        elif args[:2] == ["network", "rm"]:
            deleted.append(args[2])
            present = False
            output = ""
        else:
            assert args[1] == "ls"
            output = ""
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(resources, "_run", runner)
    observed = resources.capture_project_inventory("owned", environment)
    admitted = resources.admit_project_transition(
        resources.ProjectInventory("owned", ()), observed,
        (resources.ProjectResourceRule("network", (("com.docker.compose.network", "backend"),)),),
    )
    assert admitted.resources[0].immutable_id == "a" * 64
    assert json.loads(admitted.resources[0].fingerprint)["Name"] == "owned_backend"
    resources.cleanup_project_inventory(admitted, environment)
    assert deleted == ["a" * 64]
    assert resources.capture_project_inventory("owned", environment).resources == ()


@pytest.mark.parametrize("engine", ["docker", "podman"])
def test_native_network_direct_recovery_and_exact_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str,
) -> None:
    from tests.support.container_runtime import cleanup_owned_resources, recover_owned_resource

    environment = (podman_environment(tmp_path, monkeypatch)[0]
                   if engine == "podman" else {"AEGIS_CONTAINER_ENGINE": engine})
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    present = True
    deleted: list[str] = []

    def runner(*args: str) -> subprocess.CompletedProcess[str]:
        nonlocal present
        if args[:2] == ("network", "ls"):
            output = "short-handle\n" if present else ""
        elif args[:2] == ("network", "inspect"):
            output = json.dumps([_network_fixture(engine)])
        elif args[:2] == ("network", "rm"):
            deleted.append(args[2])
            present = False
            output = ""
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(args, 0, output, "")

    resource = recover_owned_resource(
        "network", "", "owner", "scope", "resource", "fixture", runner,
    )
    assert resource.immutable_id == "a" * 64
    cleanup_owned_resources((resource,), runner)
    assert deleted == ["a" * 64]
    assert not present


@pytest.mark.parametrize("engine", ["docker", "podman"])
@pytest.mark.parametrize("defect", [
    "missing", "empty", "conflicting", "ambiguous", "labels", "name", "created",
    "driver", "foreign-shape",
])
def test_native_network_uncertainty_refuses_project_and_direct_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str, defect: str,
) -> None:
    from aegisctl import container_resources as resources

    from tests.support.container_runtime import OwnedDirectResource, cleanup_owned_resources

    fixture = _network_fixture(engine)
    key = "id" if engine == "podman" else "Id"
    if defect == "missing":
        del fixture[key]
    elif defect == "empty":
        fixture[key] = ""
    elif defect in {"conflicting", "ambiguous"}:
        fixture["ID"] = "other" if defect == "conflicting" else fixture[key]
    elif defect == "foreign-shape":
        fixture = _network_fixture("docker" if engine == "podman" else "podman")
    else:
        field = {
            "labels": "Labels", "name": "Name", "created": "Created", "driver": "Driver",
        }[defect]
        fixture[field.lower() if engine == "podman" else field] = None
    environment = (podman_environment(tmp_path, monkeypatch)[0]
                   if engine == "podman" else {"AEGIS_CONTAINER_ENGINE": engine})
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    deleted: list[object] = []

    def runner(*args: str) -> subprocess.CompletedProcess[str]:
        if "rm" in args:
            deleted.append(args)
        output = json.dumps([fixture]) if "inspect" in args else (
            "short-handle\n" if args[:2] == ("network", "ls") else ""
        )
        return subprocess.CompletedProcess(args, 0, output, "")

    prefix = 2 if engine == "podman" else 1
    monkeypatch.setattr(resources, "_run", lambda args, env: runner(*args[prefix:]))
    with pytest.raises(resources.ProjectResourceError):
        resources.capture_project_inventory("owned", environment)
    with pytest.raises(ValueError):
        cleanup_owned_resources(
            (OwnedDirectResource("network", "a" * 64, "owner", "scope"),), runner,
        )
    assert deleted == []
