"""Pinned Playwright WebKit server container for Podman e2e runs.

Fedora cannot run Playwright's Ubuntu WebKit build, so the e2e suite connects to
this locally built server instead. Every removal is by recorded full container ID.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import socket
import time
from collections.abc import Callable
from pathlib import Path

from scripts.podman_dev import podman
from scripts.podman_dev.records import SessionError, WebkitRecord

REPOSITORY = Path(__file__).resolve().parents[2]
DOCKERFILE = Path("docker/dev-webkit.Dockerfile")
IMAGE_INPUTS = (DOCKERFILE, Path("frontend/package.json"), Path("frontend/package-lock.json"))
IMAGE_REPOSITORY = "localhost/aegis-dev-webkit"
E2E_GATEWAY_PORT = 18080
SERVER_CLI = "/opt/aegis-webkit/node_modules/@playwright/test/cli.js"
BUILD_TIMEOUT = 1800.0
FULL_ID = re.compile(r"[0-9a-f]{64}")


def image_tag(repository: Path = REPOSITORY) -> str:
    """Content-addressed tag: any change to an image input forces a rebuild."""
    digest = hashlib.sha256()
    for relative in IMAGE_INPUTS:
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update((repository / relative).read_bytes() + b"\0")
    return f"{IMAGE_REPOSITORY}:{digest.hexdigest()[:16]}"


def ensure_image(run: podman.Runner = podman.run, repository: Path = REPOSITORY) -> str:
    tag = image_tag(repository)
    found = run(["image", "inspect", "--format", "{{.Id}}", tag])
    if found.returncode != 0:
        built = run(
            ["build", "--file", str(repository / DOCKERFILE), "--tag", tag, str(repository)],
            timeout=BUILD_TIMEOUT,
        )
        if built.returncode != 0:
            raise SessionError("building the WebKit image failed:\n" + built.stderr[-4000:])
        found = run(["image", "inspect", "--format", "{{.Id}}", tag])
    image_id = found.stdout.strip()
    if found.returncode != 0 or not FULL_ID.fullmatch(image_id):
        raise SessionError(f"the WebKit image {tag} has no valid image ID")
    return image_id


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def create_arguments(image_id: str, port: int, token: str) -> list[str]:
    # The name is cosmetic (removal always uses the full ID) and never carries the token.
    return [
        "create", "--name", f"aegis-dev-webkit-{port}",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--ipc", "private", "--shm-size", "256m",
        "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--env", "HOME=/tmp",
        "--network", f"pasta:-T,{E2E_GATEWAY_PORT}",
        "--publish", f"127.0.0.1:{port}:{port}",
        image_id,
        "node", SERVER_CLI, "run-server", "--host", "0.0.0.0",
        "--port", str(port), "--path", f"/{token}",
    ]


def start_webkit(
    on_created: Callable[[WebkitRecord], None],
    run: podman.Runner = podman.run,
    *,
    repository: Path = REPOSITORY,
    port: int | None = None,
    token: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> WebkitRecord:
    image_id = ensure_image(run, repository)
    port = free_loopback_port() if port is None else port
    token = secrets.token_hex(16) if token is None else token
    created = run(create_arguments(image_id, port, token))
    container_id = created.stdout.strip()
    if created.returncode != 0 or not FULL_ID.fullmatch(container_id):
        raise SessionError("creating the WebKit server failed:\n" + created.stderr[-4000:])
    record = WebkitRecord(container_id=container_id, image_id=image_id, port=port, token=token)
    on_created(record)
    started = run(["start", container_id])
    if started.returncode != 0:
        raise SessionError("starting the WebKit server failed:\n" + started.stderr[-4000:])
    _wait_for_listener(record, run, sleep=sleep, now=now)
    return record


def _wait_for_listener(
    record: WebkitRecord, run: podman.Runner, *, sleep: Callable[[float], None],
    now: Callable[[], float], deadline_seconds: float = 60.0,
) -> None:
    end = now() + deadline_seconds
    while True:
        state = run(
            ["container", "inspect", "--format", "{{.State.Running}}", record.container_id]
        )
        if state.returncode != 0 or state.stdout.strip() != "true":
            raise SessionError("the WebKit server container stopped during startup; run stop")
        logs = run(["logs", record.container_id])
        if "Listening on" in logs.stdout + logs.stderr:
            return
        if now() >= end:
            raise SessionError("the WebKit server did not listen within 60 s; run stop")
        sleep(0.5)


def webkit_running(record: WebkitRecord, run: podman.Runner = podman.run) -> bool:
    state = run([
        "container", "inspect", "--format", "{{.Id}} {{.Image}} {{.State.Running}}",
        record.container_id,
    ])
    return state.returncode == 0 and state.stdout.split() == [
        record.container_id, record.image_id, "true",
    ]


def remove_webkit(record: WebkitRecord, run: podman.Runner = podman.run) -> None:
    exists = run(["container", "exists", record.container_id])
    if exists.returncode == 1:
        return
    if exists.returncode != 0:
        raise SessionError("could not tell whether the WebKit container exists; left alone")
    inspected = run(
        ["container", "inspect", "--format", "{{.Id}} {{.Image}}", record.container_id]
    )
    if inspected.returncode != 0 or inspected.stdout.split() != [
        record.container_id, record.image_id,
    ]:
        raise SessionError(
            f"container {record.container_id[:12]} is not the recorded WebKit server; "
            "left in place"
        )
    removed = run(["rm", "--force", "--time", "5", record.container_id])
    if removed.returncode != 0:
        raise SessionError("removing the WebKit container failed:\n" + removed.stderr[-2000:])
    if run(["container", "exists", record.container_id]).returncode != 1:
        raise SessionError("the WebKit container still exists after removal")
