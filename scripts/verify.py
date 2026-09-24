"""Run repository checks with disposable inputs, never an operator database."""
from __future__ import annotations

import argparse
import json
import os
import runpy
import secrets
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from aegisctl.container_engine import (
    compose_command,
    compose_environment,
    container_command,
    sanitized_environment,
    selected_engine,
)

REPOSITORY = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = (
    "docker.io/library/postgres:18.6-alpine@"
    "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
)


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int
    kind: int
    uid: int
    gid: int
    links: int


@dataclass
class VerificationCreation:
    container_id: str = ""
    state_known: bool = False


def file_identity(path: Path) -> FileIdentity:
    metadata = path.lstat()
    return FileIdentity(
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_nlink,
    )


def test_environment() -> dict[str, str]:
    env = sanitized_environment(os.environ)
    return env | {
        "AEGIS_ENV": "test", "DJANGO_SETTINGS_MODULE": "aegis.settings.test",
        "AEGIS_RELEASE_ID": "phase1-verification", "AEGIS_DB_NAME": "aegis",
        "AEGIS_DB_USER": "postgres", "AEGIS_DB_HOST": "127.0.0.1",
    }


def prepare_verification_bind(
    directory: Path, identity: FileIdentity, password_file: Path,
    password_identity: FileIdentity,
) -> None:
    if selected_engine() == "docker":
        return
    expected = {password_file.name}
    if (
        file_identity(directory) != identity
        or identity.kind != stat.S_IFDIR
        or identity.uid != os.geteuid()
        or identity.gid != os.getegid()
        or {entry.name for entry in directory.iterdir()} != expected
        or file_identity(password_file) != password_identity
        or password_identity.kind != stat.S_IFREG
        or password_identity.uid != os.geteuid()
        or password_identity.gid != os.getegid()
        or password_identity.links != 1
    ):
        raise RuntimeError("verification bind inventory changed")
    result = subprocess.run(
        ["chcon", "--no-dereference", "--type", "container_file_t", "--",
         str(directory), str(password_file)],
        check=False, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )
    if (
        result.returncode
        or file_identity(directory) != identity
        or file_identity(password_file) != password_identity
        or {entry.name for entry in directory.iterdir()} != expected
    ):
        raise RuntimeError("verification bind inventory changed")


def cleanup_verification_bind(
    directory: Path,
    directory_identity: FileIdentity,
    files: dict[Path, FileIdentity],
) -> None:
    current_names = {entry.name for entry in directory.iterdir()}
    if (
        file_identity(directory) != directory_identity
        or directory_identity.kind != stat.S_IFDIR
        or current_names != {path.name for path in files}
    ):
        raise RuntimeError("verification bind inventory changed; preserving evidence")
    for path, identity in files.items():
        if (
            file_identity(path) != identity
            or identity.kind != stat.S_IFREG
            or identity.links != 1
        ):
            raise RuntimeError("verification bind inventory changed; preserving evidence")
    for path in files:
        path.unlink()
    directory.rmdir()


def _owned_container_ids(owner: str) -> tuple[str, ...]:
    queried = container(
        "container", "ls", "--all", "--no-trunc", "--quiet", "--filter",
        f"label=aegis.verify.owner={owner}", check=False,
    )
    if queried.returncode:
        raise RuntimeError("verification container query failed; preserving evidence")
    handles = queried.stdout.split()
    if len(handles) != len(set(handles)) or len(handles) > 1:
        raise RuntimeError("verification container recovery is ambiguous")
    identities: list[str] = []
    for handle in handles:
        inspected = container("container", "inspect", handle, check=False)
        if inspected.returncode:
            raise RuntimeError("verification container inspection failed; preserving evidence")
        try:
            payload = json.loads(inspected.stdout)
            info = payload[0]
            identity = info["Id"]
            label = info["Config"]["Labels"].get("aegis.verify.owner")
            if len(payload) != 1 or not isinstance(identity, str) or not identity:
                raise (ValueError())
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RuntimeError(
                "verification container inspection failed; preserving evidence"
            ) from exc
        if label != owner:
            raise RuntimeError("verification container recovery found a replacement")
        identities.append(identity)
    if len(identities) != len(set(identities)):
        raise RuntimeError("verification container recovery is ambiguous")
    return tuple(sorted(identities))


