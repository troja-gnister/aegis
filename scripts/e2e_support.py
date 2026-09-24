"""Private generated inputs and bounded diagnostics for disposable browser tests."""
from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from aegis_apps.common.redaction import redact
from aegisctl.container_engine import (
    compose_command,
    compose_environment,
    container_command,
    selected_engine,
)
from aegisctl.container_launch import controlled_container_argv
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResource,
    ProjectResourceRule,
    admit_project_transition,
    capture_project_inventory,
    cleanup_project_inventory,
    require_project_inventory,
)
from aegisctl.mounts import local_identity

REPOSITORY = Path(__file__).resolve().parents[1]
SECRET_NAMES = (
    "postgres-superuser-password", "db-migrator-password", "db-web-password",
    "db-operations-password", "db-indexer-password", "db-media-password",
    "django-secret-key", "auth-throttle-hmac-key", "e2e-alice-password",
    "e2e-bob-password", "e2e-admin-password",
)
GENERATED_NAMES = (
    "mounts.toml", "mounts.manifest.json", "compose.mounts.yaml",
    "mounts.gateway.attestation",
)
PROJECT = "aegis-phase1-e2e"
ROOT_NAMES = ("alice", "bob")
LEDGER_NAME = ".aegis-e2e-ledger.json"


def checked_directory(value: str) -> Path:
    path = Path(value)
    try:
        metadata = path.lstat()
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise ValueError("invalid E2E temporary directory") from exc
    if (
        not path.is_absolute() or path.resolve(strict=True) != path
        or parent != Path("/tmp").resolve()
        or not path.name.startswith(f"{PROJECT}.")
        or len(path.name.removeprefix(f"{PROJECT}.")) != 8
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o077
    ):
        raise ValueError("invalid E2E temporary directory")
    return path


