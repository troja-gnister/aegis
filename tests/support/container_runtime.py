"""Creation-ledger support for synthetic rootless-container bind inputs."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aegisctl.container_engine import ContainerEngineError, container_command, selected_engine
from aegisctl.container_network import network_identity_fields

Identity = tuple[int, int, int, int, int, int]
DirectRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class OwnedDirectResource:
    kind: str
    immutable_id: str
    owner_key: str
    owner_value: str


@dataclass(frozen=True, order=True)
class OwnedDirectScope:
    kind: str
    owner_key: str
    owner_value: str


def read_optional_cidfile(path: Path) -> str:
    """Read a safe optional engine CID file without following aliases."""
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
        ):
            return ""
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                return ""
            raw = os.read(descriptor, 129)
        finally:
            os.close(descriptor)
    except OSError:
        return ""
    if len(raw) > 128:
        return ""
    try:
        candidate = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return ""
    if not candidate or any(not (character.isalnum() or character in "-_.")
                            for character in candidate):
        return ""
    return candidate


def _inspect_owned_resource(
    kind: str,
    handle: str,
    owner_key: str,
    owner_value: str,
    runner: DirectRunner,
    *,
    discriminator: tuple[str, str] | None = None,
) -> OwnedDirectResource:
    inspected = runner(kind, "inspect", handle)
    if inspected.returncode:
        raise ContainerEngineError("owned test resource inspection failed")
    try:
        payload = json.loads(inspected.stdout)
        info = payload[0]
        if len(payload) != 1 or not isinstance(info, dict):
            raise ValueError
        if kind == "network":
            fields = network_identity_fields(info, selected_engine())
            identity, labels = fields["Id"], fields["Labels"]
        else:
            identity = info["Id"]
            labels = info.get("Config", {}).get("Labels", {})
        if not isinstance(identity, str) or not identity or not isinstance(labels, dict):
            raise ValueError
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise ContainerEngineError("owned test resource inspection failed") from exc
    if labels.get(owner_key) != owner_value:
        raise ContainerEngineError("owned test resource ownership changed")
    if discriminator is not None and labels.get(discriminator[0]) != discriminator[1]:
        raise ContainerEngineError("owned test resource identity changed")
    return OwnedDirectResource(kind, identity, owner_key, owner_value)


def recover_owned_resource(
    kind: str,
    candidate: str,
    owner_key: str,
    owner_value: str,
    discriminator_key: str,
    discriminator_value: str,
    runner: DirectRunner,
) -> OwnedDirectResource:
    if kind not in {"container", "network"}:
        raise ContainerEngineError("owned test resource kind is invalid")
    arguments = [
        kind, "ls", "--quiet",
        "--filter", f"label={owner_key}={owner_value}",
        "--filter", f"label={discriminator_key}={discriminator_value}",
    ]
    if kind == "container":
        arguments[2:2] = ["--all", "--no-trunc"]
    queried = runner(*arguments)
    if queried.returncode:
        raise ContainerEngineError("owned test resource recovery query failed")
    handles = queried.stdout.split()
    if len(handles) != 1:
        raise ContainerEngineError(
            f"owned test resource recovery is ambiguous (handles={len(handles)})"
        )
    recovered = _inspect_owned_resource(
        kind, handles[0], owner_key, owner_value, runner,
        discriminator=(discriminator_key, discriminator_value),
    )
    if candidate:
        recorded = _inspect_owned_resource(
            kind, candidate, owner_key, owner_value, runner,
            discriminator=(discriminator_key, discriminator_value),
        )
        if recorded.immutable_id != recovered.immutable_id:
            raise ContainerEngineError("owned test resource recovery is ambiguous")
    return recovered


def _query_owned_scope(
    scope: OwnedDirectScope, runner: DirectRunner,
) -> tuple[OwnedDirectResource, ...]:
    query_arguments = [
        scope.kind, "ls", "--quiet", "--filter",
        f"label={scope.owner_key}={scope.owner_value}",
    ]
    if scope.kind == "container":
        query_arguments[2:2] = ["--all", "--no-trunc"]
    queried = runner(*query_arguments)
    if queried.returncode:
        raise ContainerEngineError("owned test resource scope query failed")
    handles = queried.stdout.split()
    if len(handles) != len(set(handles)):
        raise ContainerEngineError("owned test resource scope is ambiguous")
    observed = tuple(sorted(
        (_inspect_owned_resource(
            scope.kind, handle, scope.owner_key, scope.owner_value, runner,
        ) for handle in handles),
        key=lambda resource: resource.immutable_id,
    ))
    if len(observed) != len(set(observed)):
        raise ContainerEngineError("owned test resource scope is ambiguous")
    return observed


def validate_owned_resources(
    resources: tuple[OwnedDirectResource, ...], runner: DirectRunner,
    *, scopes: tuple[OwnedDirectScope, ...] = (),
) -> None:
    if len(set(resources)) != len(resources):
        raise ContainerEngineError("owned test resource inventory is invalid")
    all_scopes = set(scopes) | {
        OwnedDirectScope(resource.kind, resource.owner_key, resource.owner_value)
        for resource in resources
    }
    if not all_scopes:
        raise ContainerEngineError("owned test resource inventory is invalid")
    for scope in sorted(all_scopes):
        expected = tuple(sorted(
            (resource for resource in resources if (
                resource.kind, resource.owner_key, resource.owner_value
            ) == (scope.kind, scope.owner_key, scope.owner_value)),
            key=lambda resource: resource.immutable_id,
        ))
        if _query_owned_scope(scope, runner) != expected:
            raise ContainerEngineError("owned test resource inventory changed")


def cleanup_owned_resources(
    resources: tuple[OwnedDirectResource, ...], runner: DirectRunner,
    *,
    scopes: tuple[OwnedDirectScope, ...] = (),
    remove: tuple[OwnedDirectResource, ...] | None = None,
) -> None:
    validate_owned_resources(resources, runner, scopes=scopes)
    selected = resources if remove is None else remove
    if len(set(selected)) != len(selected) or not set(selected) <= set(resources):
        raise ContainerEngineError("owned test resource removal inventory is invalid")
    for resource in selected:
        remove_arguments = (
            ("rm", "--force", resource.immutable_id)
            if resource.kind == "container"
            else ("network", "rm", resource.immutable_id)
        )
        if runner(*remove_arguments).returncode:
            raise ContainerEngineError("exact owned test resource cleanup failed")
    retained = tuple(resource for resource in resources if resource not in set(selected))
    validate_owned_resources(retained, runner, scopes=scopes or tuple(sorted({
        OwnedDirectScope(resource.kind, resource.owner_key, resource.owner_value)
        for resource in resources
    })))


@dataclass
class FreshTestTree:
    root: Path
    ancestors: dict[Path, tuple[int, int]]
    entries: dict[Path, Identity] = field(default_factory=dict)


@dataclass(frozen=True)
class TestTreeInventory:
    tree: FreshTestTree
    entries: dict[Path, Identity]


def _identity(path: Path, *, require_owner: bool) -> Identity:
    error = "synthetic test bind inventory changed"
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ContainerEngineError(error) from exc
    if require_owner and metadata.st_uid != os.geteuid():
        raise ContainerEngineError(error)
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise ContainerEngineError("synthetic test bind rejects hardlinked regular files")
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink if stat.S_ISREG(metadata.st_mode) else 0,
    )


def _ancestor_paths(root: Path, temporary_root: Path) -> list[Path]:
    paths = []
    current = root
    while True:
        paths.append(current)
        if current == temporary_root:
            return list(reversed(paths))
        if current == current.parent or not current.is_relative_to(temporary_root):
            raise ContainerEngineError("fresh synthetic test tree is unsafe")
        current = current.parent


def _require_ancestors(tree: FreshTestTree) -> None:
    for path, expected in tree.ancestors.items():
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ContainerEngineError("synthetic test bind ancestor changed") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or (metadata.st_dev, metadata.st_ino) != expected
        ):
            raise ContainerEngineError("synthetic test bind ancestor changed")


def record_fresh_test_tree(root: Path) -> FreshTestTree:
    """Record an empty test-owned directory and every existing ancestor."""
    error = "fresh synthetic test tree is unsafe"
    try:
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        paths = _ancestor_paths(root, temporary_root)
        metadata = root.lstat()
    except OSError as exc:
        raise ContainerEngineError(error) from exc
    ancestors: dict[Path, tuple[int, int]] = {}
    for path in paths:
        ancestor = path.lstat()
        if not stat.S_ISDIR(ancestor.st_mode):
            raise ContainerEngineError(error)
        ancestors[path] = (ancestor.st_dev, ancestor.st_ino)
    if (
        root.resolve(strict=True) != root
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or any(root.iterdir())
    ):
        raise ContainerEngineError(error)
    tree = FreshTestTree(root, ancestors)
    tree.entries[root] = _identity(root, require_owner=True)
    return tree


def record_created_test_path(
    tree: FreshTestTree, path: Path, *, recursive: bool = False,
) -> None:
    """Add one caller-created path (or freshly copied subtree) to the ledger."""
    _require_ancestors(tree)
    if path == tree.root or not path.is_relative_to(tree.root) or path in tree.entries:
        raise ContainerEngineError("synthetic test creation ledger is invalid")
    if path.parent not in tree.entries:
        raise ContainerEngineError("synthetic test path has an unrecorded parent")
    identity = _identity(path, require_owner=True)
    tree.entries[path] = identity
    if not recursive or identity[2] != stat.S_IFDIR:
        return
    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ContainerEngineError("synthetic test creation ledger is invalid") from exc
        for child in children:
            child_path = Path(child.path)
            if child_path in tree.entries:
                raise ContainerEngineError("synthetic test creation ledger is invalid")
            child_identity = _identity(child_path, require_owner=True)
            tree.entries[child_path] = child_identity
            if child_identity[2] == stat.S_IFDIR:
                pending.append(child_path)


def _current_inventory(
    tree: FreshTestTree, *, require_owner: bool = True,
) -> dict[Path, Identity]:
    _require_ancestors(tree)
    current: dict[Path, Identity] = {}
    pending = [tree.root]
    while pending:
        path = pending.pop()
        identity = _identity(path, require_owner=require_owner)
        current[path] = identity
        if identity[2] != stat.S_IFDIR:
            continue
        try:
            children = sorted(os.scandir(path), key=lambda entry: entry.name)
        except OSError as exc:
            raise ContainerEngineError("synthetic test bind inventory changed") from exc
        for child in reversed(children):
            pending.append(Path(child.path))
    return current


def record_test_tree_inventory(tree: FreshTestTree) -> TestTreeInventory:
    """Freeze the exact creation ledger after proving no unknown entries exist."""
    current = _current_inventory(tree)
    if current != tree.entries:
        raise ContainerEngineError("synthetic test bind inventory changed")
    return TestTreeInventory(tree, dict(tree.entries))


def _ordered_paths(inventory: TestTreeInventory) -> list[Path]:
    return [inventory.tree.root, *sorted(
        (path for path in inventory.entries if path != inventory.tree.root),
        key=str,
    )]


def prepare_owned_test_inventory(inventory: TestTreeInventory) -> None:
    """Label an exact caller-recorded inventory and reject every change."""
    if _current_inventory(inventory.tree) != inventory.entries:
        raise ContainerEngineError("synthetic test bind inventory changed")
    if selected_engine() == "docker":
        return
    targets = _ordered_paths(inventory)
    for offset in range(0, len(targets), 128):
        command = [
            "chcon", "--no-dereference", "--type", "container_file_t", "--",
            *(str(target) for target in targets[offset:offset + 128]),
        ]
        try:
            result = subprocess.run(
                command, check=False, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ContainerEngineError("synthetic test bind labeling failed") from exc
        if _current_inventory(inventory.tree) != inventory.entries:
            raise ContainerEngineError("synthetic test bind inventory changed")
        if result.returncode:
            raise ContainerEngineError("synthetic test bind labeling failed")


def map_inventory_files_to_container_user(
    inventory: TestTreeInventory,
    paths: tuple[Path, ...],
    *,
    uid: int,
    gid: int,
) -> None:
    """Map exact recorded regular files to one rootless container identity."""
    if selected_engine() == "docker":
        return
    if uid < 0 or gid < 0 or _current_inventory(inventory.tree) != inventory.entries:
        raise ContainerEngineError("synthetic test ownership inventory changed")
    if not paths or len(set(paths)) != len(paths):
        raise ContainerEngineError("synthetic test ownership inventory is invalid")
    for path in paths:
        identity = inventory.entries.get(path)
        if identity is None or identity[2] != stat.S_IFREG:
            raise ContainerEngineError("synthetic test ownership inventory is invalid")
    command = container_command(
        "unshare", "chown", "--no-dereference", f"{uid}:{gid}", "--",
        *(str(path) for path in paths),
    )
    try:
        result = subprocess.run(
            command, check=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContainerEngineError("synthetic test ownership mapping failed") from exc
    if result.returncode:
        raise ContainerEngineError("synthetic test ownership mapping failed")
    verification = container_command(
        "unshare", "stat", "--format=%u:%g", "--", *(str(path) for path in paths),
    )
    try:
        verified = subprocess.run(
            verification, check=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ContainerEngineError("synthetic test ownership mapping failed") from exc
    if (
        verified.returncode
        or verified.stdout.splitlines() != [f"{uid}:{gid}"] * len(paths)
    ):
        raise ContainerEngineError("synthetic test ownership mapping failed")
    current = _current_inventory(inventory.tree, require_owner=False)
    if current.keys() != inventory.entries.keys():
        raise ContainerEngineError("synthetic test ownership inventory changed")
    for path, previous in inventory.entries.items():
        observed = current.get(path)
        if observed is None or (
            observed != previous and (
                path not in paths or observed[:3] != previous[:3] or observed[5] != previous[5]
            )
        ):
            raise ContainerEngineError("synthetic test ownership inventory changed")
    for path in paths:
        inventory.entries[path] = current[path]
        inventory.tree.entries[path] = current[path]


def copy_bind_inputs(
    parent: Path,
    sources: Mapping[str, Path],
    *,
    parent_tree: FreshTestTree | None = None,
) -> dict[str, Path]:
    """Create, ledger, copy, and prepare a dedicated tracked-input mirror."""
    if parent_tree is not None:
        record_test_tree_inventory(parent_tree)
    bind_root = parent / "container-bind-inputs"
    bind_root.mkdir(mode=0o700)
    if parent_tree is not None:
        record_created_test_path(parent_tree, bind_root)
    tree = record_fresh_test_tree(bind_root)
    copied: dict[str, Path] = {}
    for name, source in sources.items():
        if not name or Path(name).name != name:
            raise ContainerEngineError("synthetic bind copy name is invalid")
        destination = bind_root / name
        if source.is_dir():
            shutil.copytree(source, destination)
            record_created_test_path(tree, destination, recursive=True)
            if parent_tree is not None:
                record_created_test_path(parent_tree, destination, recursive=True)
        else:
            shutil.copy2(source, destination)
            record_created_test_path(tree, destination)
            if parent_tree is not None:
                record_created_test_path(parent_tree, destination)
        copied[name] = destination
    if parent_tree is None:
        prepare_owned_test_inventory(record_test_tree_inventory(tree))
    return copied


def run_deployment_process(
    command: Sequence[str], **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Route complete workload argv through the production checked launcher."""
    from aegisctl.container_launch import controlled_container_argv

    if command and command[0] in ("docker", "podman"):
        command = controlled_container_argv(
            command, environment=kwargs.get("env"),
            canonical_base=Path(__file__).resolve().parents[2] / "compose.yaml",
            working_directory=kwargs.get("cwd"),
        )
    return subprocess.run(command, **kwargs)
