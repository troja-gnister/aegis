"""Run repository checks with disposable inputs, never an operator database."""
from __future__ import annotations

import argparse
import json
import os
import runpy
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg

REPOSITORY = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = (
    "postgres:18.6-alpine@"
    "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
)


def test_environment() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("AEGIS_", "E2E_", "COMPOSE_", "DJANGO_", "PG"))
    }
    return env | {
        "AEGIS_ENV": "test", "DJANGO_SETTINGS_MODULE": "aegis.settings.test",
        "AEGIS_RELEASE_ID": "phase1-verification", "AEGIS_DB_NAME": "aegis",
        "AEGIS_DB_USER": "postgres", "AEGIS_DB_HOST": "127.0.0.1",
    }


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["docker", *arguments], capture_output=True, text=True, timeout=120,
    )
    if check and result.returncode:
        raise RuntimeError("disposable verification database command failed")
    return result


@contextmanager
def test_database() -> Iterator[dict[str, str]]:
    name = f"aegis-verify-{uuid.uuid4().hex}"
    directory = Path(tempfile.mkdtemp(prefix="aegis-verify-")).resolve()
    password_file = directory / "password"
    password = secrets.token_hex(32)
    with password_file.open("x", encoding="ascii") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(password + "\n")
    env = test_environment()
    try:
        docker(
            "run", "--detach", "--rm", "--name", name,
            "--label", f"aegis.verify.owner={name}",
            "--publish", "127.0.0.1::5432", "--memory", "512m", "--pids-limit", "128",
            "--tmpfs", "/var/lib/postgresql:rw,nosuid,nodev,size=384m",
            "--mount", f"type=bind,src={password_file},dst=/run/test-password,readonly",
            "--env", "POSTGRES_PASSWORD_FILE=/run/test-password",
            "--env", "POSTGRES_DB=aegis", POSTGRES_IMAGE,
        )
        binding = docker("port", name, "5432/tcp").stdout.strip()
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
        try:
            inspected = docker("container", "inspect", name, check=False)
            if inspected.returncode == 0:
                info = json.loads(inspected.stdout)[0]
                if info["Config"]["Labels"].get("aegis.verify.owner") != name:
                    raise RuntimeError("refusing cleanup of an unowned container")
                docker("rm", "--force", info["Id"])
                print("Removed disposable verification database (memory-only data).", flush=True)
        finally:
            password_file.unlink()
            directory.rmdir()


def run(arguments: list[str], env: dict[str, str]) -> None:
    subprocess.run(arguments, cwd=REPOSITORY, env=env, check=True)


def verify_compose() -> None:
    support = runpy.run_path(str(REPOSITORY / "scripts/e2e_support.py"))
    directory = Path(tempfile.mkdtemp(prefix="aegis-phase1-e2e.", dir="/tmp")).resolve()
    support["checked_directory"](str(directory))
    env = test_environment() | {"AEGIS_TEST_SECRET_DIR": str(directory / "secrets")}
    command = [
        "docker", "compose", "--env-file", "/dev/null", "--project-name", "aegis-phase1-e2e",
        "-f", "compose.yaml", "-f", "compose.test.yaml",
    ]
    try:
        support["prepare"](directory)
        run([*command, "config", "--quiet"], env)
        run([*command, "build", "web", "gateway", "postgres"], env)
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