def _identity(path: Path) -> list[int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError("synthetic E2E input is missing") from exc
    kind = stat.S_IFMT(metadata.st_mode)
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise ValueError("synthetic E2E input is hardlinked")
    links = metadata.st_nlink if stat.S_ISREG(metadata.st_mode) else 0
    return [
        metadata.st_dev, metadata.st_ino, kind,
        metadata.st_uid, metadata.st_gid, links,
    ]


def _relative(path: Path, target: Path) -> str:
    try:
        relative = target.relative_to(path).as_posix()
    except ValueError as exc:
        raise ValueError("synthetic E2E ledger path is invalid") from exc
    return "." if relative == "." else relative


def _record(ledger: dict[str, Any], path: Path, target: Path) -> None:
    key = _relative(path, target)
    entries = ledger["entries"]
    if key in entries:
        raise ValueError("synthetic E2E creation ledger is invalid")
    parent_key = _relative(path, target.parent) if target != path else None
    if parent_key is not None and parent_key not in entries:
        raise ValueError("synthetic E2E creation parent was not recorded")
    entries[key] = _identity(target)


def _write_ledger(
    path: Path, ledger: dict[str, Any], *, descriptor: int | None = None,
) -> None:
    raw = (json.dumps(ledger, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
    close = descriptor is None
    if descriptor is None:
        descriptor = os.open(path / LEDGER_NAME, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        if close:
            os.close(descriptor)


def _load_ledger(path: Path) -> dict[str, Any]:
    checked_directory(str(path))
    try:
        ledger = json.loads((path / LEDGER_NAME).read_bytes())
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("synthetic E2E creation ledger is invalid") from exc
    if (
        not isinstance(ledger, dict) or ledger.get("version") != 1
        or not isinstance(ledger.get("entries"), dict)
        or not isinstance(ledger.get("ancestors"), dict)
    ):
        raise ValueError("synthetic E2E creation ledger is invalid")
    return ledger


def _current_entries(path: Path) -> dict[str, list[int]]:
    current: dict[str, list[int]] = {}
    pending = [path]
    while pending:
        target = pending.pop()
        current[_relative(path, target)] = _identity(target)
        if stat.S_IFMT(target.lstat().st_mode) != stat.S_IFDIR:
            continue
        try:
            children = list(os.scandir(target))
        except OSError as exc:
            raise ValueError("synthetic E2E inventory cannot be read") from exc
        pending.extend(Path(child.path) for child in children)
    return current


def _validate_ledger(
    path: Path, ledger: dict[str, Any], *, exact: bool = True,
) -> None:
    for name, expected in ledger["ancestors"].items():
        ancestor = Path(name)
        try:
            metadata = ancestor.lstat()
        except OSError as exc:
            raise ValueError("synthetic E2E ancestor was replaced") from exc
        if not stat.S_ISDIR(metadata.st_mode) or [metadata.st_dev, metadata.st_ino] != expected:
            raise ValueError("synthetic E2E ancestor was replaced")
    current = _current_entries(path)
    expected = ledger["entries"]
    changed = any(
        current.get(key) != identity for key, identity in expected.items()
    )
    if (exact and current != expected) or changed:
        raise ValueError("synthetic E2E inventory changed or contains unknown inputs")


def prepare(path: Path) -> None:
    path = checked_directory(str(path))
    if any(path.iterdir()):
        raise ValueError("synthetic E2E directory must be empty")
    root_metadata = path.lstat()
    tmp_metadata = path.parent.lstat()
    ledger = {
        "version": 1,
        "ancestors": {
            str(path.parent): [tmp_metadata.st_dev, tmp_metadata.st_ino],
            str(path): [root_metadata.st_dev, root_metadata.st_ino],
        },
        "entries": {".": _identity(path)},
        "resources": [],
    }
    ledger_path = path / LEDGER_NAME
    descriptor = os.open(ledger_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        _record(ledger, path, ledger_path)
        secret_dir = path / "secrets"
        secret_dir.mkdir(mode=0o700)
        _record(ledger, path, secret_dir)
        for name in SECRET_NAMES:
            target = secret_dir / name
            with target.open("x", encoding="ascii") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(secrets.token_hex(32) + "\n")
            _record(ledger, path, target)
        root_dir = path / "roots"
        root_dir.mkdir(mode=0o700)
        _record(ledger, path, root_dir)
        lines = ["version = 1"]
        for name in ROOT_NAMES:
            source = root_dir / name
            source.mkdir(mode=0o755)
            _record(ledger, path, source)
            lines.extend([
                "", "[[slots]]", f'slot_id = "e2e-{name}"',
                f"source = {json.dumps(str(source))}",
                f'container_path = "/srv/aegis/roots/e2e-{name}"',
                'mode = "read_only"',
                f"expected_identity = {json.dumps(local_identity(source))}",
            ])
        config = path / "mounts.toml"
        with config.open("x", encoding="ascii") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write("\n".join(lines) + "\n")
        _record(ledger, path, config)
        _write_ledger(path, ledger, descriptor=descriptor)
    finally:
        os.close(descriptor)


def record_generated(path: Path) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger, exact=False)
    for name in GENERATED_NAMES[1:]:
        if name not in ledger["entries"]:
            _record(ledger, path, path / name)
    _write_ledger(path, ledger)
    _validate_ledger(path, ledger)


def _stored_resources(ledger: dict[str, Any]) -> ProjectInventory:
    try:
        resources = tuple(ProjectResource(**entry) for entry in ledger["resources"])
    except (KeyError, TypeError) as exc:
        raise ValueError("synthetic E2E resource ledger is invalid") from exc
    return ProjectInventory(PROJECT, tuple(sorted(resources)))


def resources_check(path: Path) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    require_project_inventory(_stored_resources(ledger))


def _resource_rules(operation: str, token: str) -> tuple[ProjectResourceRule, ...]:
    provenance = ("aegis.test.resource-token", token)
    if operation == "run-web":
        return (ProjectResourceRule("container", (
            ("com.docker.compose.service", "web"),
            ("com.docker.compose.oneoff", "True"),
            provenance,
        )),)
    if operation != "up":
        raise ValueError("unknown E2E resource transition")
    services = ("postgres", "migrate", "web", "operations", "indexer", "media", "gateway")
    networks = ("backend", "edge")
    volumes = (
        "indexer-coordination", "postgres-data", "staging", "derivatives",
        "model-cache", "quarantine", "frontier-outbox",
    )
    return (
        *(ProjectResourceRule("container", (
            ("com.docker.compose.service", service),
            ("com.docker.compose.oneoff", "False"),
            provenance,
        )) for service in services),
        *(ProjectResourceRule("network", (
            ("com.docker.compose.network", network),
            provenance,
        )) for network in networks),
        *(ProjectResourceRule("volume", (
            ("com.docker.compose.volume", volume),
            provenance,
        )) for volume in volumes),
    )


def resources_record(path: Path, operation: str) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    inventory = admit_project_transition(
        _stored_resources(ledger), capture_project_inventory(PROJECT),
        _resource_rules(operation, path.name),
    )
    ledger["resources"] = [
        {
            "kind": resource.kind, "handle": resource.handle,
            "immutable_id": resource.immutable_id, "fingerprint": resource.fingerprint,
        }
        for resource in inventory.resources
    ]
    _write_ledger(path, ledger)
    _validate_ledger(path, ledger)


def resources_cleanup(path: Path) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    cleanup_project_inventory(_stored_resources(ledger))
    ledger["resources"] = []
    _write_ledger(path, ledger)


def _targets(path: Path, ledger: dict[str, Any], names: list[str]) -> list[Path]:
    targets = [path / name for name in names]
    for target in targets:
        if ledger["entries"].get(_relative(path, target)) != _identity(target):
            raise ValueError("synthetic E2E input was replaced")
    return targets


def _run_preparation(path: Path, ledger: dict[str, Any], command: list[str]) -> None:
    _validate_ledger(path, ledger)
    try:
        result = subprocess.run(
            command, check=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("synthetic E2E runtime preparation failed") from exc
    _validate_ledger(path, ledger)
    if result.returncode:
        raise ValueError("synthetic E2E runtime preparation failed")


def prepare_sources(path: Path) -> None:
    if selected_engine() == "docker":
        return
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    roots = _targets(path, ledger, [f"roots/{name}" for name in ROOT_NAMES])
    if any(any(root.iterdir()) for root in roots):
        raise ValueError("synthetic E2E root contains unknown inputs")
    _run_preparation(path, ledger, [
        "chcon", "--no-dereference", "--type", "container_file_t", "--",
        *(str(root) for root in roots),
    ])


def prepare_runtime(path: Path) -> None:
    if selected_engine() == "docker":
        return
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    secret_names = [f"secrets/{name}" for name in SECRET_NAMES]
    secrets_to_own = _targets(path, ledger, secret_names)
    for secret in secrets_to_own:
        if secret.lstat().st_mode & 0o777 != 0o600:
            raise ValueError("synthetic E2E secret has unsafe mode")
    manifest = _targets(path, ledger, ["mounts.manifest.json"])[0]
    attestation = _targets(path, ledger, ["mounts.gateway.attestation"])[0]
    uid = os.environ.get("AEGIS_UID", "")
    gid = os.environ.get("AEGIS_GID", "")
    if not uid.isdecimal() or not gid.isdecimal():
        raise ValueError("AEGIS_UID and AEGIS_GID must be decimal synthetic identities")
    label_targets = [*secrets_to_own, manifest, attestation]
    _run_preparation(path, ledger, [
        "chcon", "--no-dereference", "--type", "container_file_t", "--",
        *(str(target) for target in label_targets),
    ])
    ownership_targets = [*secrets_to_own, manifest]
    _validate_ledger(path, ledger)
    try:
        mapped = subprocess.run(
            container_command(
                "unshare", "chown", "--no-dereference", f"{uid}:{gid}", "--",
                *map(str, ownership_targets),
            ),
            check=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
        verified = subprocess.run(
            container_command(
                "unshare", "stat", "--format=%u:%g", "--",
                *map(str, ownership_targets),
            ),
            check=False, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("synthetic E2E runtime preparation failed") from exc
    if (
        mapped.returncode or verified.returncode
        or verified.stdout.splitlines() != [f"{uid}:{gid}"] * len(ownership_targets)
    ):
        raise ValueError("synthetic E2E ownership mapping failed")
    current = _current_entries(path)
    if current.keys() != ledger["entries"].keys():
        raise ValueError("synthetic E2E ownership inventory changed")
    target_names = {_relative(path, target) for target in ownership_targets}
    for name, previous in ledger["entries"].items():
        observed = current.get(name)
        if observed is None or (
            observed != previous and (
                name not in target_names
                or observed[:3] != previous[:3]
                or observed[5] != previous[5]
            )
        ):
            raise ValueError("synthetic E2E ownership inventory changed")
    for name in target_names:
        ledger["entries"][name] = current[name]
    _write_ledger(path, ledger)
    _validate_ledger(path, ledger)


def check_compose(config: dict[str, Any]) -> None:
    if config["name"] != PROJECT:
        raise ValueError("unexpected E2E project")
    for volume in config["volumes"].values():
        if volume.get("external") or not volume["name"].startswith(f"{PROJECT}_"):
            raise ValueError("E2E volumes must be disposable and project-scoped")
    services = config["services"]
    if not config["networks"]["backend"]["internal"]:
        raise ValueError("E2E backend requires an internal network")
    for role in ("web", "migrate", "operations", "indexer", "media", "gateway"):
        service = services[role]
        if not service["read_only"] or service["cap_drop"] != ["ALL"]:
            raise ValueError("E2E runtime isolation is missing")
        if role != "gateway" and set(service["networks"]) != {"backend"}:
            raise ValueError("E2E backend must have no internet egress")
        roots = [
            volume for volume in service.get("volumes", [])
            if volume["target"].startswith("/srv/aegis/roots/")
        ]
        if role in ("web", "migrate"):
            if roots:
                raise ValueError("web/migrator cannot mount originals")
        elif len(roots) != 2 or any(v.get("read_only") is not True for v in roots):
            raise ValueError("every original mount must be read-only")
        for volume in service.get("volumes", []):
            if volume["type"] == "bind" and Path(volume["source"]).resolve() == REPOSITORY:
                raise ValueError("runtime cannot mount the repository")
    for role in ("postgres", "gateway"):
        if any(p.get("host_ip") != "127.0.0.1" for p in services[role]["ports"]):
            raise ValueError("E2E ports must bind loopback")


def sanitize_line(line: str) -> str:
    safe: dict[str, Any] = {"message": "Service event (details omitted)"}
    if len(line) <= 65_536:
        try:
            data = redact(json.loads(line))
        except (ValueError, RecursionError):
            data = None
        if isinstance(data, dict):
            if data.get("level") in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
                safe["level"] = data["level"]
            if type(data.get("status")) is int and 100 <= data["status"] <= 599:
                safe["status"] = data["status"]
    return json.dumps(safe, separators=(",", ":"))


def cleanup(path: Path) -> None:
    ledger = _load_ledger(path)
    _validate_ledger(path, ledger)
    entries = [path / name for name in ledger["entries"] if name not in (".", LEDGER_NAME)]
    entries.sort(key=lambda target: len(target.parts), reverse=True)
    for target in entries:
        if stat.S_IFMT(target.lstat().st_mode) == stat.S_IFDIR:
            target.rmdir()
        else:
            target.unlink()
    (path / LEDGER_NAME).unlink()
    path.rmdir()


def controlled_compose(arguments: list[str]) -> int:
    environment = compose_environment(os.environ)
    command = controlled_container_argv(
        compose_command(*arguments, environment=environment), environment=environment,
        canonical_base=REPOSITORY / "compose.yaml", working_directory=REPOSITORY,
    )
    return subprocess.run(command, cwd=REPOSITORY, env=environment, check=False).returncode


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "controlled-compose":
        raise SystemExit(controlled_compose(sys.argv[2:]))
    elif action == "check-compose":
        check_compose(json.load(sys.stdin))
    elif action == "sanitize-logs":
        for _ in range(4000):
            line = sys.stdin.readline(65_538)
            if not line:
                break
            print(sanitize_line(line))
    elif action == "resources-record":
        resources_record(checked_directory(sys.argv[2]), sys.argv[3])
    elif action in (
        "prepare", "record-generated", "prepare-sources", "prepare-runtime",
        "resources-check", "resources-cleanup", "cleanup",
    ):
        directory = checked_directory(sys.argv[2])
        {
            "prepare": prepare,
            "record-generated": record_generated,
            "prepare-sources": prepare_sources,
            "prepare-runtime": prepare_runtime,
            "resources-check": resources_check,
            "resources-cleanup": resources_cleanup,
            "cleanup": cleanup,
        }[action](directory)
    else:
        raise ValueError("unknown E2E support action")