def _record_cidfile(
    cidfile: Path, files: dict[Path, FileIdentity],
) -> str:
    if not cidfile.exists():
        return ""
    identity = file_identity(cidfile)
    if (
        identity.kind != stat.S_IFREG or identity.links != 1
        or identity.uid != os.geteuid() or identity.gid != os.getegid()
    ):
        raise RuntimeError("verification container recovery cidfile is unsafe")
    descriptor = os.open(cidfile, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if file_identity(cidfile) != identity:
            raise RuntimeError("verification container recovery cidfile changed")
        raw = os.read(descriptor, 129)
    finally:
        os.close(descriptor)
    if len(raw) > 128:
        raise RuntimeError("verification container recovery cidfile is invalid")
    try:
        candidate = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("verification container recovery cidfile is invalid") from exc
    if not candidate or any(character not in "0123456789abcdefABCDEF" for character in candidate):
        raise RuntimeError("verification container recovery cidfile is invalid")
    files[cidfile] = identity
    return candidate


def create_verification_container(
    owner: str,
    cidfile: Path,
    files: dict[Path, FileIdentity],
    record: VerificationCreation,
    *arguments: str,
) -> str:
    result: subprocess.CompletedProcess[str] | None = None
    create_error: Exception | None = None
    interruption: KeyboardInterrupt | SystemExit | None = None
    try:
        result = container(
            "create", "--cidfile", str(cidfile), "--name", owner,
            "--label", f"aegis.verify.owner={owner}", *arguments,
            check=False,
        )
    except (KeyboardInterrupt, SystemExit) as exc:
        interruption = exc
    except Exception as exc:  # Recovery must run after transport/time-limit failures.
        create_error = exc
    try:
        candidate = _record_cidfile(cidfile, files)
        identities = _owned_container_ids(owner)
        if candidate:
            inspected = container("container", "inspect", candidate, check=False)
            if inspected.returncode:
                raise RuntimeError("verification container recovery inspection failed")
            try:
                payload = json.loads(inspected.stdout)
                canonical = payload[0]["Id"]
                label = payload[0]["Config"]["Labels"].get("aegis.verify.owner")
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                raise RuntimeError("verification container recovery inspection failed") from exc
            if not isinstance(canonical, str) or not canonical or label != owner:
                raise RuntimeError("verification container recovery found a replacement")
            if identities != (canonical,):
                raise RuntimeError("verification container recovery is ambiguous")
            record.container_id = canonical
        elif len(identities) == 1:
            record.container_id = identities[0]
        elif identities:
            raise RuntimeError("verification container recovery is ambiguous")
        record.state_known = True
    except Exception as exc:
        if interruption is not None:
            raise interruption from exc
        raise RuntimeError("verification container recovery failed; preserving evidence") from exc
    if interruption is not None:
        raise interruption
    if create_error is not None or result is None or result.returncode:
        raise RuntimeError("verification container creation failed") from create_error
    if not record.container_id:
        raise RuntimeError("verification container creation returned no owned identity")
    return record.container_id


def cleanup_verification_resources(
    owner: str,
    container_id: str,
    directory: Path,
    directory_identity: FileIdentity,
    files: dict[Path, FileIdentity],
) -> bool:
    # Validate every filesystem and resource identity before the first mutation.
    validate_verification_bind(directory, directory_identity, files)
    current = _owned_container_ids(owner)
    if container_id:
        if current != (container_id,):
            raise RuntimeError("verification container inventory changed; preserving evidence")
        validate_verification_bind(directory, directory_identity, files)
        removed = container("rm", "--force", container_id, check=False)
        if removed.returncode:
            raise RuntimeError("verification container cleanup failed; preserving evidence")
        if _owned_container_ids(owner):
            raise RuntimeError("verification container absence is unproved; preserving evidence")
        _accept_removed_podman_cidfile(directory, directory_identity, files)
    elif current:
        raise RuntimeError("verification container inventory changed; preserving evidence")
    validate_verification_bind(directory, directory_identity, files)
    cleanup_verification_bind(directory, directory_identity, files)
    return bool(container_id)


def _accept_removed_podman_cidfile(
    directory: Path,
    directory_identity: FileIdentity,
    files: dict[Path, FileIdentity],
) -> None:
    if selected_engine() != "podman":
        return
    cidfiles = [path for path in files if path.name == "container.cid"]
    if not cidfiles:
        return
    if len(cidfiles) != 1:
        raise RuntimeError("verification bind inventory changed; preserving evidence")
    cidfile = cidfiles[0]
    try:
        cidfile.lstat()
    except FileNotFoundError:
        retained = {path: identity for path, identity in files.items() if path != cidfile}
        validate_verification_bind(directory, directory_identity, retained)
        del files[cidfile]
    except OSError as exc:
        raise RuntimeError("verification bind inventory changed; preserving evidence") from exc


def validate_verification_bind(
    directory: Path,
    directory_identity: FileIdentity,
    files: dict[Path, FileIdentity],
) -> None:
    current_names = {entry.name for entry in directory.iterdir()}
    if (
        file_identity(directory) != directory_identity
        or directory_identity.kind != stat.S_IFDIR
        or current_names != {path.name for path in files}
    ):
        raise RuntimeError("verification bind inventory changed; preserving evidence")
    for path, identity in files.items():
        if file_identity(path) != identity or identity.kind != stat.S_IFREG or identity.links != 1:
            raise RuntimeError("verification bind inventory changed; preserving evidence")


def container(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    environment = sanitized_environment(os.environ)
    result = subprocess.run(
        container_command(*arguments, environment=environment), env=environment,
        capture_output=True, text=True, timeout=120,
    )
    if check and result.returncode:
        raise RuntimeError("disposable verification database command failed")
    return result


def compose(
    *arguments: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    source = sanitized_environment(os.environ) if env is None else env
    environment = compose_environment(source)
    result = subprocess.run(
        compose_command(*arguments, environment=environment), cwd=REPOSITORY, env=environment,
        capture_output=True, text=True, timeout=120,
    )
    if check and result.returncode:
        raise RuntimeError("disposable verification Compose command failed")
    return result


@contextmanager
def test_database() -> Iterator[dict[str, str]]:
    name = f"aegis-verify-{uuid.uuid4().hex}"
    directory = Path(tempfile.mkdtemp(prefix="aegis-verify-")).resolve()
    directory_metadata = directory.lstat()
    if (
        not stat.S_ISDIR(directory_metadata.st_mode)
        or directory_metadata.st_uid != os.geteuid()
        or directory_metadata.st_mode & 0o077
        or any(directory.iterdir())
    ):
        raise RuntimeError("verification directory was not created empty")
    directory_identity = file_identity(directory)
    password_file = directory / "password"
    password = secrets.token_hex(32)
    with password_file.open("x", encoding="ascii") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(password + "\n")
    password_identity = file_identity(password_file)
    prepare_verification_bind(
        directory, directory_identity, password_file, password_identity,
    )
    env = test_environment()
    creation = VerificationCreation()
    cidfile = directory / "container.cid"
    file_inventory = {password_file: password_identity}
    try:
        container_id = create_verification_container(
            name, cidfile, file_inventory, creation,
            "--publish", "127.0.0.1::5432", "--memory", "512m", "--pids-limit", "128",
            "--tmpfs", "/var/lib/postgresql:rw,nosuid,nodev,size=384m",
            "--mount", f"type=bind,src={password_file},dst=/run/test-password,readonly",
            "--env", "POSTGRES_PASSWORD_FILE=/run/test-password",
            "--env", "POSTGRES_DB=aegis", POSTGRES_IMAGE,
        )
        container("start", container_id)
        binding = container("port", name, "5432/tcp").stdout.strip()
        host, port = binding.rsplit(":", 1)
        if host != "127.0.0.1" or not port.isdecimal():
            raise RuntimeError("verification database must bind loopback")
        env |= {"AEGIS_DB_PORT": port, "AEGIS_DB_PASSWORD_FILE": str(password_file)}
        deadline = time.monotonic() + 60
        while True:
            try:
                with psycopg.connect(
                    host=host, port=int(port), dbname="aegis", user="postgres",
                    password=password, connect_timeout=2,
                ):
                    break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("verification database did not become ready") from None
                time.sleep(0.2)
        yield env
    finally:
        if creation.state_known:
            removed = cleanup_verification_resources(
                name, creation.container_id, directory, directory_identity, file_inventory,
            )
            if removed:
                print(
                    "Removed disposable verification database (memory-only data).",
                    flush=True,
                )


def run(arguments: list[str], env: dict[str, str]) -> None:
    subprocess.run(arguments, cwd=REPOSITORY, env=env, check=True)


def verify_compose() -> None:
    support = runpy.run_path(str(REPOSITORY / "scripts/e2e_support.py"))
    directory = Path(tempfile.mkdtemp(prefix="aegis-phase1-e2e.", dir="/tmp")).resolve()
    support["checked_directory"](str(directory))
    env = test_environment() | {
        "AEGIS_TEST_SECRET_DIR": str(directory / "secrets"),
        "AEGIS_TEST_RESOURCE_TOKEN": directory.name,
    }
    command = (
        "--env-file", "/dev/null", "--project-name", "aegis-phase1-e2e",
        "-f", "compose.yaml", "-f", "compose.test.yaml",
    )
    try:
        support["prepare"](directory)
        compose(*command, "config", "--quiet", env=env)
        compose(*command, "build", "web", "gateway", "postgres", env=env)
    finally:
        support["cleanup"](directory)


def test_targets(mode: str, targets: list[str], parser: argparse.ArgumentParser) -> list[str]:
    tree = "backend/tests" if mode == "backend" else "tests/deployment"
    allowed = (REPOSITORY / tree).resolve()
    validated = []
    for target in targets:
        filename, *selectors = target.split("::")
        path = Path(filename)
        if not filename or filename.startswith("-") or path.is_absolute() or ".." in path.parts:
            parser.error(
                "test targets must be repository-relative paths beneath the mode's test tree",
            )
        if any(not selector.isidentifier() for selector in selectors):
            parser.error("test selectors must be nonempty identifiers separated by ::")
        try:
            resolved = (REPOSITORY / path).resolve()
            valid = resolved.is_relative_to(allowed) and (resolved.is_file() or resolved.is_dir())
        except (OSError, ValueError, RuntimeError):
            valid = False
        if not valid:
            parser.error("test target must exist beneath the mode's test tree")
        if selectors and not resolved.is_file():
            parser.error("test selectors require a file target")
        validated.append("::".join([resolved.relative_to(REPOSITORY).as_posix(), *selectors]))
    return validated or [tree]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("backend", "deployment", "compose"))
    parser.add_argument("--test-target", action="append", default=[], metavar="PATH[::IDENTIFIER]")
    arguments = parser.parse_args()
    if arguments.mode == "compose":
        if arguments.test_target:
            parser.error("compose mode does not accept test targets")
        verify_compose()
        return
    targets = test_targets(arguments.mode, arguments.test_target, parser)
    with test_database() as env:
        if arguments.mode == "backend":
            run([sys.executable, "backend/manage.py", "check"], env)
            run([
                sys.executable, "backend/manage.py", "makemigrations", "--check", "--dry-run",
            ], env)
        run([sys.executable, "-m", "pytest", *targets, "-q", "--tb=short"], env)


if __name__ == "__main__":
    def terminate(_signal: int, _frame: object) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    main()
