"""Private generated inputs and bounded diagnostics for disposable browser tests."""
from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

from aegis_apps.common.redaction import redact
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


def checked_directory(value: str) -> Path:
    path = Path(value)
    if (
        path.is_symlink() or path.parent.resolve() != Path("/tmp").resolve()
        or not path.name.startswith(f"{PROJECT}.")
        or len(path.name.removeprefix(f"{PROJECT}.")) != 8
        or path.stat().st_uid != os.geteuid()
        or path.stat().st_mode & 0o077
    ):
        raise ValueError("invalid E2E temporary directory")
    return path


def prepare(path: Path) -> None:
    secret_dir = path / "secrets"
    secret_dir.mkdir(mode=0o700)
    for name in SECRET_NAMES:
        with (secret_dir / name).open("x", encoding="ascii") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(secrets.token_hex(32) + "\n")
    lines = ["version = 1"]
    for name in ("alice", "bob"):
        source = REPOSITORY / "tests" / "fixtures" / "roots" / name
        lines.extend([
            "", "[[slots]]", f'slot_id = "e2e-{name}"',
            f"source = {json.dumps(str(source))}",
            f'container_path = "/srv/aegis/roots/e2e-{name}"',
            'mode = "read_only"',
            f"expected_identity = {json.dumps(local_identity(source))}",
        ])
    with (path / "mounts.toml").open("x", encoding="ascii") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write("\n".join(lines) + "\n")


def check_compose(config: dict) -> None:
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
            v for v in service.get("volumes", [])
            if v["target"].startswith("/srv/aegis/roots/")
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
    # Free text may contain SQL, paths, or credentials. Keep typed counters only.
    safe: dict = {"message": "Service event (details omitted)"}
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
    # Remove known generated files only; unexpected content makes rmdir fail.
    for name in GENERATED_NAMES:
        (path / name).unlink(missing_ok=True)
    secret_dir = path / "secrets"
    if secret_dir.exists():
        if secret_dir.is_symlink():
            raise ValueError("invalid E2E secret directory")
        for name in SECRET_NAMES:
            (secret_dir / name).unlink(missing_ok=True)
        secret_dir.rmdir()
    path.rmdir()


if __name__ == "__main__":
    action = sys.argv[1]
    if action == "check-compose":
        check_compose(json.load(sys.stdin))
    elif action == "sanitize-logs":
        for _ in range(4000):
            line = sys.stdin.readline(65_538)
            if not line:
                break
            print(sanitize_line(line))
    elif action in ("prepare", "cleanup"):
        directory = checked_directory(sys.argv[2])
        (prepare if action == "prepare" else cleanup)(directory)
    else:
        raise ValueError("unknown E2E support action")
