"""Task 7 Linux process evidence using only owned synthetic sources/resources.

Every test container is created unstarted, its engine-written ID is recorded and checked
before any payload runs, and cleanup validates the whole owned scope before removing
exactly the recorded immutable IDs. The module image is pinned by its built ID.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from aegisctl.container_engine import ContainerEngineError, container_command, selected_engine
from aegisctl.container_launch import controlled_container_argv
from aegisctl.mounts import parse_mountinfo

from tests.support.container_runtime import (
    OwnedDirectResource,
    OwnedDirectScope,
    cleanup_owned_resources,
    prepare_owned_test_inventory,
    read_optional_cidfile,
    record_created_test_path,
    record_fresh_test_tree,
    record_test_tree_inventory,
    recover_owned_resource,
    validate_owned_resources,
)
from tests.support.container_runtime import (
    run_deployment_process as run_container,
)
from tests.support.database_roles import RoleDatabase

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()
OWNER_LABEL = "aegis.indexer.owner"
RESOURCE_LABEL = "aegis.indexer.resource"
COORDINATION_TARGET = "/srv/aegis/indexer-coordination"
POWERCAP = "/sys/devices/virtual/powercap"
ZERO_CAPABILITIES = "0000000000000000"
# Inspect form of `--cap-drop ALL`: Docker keeps ["ALL"]; rootless Podman 5.8.7 expands it
# (root-measured on a created, never-started container). /proc capability sets are the proof.
PODMAN_CAP_DROP_ALL = (
    "CAP_CHOWN", "CAP_DAC_OVERRIDE", "CAP_FOWNER", "CAP_FSETID", "CAP_KILL",
    "CAP_NET_BIND_SERVICE", "CAP_SETFCAP", "CAP_SETGID", "CAP_SETPCAP", "CAP_SETUID",
    "CAP_SYS_CHROOT",
)
_IMAGE_IDENTITY = re.compile(r"(?:sha256:)?([0-9a-f]{64})")
_ROOT_LOCK = re.compile(r"root-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.lock")


def coordination_tmpfs(owner: tuple[int, int] | None) -> str:
    """Exact private coordination tmpfs for the selected engine; not a generic translator.

    ``owner`` is a positive fixture's process user (the dynamic test user or 501:20) and
    ``None`` the deliberately misowned case. Docker keeps its accepted strings. Podman
    rejects uid/gid: positives take the owner from the process user with U and start
    empty with notmpcopyup; the misowned mount stays namespace-root owned, so it has
    neither U nor uid/gid. Its size stays undeclared on both engines.
    """
    if owner is not None and owner not in {(os.geteuid(), os.getegid()), (501, 20)}:
        raise ValueError("coordination tmpfs owner is not a known fixture identity")
    if selected_engine() == "docker":
        if owner is None:
            return f"{COORDINATION_TARGET}:rw,nosuid,nodev,uid=0,gid=0,mode=0777"
        return (f"{COORDINATION_TARGET}:rw,nosuid,nodev,size=1m,"
                f"uid={owner[0]},gid={owner[1]},mode=0700")
    if owner is None:
        return f"{COORDINATION_TARGET}:rw,nosuid,nodev,notmpcopyup,mode=0777"
    return f"{COORDINATION_TARGET}:rw,nosuid,nodev,size=1m,U,notmpcopyup,mode=0700"


def _engine(*arguments: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Owned-resource query/removal runner; the checked launcher passes these through."""
    return run_container(
        [*CONTAINER_COMMAND, *arguments], capture_output=True, text=True, timeout=timeout,
        check=False,
    )


def _inspect_one(*arguments: str) -> dict[str, Any]:
    inspected = _engine(*arguments)
    try:
        if inspected.returncode:
            raise ValueError(inspected.stderr[-2000:])
        payload = json.loads(inspected.stdout)
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ValueError("ambiguous inspection")
    except ValueError as exc:
        raise ContainerEngineError("owned test resource inspection failed") from exc
    return payload[0]


def _image_identity(value: object) -> str:
    """Normalize an engine image ID; Docker prefixes sha256:, Podman inspect does not."""
    match = _IMAGE_IDENTITY.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ContainerEngineError("test image identity is invalid")
    return match[1]


def _cleanup_preserving_failure(
    primary: BaseException | None, cleanup: Callable[[], None],
) -> None:
    """Run exact cleanup; a refusal is attached to, never substituted for, a failure."""
    try:
        cleanup()
    except Exception as refused:
        if primary is None:
            raise
        primary.add_note(
            "exact owned cleanup refused; resources retained for diagnosis: "
            f"{type(refused).__name__}: {refused}"
        )


def _read_iidfile(path: Path) -> str:
    """Read the engine-written image ID without following aliases; '' when unusable."""
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
    match = _IMAGE_IDENTITY.fullmatch(raw.decode("ascii", "replace").strip())
    return match[1] if match is not None and len(raw) <= 128 else ""


def _require_owned_image(identity: str, tag: str) -> dict[str, Any]:
    """Refuse a replaced image, owner-label drift or a rebound unique tag."""
    info = _inspect_one("image", "inspect", identity)
    if _image_identity(info.get("Id")) != identity:
        raise ContainerEngineError("owned test image was replaced")
    if ((info.get("Config") or {}).get("Labels") or {}).get(OWNER_LABEL) != tag:
        raise ContainerEngineError("owned test image ownership changed")
    if _image_identity(_inspect_one("image", "inspect", tag).get("Id")) != identity:
        raise ContainerEngineError("owned test image tag was rebound")
    return info


def _listed_images(*arguments: str) -> set[str]:
    listed = _engine("image", "ls", "--no-trunc", "--quiet", *arguments)
    if listed.returncode:
        raise ContainerEngineError("owned test image query failed")
    return {_image_identity(line) for line in listed.stdout.split()}


def _remove_owned_image(identity: str, tag: str) -> None:
    """Validate the recorded ID, label and tag, then remove exactly that ID and prove it."""
    if not identity:
        # Unknown creation: never adopt an image found by its reusable tag or label.
        if _listed_images("--filter", f"reference={tag}"):
            raise ContainerEngineError("test image identity was never recorded; tag retained")
        return
    _require_owned_image(identity, tag)
    if _engine("image", "rm", identity).returncode:
        raise ContainerEngineError("exact owned test image removal failed")
    if identity in _listed_images("--all") or _listed_images("--filter", f"reference={tag}"):
        raise ContainerEngineError("owned test image absence is unproved")


def _pinned_local_image(reference: str) -> str:
    """Resolve a local image once; launches then name only its immutable ID (never pull)."""
    info = _inspect_one("image", "inspect", reference)
    if (info.get("Config") or {}).get("Volumes"):
        raise ContainerEngineError("test image declares anonymous volumes")
    return _image_identity(info.get("Id"))


@dataclass
class _Workload:
    """One named test container; its immutable ID is recorded before any payload runs."""

    name: str
    image: str
    cidfile: Path
    recorded: list[OwnedDirectResource] = field(default_factory=list)

    @property
    def scope(self) -> OwnedDirectScope:
        return OwnedDirectScope("container", OWNER_LABEL, self.name)


def _new_workload(
    prefix: str, image: str, tmp_path_factory: pytest.TempPathFactory,
) -> _Workload:
    directory = tmp_path_factory.mktemp("indexer-workload")
    return _Workload(f"{prefix}-{uuid.uuid4().hex}", image, directory / "container.cid")


def _require_workload(workload: _Workload, *, unstarted: bool) -> dict[str, Any]:
    """Check the recorded ID's exact name, pinned image, both labels and no anonymous volume."""
    (resource,) = workload.recorded
    info = _inspect_one("container", "inspect", resource.immutable_id)
    labels = (info.get("Config") or {}).get("Labels") or {}
    if (
        info.get("Id") != resource.immutable_id
        or str(info.get("Name", "")).removeprefix("/") != workload.name
        or labels.get(OWNER_LABEL) != workload.name
        or labels.get(RESOURCE_LABEL) != workload.name
    ):
        raise ContainerEngineError("owned test container identity changed")
    if _image_identity(info.get("Image")) != workload.image:
        raise ContainerEngineError("owned test container is not the pinned image")
    if any(mount.get("Type") == "volume" for mount in info.get("Mounts") or []):
        raise ContainerEngineError("owned test container has an anonymous volume")
    if unstarted and (info.get("State") or {}).get("Status") != "created":
        raise ContainerEngineError("owned test container ran before its identity was checked")
    return info


def _recover_created(workload: _Workload) -> OwnedDirectResource | None:
    """Recover only this create: its CID file plus unique labels; None if provably none."""
    candidate = read_optional_cidfile(workload.cidfile)
    try:
        return recover_owned_resource(
            "container", candidate, OWNER_LABEL, workload.name,
            RESOURCE_LABEL, workload.name, _engine,
        )
    except ContainerEngineError:
        # Only an empty owned scope proves nothing was created; anything else is retained.
        validate_owned_resources((), _engine, scopes=(workload.scope,))
        if candidate:
            raise
        return None


def _create_workload(workload: _Workload, launch: Sequence[str]) -> dict[str, Any]:
    """Create unstarted and record the immutable ID before any payload can run.

    ``launch`` carries each test's unchanged isolation options, pinned image and payload.
    Failed, interrupted or ambiguous creation is recorded or retained, never guessed.
    """
    created: subprocess.CompletedProcess[str] | None = None
    failure: BaseException | None = None
    try:
        created = run_container(
            [*CONTAINER_COMMAND, "create", "--cidfile", str(workload.cidfile),
             "--pull", "never", "--name", workload.name,
             "--label", f"{OWNER_LABEL}={workload.name}",
             "--label", f"{RESOURCE_LABEL}={workload.name}", *launch],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except BaseException as exc:  # identity recovery still runs after interrupts/timeouts
        failure = exc
    try:
        resource = _recover_created(workload)
    except Exception as unrecovered:
        if failure is not None:
            raise failure from unrecovered
        if created is not None and created.returncode:
            raise AssertionError(
                "test container create failed: " + created.stderr[-5000:]
            ) from unrecovered
        raise
    if resource is not None:
        workload.recorded.append(resource)
    if failure is not None:
        raise failure
    if resource is None or created is None or created.returncode:
        raise AssertionError(
            "test container create failed: " + (created.stderr[-5000:] if created else "")
        )
    return _require_workload(workload, unstarted=True)


def _start_command(workload: _Workload, *, interactive: bool = False) -> list[str]:
    (resource,) = workload.recorded
    return [
        *CONTAINER_COMMAND, "start", "--attach", *(["--interactive"] if interactive else []),
        resource.immutable_id,
    ]


def _start_workload(workload: _Workload, *, timeout: int) -> subprocess.CompletedProcess[str]:
    return run_container(
        _start_command(workload), capture_output=True, text=True, timeout=timeout, check=False,
    )


def _remove_recorded_workload(workload: _Workload) -> None:
    """Validate the whole owned scope before the first deletion; remove recorded IDs only."""
    if workload.recorded:
        _require_workload(workload, unstarted=False)
    cleanup_owned_resources(tuple(workload.recorded), _engine, scopes=(workload.scope,))
    listed = _engine("container", "ls", "--all", "--no-trunc", "--quiet")
    recorded = {resource.immutable_id for resource in workload.recorded}
    if listed.returncode or recorded & set(listed.stdout.split()):
        raise ContainerEngineError("owned test container absence is unproved")


def _cleanup_workload(workload: _Workload, primary: BaseException | None) -> None:
    _cleanup_preserving_failure(primary, lambda: _remove_recorded_workload(workload))


@pytest.fixture(scope="module")
def indexer_image(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Build one owned image and yield its pinned immutable ID; remove exactly that ID."""
    name = f"aegis-task7-{uuid.uuid4().hex}"
    iidfile = tmp_path_factory.mktemp("indexer-image") / "image.iid"
    identity = ""
    primary: BaseException | None = None
    try:
        try:
            built = run_container(
                [*CONTAINER_COMMAND, "build", "--iidfile", str(iidfile), "--tag", name,
                 "--label", f"{OWNER_LABEL}={name}",
                 "--build-arg", "AEGIS_UID=501", "--build-arg", "AEGIS_GID=20",
                 "--file", "docker/backend.Dockerfile", "."], cwd=REPOSITORY,
                capture_output=True, text=True, timeout=300, check=False,
            )
        finally:
            # The engine-written ID is recorded even when the build call is interrupted.
            identity = _read_iidfile(iidfile)
        assert built.returncode == 0, built.stderr[-5000:]
        assert identity, "built image identity was not recorded"
        info = _require_owned_image(identity, name)
        assert not (info.get("Config") or {}).get("Volumes"), "image declares anonymous volumes"
        yield identity
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _cleanup_preserving_failure(primary, lambda: _remove_owned_image(identity, name))


def test_new_coordination_volume_supports_nondefault_uid_gid(indexer_image: str) -> None:
    volume = f"aegis-task7-coordination-{uuid.uuid4().hex}"
    run_container(
        [*CONTAINER_COMMAND, "volume", "create", "--label",
         f"aegis.indexer.owner={volume}", volume],
        capture_output=True, timeout=20, check=True,
    )
    try:
        probe = (
            "import os; from aegis_apps.indexing.processes import Coordination; "
            "s=os.stat('/srv/aegis/indexer-coordination'); "
            "assert (s.st_uid,s.st_gid)==(501,20); c=Coordination(); c.close(); print('owned')"
        )
        result = run_container(
            [*CONTAINER_COMMAND, "run", "--rm", "--init", "--network", "none", "--read-only",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "501:20",
             "--mount", f"type=volume,src={volume},dst=/srv/aegis/indexer-coordination",
             "--entrypoint", "python", indexer_image, "-c", probe],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "owned"
    finally:
        info = json.loads(run_container([*CONTAINER_COMMAND, "volume", "inspect", volume],
                          capture_output=True, text=True, timeout=20, check=True).stdout)[0]
        assert info["Labels"]["aegis.indexer.owner"] == volume
        run_container([*CONTAINER_COMMAND, "volume", "rm", volume], capture_output=True,
                       timeout=20, check=True)

TRANSPORT_PROBE = r'''
import json, os, time
from uuid import uuid4
from aegisctl.mounts import parse_mountinfo
from aegis_apps.indexing.processes import Coordination, ProcessReader, ReaderSource
from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete
root = "/srv/aegis/roots/synthetic"
records = parse_mountinfo(open("/proc/self/mountinfo", "rb").read(1048577))
assert records[root].effective_mode == "read_only", "SETUP: root mount missing"
assert not any(path.startswith(root + "/") for path in records), "SETUP: descendant mount"
assert os.stat(root + "/keep0").st_size == 8, "SETUP: sentinel missing"
assert os.geteuid() != 0
status = open("/proc/self/status").read()
assert "CapEff:\t0000000000000000" in status and "NoNewPrivs:\t1" in status
coordination = Coordination()
reader = ProcessReader(ReaderSource(root, records[root].mount_fingerprint, (), 500),
                       coordination, uuid4())
count, complete = 0, False
try:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        message = reader.receive()
        if isinstance(message, ReaderBatch):
            count += len(message.observations)
            reader.acknowledge(message.sequence)
        elif isinstance(message, ReaderComplete):
            complete = True
            break
        elif message is not None:
            raise AssertionError(message)
        time.sleep(.01)
    assert complete and count == 1101, (complete, count)
    while not reader.reaped() and time.monotonic() < deadline:
        time.sleep(.01)
    assert reader.reaped(), "reader not reaped"
    print(json.dumps({"observed": count, "complete": complete, "reaped": reader.reaped()}))
finally:
    reader.close_credits()
    reader.kill()
    reader.process.wait(timeout=5)
    reader.reaped()
    coordination.close()
'''


def test_actual_linux_reader_transport(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    for number in range(1101):
        (source / f"keep{number}").write_bytes(b"preserve")
    before = {item.name: (item.stat().st_size, item.stat().st_mtime_ns)
              for item in source.iterdir()}
    record_created_test_path(tree, source, recursive=True)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    image = _pinned_local_image("aegis-backend")
    workload = _new_workload("aegis-indexer", image, tmp_path_factory)
    launch = [
        "--init", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--user", f"{os.geteuid()}:{os.getegid()}", "--memory", "128m", "--pids-limit", "32",
        "--tmpfs", coordination_tmpfs((os.geteuid(), os.getegid())),
        "--mount", f"type=bind,src={source},dst=/srv/aegis/roots/synthetic,readonly",
        "--entrypoint", "python", image, "-c", TRANSPORT_PROBE,
    ]
    primary: BaseException | None = None
    try:
        _create_workload(workload, launch)
        result = _start_workload(workload, timeout=45)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {"observed": 1101, "complete": True, "reaped": True}
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _cleanup_workload(workload, primary)
        assert {item.name: (item.stat().st_size, item.stat().st_mtime_ns)
                for item in source.iterdir()} == before
        assert all(item.read_bytes() == b"preserve" for item in source.iterdir())


RUNTIME_PROBE = r'''
import hashlib, json, os, signal, sys, threading, time
from datetime import datetime, timezone
data = json.loads(sys.stdin.readline())
mode = data['mode']
with open('/tmp/database-password', 'x') as secret:
    os.fchmod(secret.fileno(), 0o600)
    secret.write(data['password'])
os.environ.update({
    'DJANGO_SETTINGS_MODULE': 'aegis.settings.test', 'AEGIS_ENV': 'test',
    'AEGIS_PROCESS_ROLE': 'indexer', 'AEGIS_RELEASE_ID': 'task7-runtime',
    'AEGIS_DB_HOST': data['host'], 'AEGIS_DB_PORT': str(data['port']),
    'AEGIS_DB_NAME': data['database'], 'AEGIS_DB_USER': 'aegis_indexer',
    'AEGIS_DB_PASSWORD_FILE': '/tmp/database-password',
    'AEGIS_SCAN_IDLE_TIMEOUT_SECONDS': str(data['timeout']),
})
from aegisctl.mounts import parse_mountinfo
records = parse_mountinfo(open('/proc/self/mountinfo', 'rb').read(1048577))
slots = []
for root in data['roots']:
    path = '/srv/aegis/roots/' + root['slot']
    record = records[path]
    assert record.effective_mode == 'read_only', 'SETUP: root is not read-only'
    assert not any(p.startswith(path + '/') for p in records), 'SETUP: descendant mount'
    assert os.stat(path + '/keep0').st_size == 8, 'SETUP: sentinel'
    info = os.stat(path)
    slots.append({'slotId': root['slot'], 'containerPath': path, 'mode': 'read_only',
                  'filesystemId': info.st_dev, 'rootInode': info.st_ino,
                  'expectedIdentity': f'local:{info.st_dev}:{info.st_ino}',
                  'mountFingerprint': record.mount_fingerprint})
raw = json.dumps({'version': 1, 'generatedAt': datetime.now(timezone.utc).isoformat()
                  .replace('+00:00', 'Z'), 'slots': slots}).encode()
digest = hashlib.sha256(raw).hexdigest()
with open('/tmp/manifest.json', 'xb') as manifest:
    os.fchmod(manifest.fileno(), 0o600)
    manifest.write(raw)
os.environ['AEGIS_MOUNT_MANIFEST'] = '/tmp/manifest.json'
os.environ['AEGIS_MOUNT_MANIFEST_SHA256'] = digest
print(json.dumps({'phase': 'ready', 'digest': digest}), flush=True)
assert sys.stdin.readline().strip() == 'go'
import django
django.setup()
from django.conf import settings
settings.AEGIS_ENVIRONMENT = 'development'
from django.db import connection
from aegis_apps.indexing import runner
from aegis_apps.indexing.models import DirectoryWork, RootIndexState
from aegis_apps.indexing.processes import Coordination, CoordinationError, ReaderLaunchFailure
from aegis_apps.operations.management.commands import run_role
from aegis_apps.operations.models import WorkerHeartbeat
children, errors, evidence, shared = [], [], {}, {}
if mode == 'database':
    from aegis_apps.indexing import checkpoints
    original_record = checkpoints.record_batch
    def blocked_record(lease, batch):
        if str(lease.root_id) == data['roots'][0]['id']:
            shared['blocked_pid'] = connection.connection.info.backend_pid
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_sleep(10)')
        return original_record(lease, batch)
    checkpoints.record_batch = blocked_record
original_reader = runner.ProcessReader
if mode == 'setup':
    from aegis_apps.indexing import processes
    class BrokenReceiver(threading.Thread):
        def start(self):
            raise RuntimeError('owned synthetic receiver exhaustion')
    processes.Thread = BrokenReceiver
def spawn(source, coordination, root):
    try:
        child = original_reader(source, coordination, root)
    except ReaderLaunchFailure as error:
        children.append(error.reader)
        raise
    children.append(child)
    if str(root) == data['roots'][0]['id']:
        shared['large_child'] = child
        if mode == 'pause':
            os.kill(child.process.pid, signal.SIGSTOP)
        interval = 17 if mode in ('healthy', 'sigterm', 'manifest', 'schema') else 1
        original_receive, next_delivery = child.receive, time.monotonic() + interval
        def paced_receive():
            nonlocal next_delivery
            if time.monotonic() < next_delivery:
                return None
            message = original_receive()
            if message is not None:
                next_delivery = time.monotonic() + interval
            return message
        child.receive = paced_receive
    return child
runner.ProcessReader = spawn
started = time.monotonic()
def observe():
    try:
        while time.monotonic() - started < 115:
            if run_role._shutdown_requested():
                return
            elapsed = time.monotonic() - started
            states = {str(row.root_id): row for row in RootIndexState.objects.all()}
            large, small = (states.get(root['id']) for root in data['roots'])
            if small is not None and small.status == 'ready':
                evidence.setdefault('small_ready_seconds', elapsed)
            if elapsed >= 2 and 'interrupted' not in evidence and mode != 'healthy':
                child = shared.get('large_child')
                if mode == 'sigterm' and child is not None:
                    evidence['interrupted'] = 'SIGTERM'
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                if mode == 'kill' and child is not None:
                    assert child.process.poll() is None, 'SETUP: reader already exited'
                    os.kill(child.process.pid, signal.SIGKILL)
                    evidence['interrupted'] = 'SIGKILL'
                if mode == 'pause' and child is not None:
                    status = open(f'/proc/{child.process.pid}/status').read()
                    assert 'State:\tT' in status, 'SETUP: reader not stopped'
                    try:
                        Coordination()
                    except CoordinationError:
                        evidence['replacement_excluded'] = True
                    else:
                        raise AssertionError('paused reader allowed a replacement coordinator')
                    evidence['interrupted'] = 'SIGSTOP'
                if mode == 'database' and 'blocked_pid' in shared:
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT pg_terminate_backend(%s)', [shared['blocked_pid']])
                        assert cursor.fetchone()[0], 'SETUP: backend interruption failed'
                    evidence['interrupted'] = 'database'
                if mode == 'manifest':
                    os.environ['AEGIS_MOUNT_MANIFEST_SHA256'] = '0' * 64
                    evidence['interrupted'] = 'manifest'
                if mode == 'schema':
                    evidence['interrupted'] = 'schema'
            if mode == 'healthy' and elapsed >= 65 and 'live_at_65' not in evidence:
                with connection.cursor() as cursor:
                    cursor.execute('SELECT clock_timestamp()')
                    now = cursor.fetchone()[0]
                work = DirectoryWork.objects.get(run_id=large.active_run_id, state='reading')
                heartbeat = WorkerHeartbeat.objects.get(worker_id=data['worker'], role='indexer')
                evidence['live_at_65'] = (work.lease_expires_at > now
                    and (now-heartbeat.last_seen_at).total_seconds() < 20
                    and heartbeat.current_job_id is None)
            if large is not None and small is not None and all(
                state.status == 'ready' for state in (large, small)
            ):
                evidence['counts'] = [large.observed_entries, small.observed_entries]
                run_role.request_shutdown()
                return
            if (mode != 'healthy' and large is not None and large.status == 'degraded'
                    and (mode != 'setup' or len(children) == 2)):
                run_role.request_shutdown()
                return
            time.sleep(.2)
        raise AssertionError('runtime did not complete before deadline')
    except BaseException as error:
        errors.append(type(error).__name__ + ': ' + str(error))
        errors.append(str(list(RootIndexState.objects.values(
            'root_id', 'status', 'observed_entries', 'active_run_id'))))
        errors.append(str(list(DirectoryWork.objects.values(
            'state', 'observed_count', 'error_code', 'attempt'))))
        errors.append(str([(child.process.pid, child.process.poll()) for child in children]))
        run_role.request_shutdown()
    finally:
        connection.close()
run_role._reset_shutdown()
observer = threading.Thread(target=observe)
observer.start()
try:
    with run_role._installed_signal_handlers():
        run_role.run_worker(role='indexer', once=False, worker_id=data['worker'])
except (ValueError, RuntimeError) as error:
    if mode not in ('manifest', 'schema'):
        raise
    evidence['stopped_error'] = type(error).__name__
finally:
    run_role.request_shutdown()
    observer.join(timeout=10)
assert not errors, errors
assert len(children) == 2 and all(child.reaped() for child in children), evidence
for child in children:
    if child._receiver is not None:
        if child._receiver.ident is not None:
            child._receiver.join(timeout=1)
        assert not child._receiver.is_alive(), 'abandoned result receiver leaked'
replacement = Coordination()
replacement.close()
evidence['replacement_after_reap'] = True
if mode == 'healthy':
    assert evidence['live_at_65'], evidence
    assert evidence['small_ready_seconds'] < 15, evidence
    assert evidence['counts'] == [1501, 3], evidence
else:
    work = DirectoryWork.objects.get(run__root_id=data['roots'][0]['id'])
    state = RootIndexState.objects.get(root_id=data['roots'][0]['id'])
    assert work.state == 'degraded' and state.completed_directories == 0, (work.state, evidence)
    evidence['observed_after_stop'] = state.observed_entries
    time.sleep(.2)
    state.refresh_from_db()
    assert state.observed_entries == evidence['observed_after_stop'], 'late checkpoint'
    evidence['duration'] = time.monotonic() - started
    if mode == 'pause':
        assert evidence['duration'] >= 30, evidence
heartbeat = WorkerHeartbeat.objects.get(worker_id=data['worker'], role='indexer')
assert heartbeat.status == 'stopping' and heartbeat.current_job_id is None
evidence['reaped'] = len(children)
print(json.dumps(evidence), flush=True)
'''


def _run_runtime_case(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, role_database: RoleDatabase,
    indexer_image: str, mode: str,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
    from aegis_apps.catalog.models import CatalogEntry
    from aegis_apps.indexing.models import IndexDeployment

    from tests.deployment.test_database_roles import _create_scan_fixture

    large, worker, _ = _create_scan_fixture()
    small, _, _ = _create_scan_fixture()
    roots = [large, small]
    anchor = CatalogEntry.objects.get(root=large, source_parent=None)
    prior = CatalogEntry.objects.create(
        root=large, source_parent=anchor, raw_name=b"prior", display_name="prior",
        name_key=b"prior", kind="file", source_state="present", source_parent_revision=0,
    )
    mounts: list[str] = []
    sources = []
    for root, count in zip(roots, (1501, 3), strict=True):
        source = tmp_path / root.slot_id
        source.mkdir()
        sources.append(source)
        for number in range(count):
            (source / f"keep{number}").write_bytes(b"preserve")
        record_created_test_path(tree, source, recursive=True)
        mounts += ["--mount", f"type=bind,src={source},"
                   f"dst=/srv/aegis/roots/{root.slot_id},readonly"]
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    workload = _new_workload("aegis-indexer-runtime", indexer_image, tmp_path_factory)
    host = "host.docker.internal" if sys.platform == "darwin" else "127.0.0.1"
    if sys.platform == "darwin":
        network = []
    elif selected_engine() == "podman":
        # Rootless Podman host networking rbinds the host /sys, duplicating /sys/fs/cgroup and
        # /sys/fs/selinux, which RUNTIME_PROBE's unchanged strict parser refuses (root O12).
        # pasta -T forwards only the database's loopback port into the container (O13).
        network = ["--network", f"pasta:-T,{role_database.port}"]
    else:
        network = ["--network", "host"]
    launch = [
        "--interactive", "--init", *network, "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "501:20", "--memory", "256m",
        "--pids-limit", "64", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m,mode=1777",
        "--tmpfs", coordination_tmpfs((501, 20)), *mounts,
        "--entrypoint", "python", indexer_image, "-c", RUNTIME_PROBE,
    ]
    schema_thread = None
    migration = None
    primary: BaseException | None = None
    try:
        _create_workload(workload, launch)
        # The attached start keeps the former `run --interactive` stdin/stdout handshake.
        with subprocess.Popen(controlled_container_argv(_start_command(workload, interactive=True)),
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            assert process.stdin is not None and process.stdout is not None
            process.stdin.write(json.dumps({
                "host": host, "port": role_database.port, "database": role_database.database_name,
                "password": role_database.passwords["aegis_indexer"], "worker": worker,
                "mode": mode, "timeout": 30 if mode == "pause" else 120,
                "roots": [{"id": str(root.pk), "slot": root.slot_id} for root in roots],
            }) + "\n")
            process.stdin.flush()
            ready = json.loads(process.stdout.readline())
            assert ready["phase"] == "ready"
            IndexDeployment.objects.filter(pk=1).update(
                manifest_identity=ready["digest"],
                idle_timeout_seconds=30 if mode == "pause" else 120,
            )
            if mode == "schema":
                with role_database.connect("aegis_migrator") as owner:
                    migration = owner.execute(
                        "SELECT id, app, name, applied FROM django_migrations "
                        "WHERE app='indexing' ORDER BY name DESC LIMIT 1",
                    ).fetchone()
                assert migration is not None

                def replace_schema() -> None:
                    time.sleep(4)
                    with role_database.connect("aegis_migrator") as owner:
                        owner.execute("DELETE FROM django_migrations WHERE id=%s", [migration[0]])

                schema_thread = threading.Thread(target=replace_schema)
                schema_thread.start()
            output, error = process.communicate("go\n", timeout=135)
            assert process.returncode == 0, output + error
            evidence = json.loads(output)
            assert evidence["reaped"] == 2
            if mode == "healthy":
                assert evidence["counts"] == [1501, 3] and evidence["live_at_65"]
            else:
                prior.refresh_from_db()
                assert prior.source_state == "present", "cancelled scan finalized missing entries"
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if schema_thread is not None:
            schema_thread.join(timeout=10)
            assert not schema_thread.is_alive()
        if migration is not None:
            with role_database.connect("aegis_migrator") as owner:
                owner.execute(
                    "INSERT INTO django_migrations(id,app,name,applied) VALUES(%s,%s,%s,%s)",
                    migration,
                )
        _cleanup_workload(workload, primary)
        assert all(item.read_bytes() == b"preserve"
                   for source in sources for item in source.iterdir())


@pytest.mark.django_db(transaction=True)
def test_minute_plus_scan_keeps_lease_heartbeat_and_second_root_progress(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, role_database: RoleDatabase,
    indexer_image: str,
) -> None:
    _run_runtime_case(tmp_path, tmp_path_factory, role_database, indexer_image, "healthy")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mode", [
    "sigterm", "kill", "pause", "database", "manifest", "schema", "setup",
])
def test_actual_runtime_interruption_never_finalizes_missing(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, role_database: RoleDatabase,
    indexer_image: str, mode: str,
) -> None:
    _run_runtime_case(tmp_path, tmp_path_factory, role_database, indexer_image, mode)


RESTART_PROBE = r'''
import ctypes, json, os, signal, subprocess, sys, time
from aegis_apps.indexing.processes import Coordination, CoordinationError
libc = ctypes.CDLL(None)
owned_reaper = sys.argv[1] == 'owned'
if owned_reaper:
    assert libc.prctl(36, 1, 0, 0, 0) == 0
else:
    assert os.getpid() != 1, 'container init is absent'
worker_code = """
import os, signal, time
from uuid import uuid4
from aegis_apps.indexing.processes import Coordination, ProcessReader, ReaderSource
from aegisctl.mounts import parse_mountinfo
root = '/srv/aegis/roots/synthetic'
records = parse_mountinfo(open('/proc/self/mountinfo', 'rb').read(1048577))
assert records[root].effective_mode == 'read_only'
assert os.stat(root + '/keep0').st_size == 8
coordination = Coordination()
reader = ProcessReader(ReaderSource(root, records[root].mount_fingerprint, (), 500),
                       coordination, uuid4())
deadline = time.monotonic() + 10
while reader._queue.qsize() < 2 and time.monotonic() < deadline:
    time.sleep(.01)
assert reader._queue.qsize() == 2, 'SETUP: child did not arm and send two batches'
print(reader.process.pid, flush=True)
signal.pause()
"""
worker = subprocess.Popen([sys.executable, '-c', worker_code], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, close_fds=True)
reader_pid = None
try:
    line = worker.stdout.readline().strip()
    assert line.isdecimal(), worker.stderr.read()
    reader_pid = int(line)
    try:
        Coordination()
    except CoordinationError:
        pass
    else:
        raise AssertionError('active coordinator allowed replacement')
    worker.kill()
    assert worker.wait(timeout=5) == -signal.SIGKILL
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if owned_reaper:
            reaped, status = os.waitpid(reader_pid, os.WNOHANG)
            gone = reaped == reader_pid
            if gone:
                assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        else:
            # No test subreaper: the orphan belongs to the container's actual init.
            gone = not os.path.exists('/proc/' + str(reader_pid))
        if gone:
            reader_pid = None
            break
        time.sleep(.01)
    else:
        raise AssertionError('parent-death child was not reaped')
    replacement = Coordination()
    replacement.close()
    print(json.dumps({'parent_death': 'SIGKILL', 'child_reaped': True, 'replacement': True,
                      'reaper': sys.argv[1]}))
finally:
    if worker.poll() is None:
        worker.kill()
        worker.wait(timeout=5)
    if reader_pid is not None:
        try:
            os.kill(reader_pid, signal.SIGKILL)
            os.waitpid(reader_pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
'''


@pytest.mark.parametrize("reaper", ["owned", "init"])
def test_parent_death_reaps_reader_before_replacement_admission(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, indexer_image: str, reaper: str,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    for number in range(1501):
        (source / f"keep{number}").write_bytes(b"preserve")
    record_created_test_path(tree, source, recursive=True)
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    workload = _new_workload("aegis-indexer-restart", indexer_image, tmp_path_factory)
    launch = [
        "--init", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "501:20",
        "--tmpfs", coordination_tmpfs((501, 20)),
        "--mount", f"type=bind,src={source},dst=/srv/aegis/roots/synthetic,readonly",
        "--entrypoint", "python", indexer_image, "-c", RESTART_PROBE, reaper,
    ]
    primary: BaseException | None = None
    try:
        _create_workload(workload, launch)
        result = _start_workload(workload, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {
            "parent_death": "SIGKILL", "child_reaped": True, "replacement": True,
            "reaper": reaper,
        }
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _cleanup_workload(workload, primary)
        assert all(item.read_bytes() == b"preserve" for item in source.iterdir())


# Metadata-only probe: no source bind, database or network. It emits everything it
# observes before Coordination is first invoked, then the Coordination outcome.
COORDINATION_PROBE = r'''
import json, os, stat, uuid
from aegisctl.mounts import MAX_MOUNTINFO_BYTES, parse_mountinfo
from aegis_apps.indexing.processes import Coordination, CoordinationError
target = '/srv/aegis/indexer-coordination'
def node(path):
    info = os.stat(path, follow_symlinks=False)
    return {'type': stat.S_IFMT(info.st_mode), 'uid': info.st_uid, 'gid': info.st_gid,
            'mode': stat.S_IMODE(info.st_mode), 'links': info.st_nlink, 'device': info.st_dev}
def usage(path):
    info = os.statvfs(path)
    return {'bytes': info.f_blocks * info.f_frsize, 'readOnly': bool(info.f_flag & os.ST_RDONLY)}
with open('/proc/self/status', 'rb') as source:
    status = source.read(65537).decode('ascii')
with open('/proc/self/mountinfo', 'rb') as source:
    mountinfo = source.read(MAX_MOUNTINFO_BYTES + 1)
try:
    parse_mountinfo(mountinfo)
    parse_error = None
except Exception as error:
    parse_error = f'{type(error).__name__}: {error}'
try:
    with open('/proc/self/attr/current', 'rb') as source:
        label = source.read(4097).decode('ascii').rstrip('\0\n')
except (OSError, UnicodeDecodeError) as error:
    label = type(error).__name__
fields = {}
for line in status.splitlines():
    name, _, value = line.partition(':')
    if name in ('Uid', 'Gid', 'CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb', 'NoNewPrivs'):
        fields[name] = value.strip()
print(json.dumps({
    'status': fields, 'label': label, 'mountinfo': mountinfo.decode('ascii').splitlines(),
    'parseError': parse_error, 'root': usage('/'), 'target': node(target) | usage(target),
    'entries': sorted(os.listdir(target)),
}), flush=True)
outcome = {}
try:
    coordination = Coordination()
except CoordinationError as error:
    outcome['coordination'] = type(error).__name__
else:
    try:
        outcome['coordination'] = 'created'
        outcome['deploymentLock'] = node(target + '/deployment.lock')
        try:
            Coordination().close()
        except CoordinationError as error:
            outcome['concurrent'] = type(error).__name__
        else:
            outcome['concurrent'] = 'admitted'
        root = uuid.uuid4()
        outcome['rootLockName'] = f'root-{root}.lock'
        scan = coordination.root(root)
        try:
            outcome['rootLock'] = node(target + '/' + outcome['rootLockName'])
            try:
                coordination.root(root).close()
            except CoordinationError as error:
                outcome['rootConcurrent'] = type(error).__name__
            else:
                outcome['rootConcurrent'] = 'admitted'
        finally:
            scan.close()
    finally:
        coordination.close()
    Coordination().close()
    outcome['replacement'] = True
outcome['entries'] = sorted(os.listdir(target))
print(json.dumps(outcome), flush=True)
'''
COORDINATION_CASES = frozenset({"dynamic", "fixed", "misowned", "absent"})


def validate_coordination_container(
    engine: str, info: Mapping[str, Any], user: str, limits: tuple[int, int] | None,
) -> None:
    """Prestart isolation of a metadata-only case, in the measured engine inspect forms."""
    host = info["HostConfig"]
    assert info["Config"]["User"] == user, "process user changed"
    assert host["ReadonlyRootfs"] is True and host.get("Privileged") is False
    assert host["NetworkMode"] == "none", "metadata case is not network-isolated"
    assert host["CapDrop"] == (list(PODMAN_CAP_DROP_ALL) if engine == "podman" else ["ALL"])
    assert not host.get("CapAdd"), "capabilities were added"
    assert host["SecurityOpt"] == (
        ["no-new-privileges"] if engine == "podman" else ["no-new-privileges:true"]
    )
    if engine == "podman":
        assert ":container_t:" in info["ProcessLabel"], "process is not SELinux-confined"
        assert ":container_file_t:" in info["MountLabel"], "mounts are not SELinux-labeled"
    assert all(mount.get("Type") == "tmpfs" for mount in info.get("Mounts") or []), (
        "metadata case has a source bind or volume"
    )
    if limits is not None:
        assert (host["Memory"], host["PidsLimit"]) == limits, "resource limits changed"


def validate_coordination_evidence(
    engine: str, case: str, user: tuple[int, int], evidence: Mapping[str, Any],
) -> None:
    """Everything the probe observed before its first Coordination invocation."""
    assert case in COORDINATION_CASES
    uid, gid = user
    assert evidence["status"] == {
        "Uid": "\t".join([str(uid)] * 4), "Gid": "\t".join([str(gid)] * 4),
        **dict.fromkeys(("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"), ZERO_CAPABILITIES),
        "NoNewPrivs": "1",
    }, "process identity, capabilities or no-new-privileges changed"
    if engine == "podman":
        label = evidence["label"]
        assert isinstance(label, str) and label.split(":")[2:3] == ["container_t"], (
            "process is not SELinux-confined"
        )
    assert evidence["parseError"] is None, "the unchanged strict parser refused mountinfo"
    lines = evidence["mountinfo"]
    records = parse_mountinfo(("\n".join(lines) + "\n").encode("ascii"))
    assert records["/"].effective_mode == "read_only", "container root is writable"
    assert evidence["root"]["readOnly"] is True, "container root is writable"
    if engine == "podman":
        # One admitted powercap mask; the unchanged parser refuses a duplicate mountpoint.
        assert POWERCAP in records, "powercap mask is absent"
        assert records[POWERCAP].effective_mode == "read_only", "powercap mask is writable"
    target = evidence["target"]
    assert target["type"] == stat.S_IFDIR and evidence["entries"] == [], (
        "coordination directory is not initially empty"
    )
    if case == "absent":
        assert COORDINATION_TARGET not in records, "absent case has a coordination mount"
        return
    assert COORDINATION_TARGET in records, "coordination tmpfs is not a separate mount"
    record = records[COORDINATION_TARGET]
    device, filesystem = record.filesystem_identity
    assert filesystem == "tmpfs" and record.effective_mode == "read_write"
    assert record.filesystem_root == PurePosixPath("/"), "coordination mount is a subtree bind"
    rows = [line.split(" ") for line in lines]
    assert [row[2] for row in rows].count(device) == 1, "coordination tmpfs backing is shared"
    (row,) = [row for row in rows if row[4] == COORDINATION_TARGET]
    separator = row.index("-")
    assert not any(tag.startswith(("shared:", "master:")) for tag in row[6:separator]), (
        "coordination tmpfs propagation is not private"
    )
    assert {"rw", "nosuid", "nodev"} <= set(row[5].split(",")), "declared flags are missing"
    assert target["readOnly"] is False, "coordination tmpfs is read-only"
    # Ownership is proved by in-container stat, never by raw superblock uid/gid fields.
    if case == "misowned":
        assert (target["uid"], target["gid"], target["mode"]) == (0, 0, 0o777), (
            "misowned coordination tmpfs is not exactly 0:0 mode 0777"
        )
        return
    assert (target["uid"], target["gid"], target["mode"]) == (uid, gid, 0o700), (
        "coordination tmpfs is not owned by the process user with mode 0700"
    )
    assert target["bytes"] == 1024 * 1024, "coordination tmpfs limit is not 1 MiB"
    assert "size=1024k" in row[separator + 3].split(","), "coordination tmpfs limit changed"


def validate_coordination_outcome(
    case: str, user: tuple[int, int], evidence: Mapping[str, Any], outcome: Mapping[str, Any],
) -> None:
    """Positives keep the existing lock contract; negatives reject before creating a lock."""
    assert case in COORDINATION_CASES
    if case in ("misowned", "absent"):
        assert outcome == {"coordination": "CoordinationError", "entries": []}, outcome
        return
    lock = {
        "type": stat.S_IFREG, "uid": user[0], "gid": user[1], "mode": 0o600, "links": 1,
        "device": evidence["target"]["device"],
    }
    name = outcome.get("rootLockName")
    assert isinstance(name, str) and _ROOT_LOCK.fullmatch(name), outcome
    assert outcome == {
        "coordination": "created", "deploymentLock": lock, "concurrent": "CoordinationError",
        "rootLockName": name, "rootLock": lock, "rootConcurrent": "CoordinationBusy",
        "replacement": True, "entries": sorted(["deployment.lock", name]),
    }, outcome


def _coordination_lines(output: str) -> tuple[dict[str, Any], dict[str, Any]]:
    lines = output.splitlines()
    assert len(lines) == 2, output
    evidence, outcome = json.loads(lines[0]), json.loads(lines[1])
    assert isinstance(evidence, dict) and isinstance(outcome, dict), output
    return evidence, outcome


def _run_coordination_case(
    tmp_path_factory: pytest.TempPathFactory, image: str, case: str, user: tuple[int, int],
    isolation: Sequence[str], limits: tuple[int, int] | None,
) -> None:
    """Recorded metadata-only case; an engine or setup failure can never pass a negative."""
    engine = selected_engine()
    workload = _new_workload("aegis-indexer-coordination", image, tmp_path_factory)
    primary: BaseException | None = None
    try:
        info = _create_workload(
            workload, [*isolation, "--entrypoint", "python", image, "-c", COORDINATION_PROBE],
        )
        validate_coordination_container(engine, info, f"{user[0]}:{user[1]}", limits)
        result = _start_workload(workload, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        evidence, outcome = _coordination_lines(result.stdout)
        validate_coordination_evidence(engine, case, user, evidence)
        validate_coordination_outcome(case, user, evidence, outcome)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        _cleanup_workload(workload, primary)


@pytest.mark.parametrize("user", ["dynamic", "fixed"])
def test_private_coordination_tmpfs_belongs_to_process_user(
    tmp_path_factory: pytest.TempPathFactory, indexer_image: str, user: str,
) -> None:
    """Positive metadata: fresh 1 MiB 0700 tmpfs owned by the process user, then locks."""
    if user == "dynamic":
        identity = (os.geteuid(), os.getegid())
        # test_actual_linux_reader_transport's identity and isolation, without its source.
        isolation = [
            "--init", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--user", f"{os.geteuid()}:{os.getegid()}", "--memory", "128m", "--pids-limit", "32",
            "--tmpfs", coordination_tmpfs(identity),
        ]
        limits: tuple[int, int] | None = (128 * 1024 * 1024, 32)
    else:
        identity = (501, 20)
        # The parent-death fixture's 501:20 identity and isolation, without its source.
        isolation = [
            "--init", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges:true", "--user", "501:20",
            "--tmpfs", coordination_tmpfs(identity),
        ]
        limits = None
    _run_coordination_case(tmp_path_factory, indexer_image, user, identity, isolation, limits)


@pytest.mark.parametrize("mount", ["absent", "misowned"])
def test_coordination_fails_closed_without_owned_mount(
    tmp_path_factory: pytest.TempPathFactory, indexer_image: str, mount: str,
) -> None:
    """Rejected only after proving the absent mount, or a fresh 0:0 0777 tmpfs, under 501:20."""
    mounts = [] if mount == "absent" else ["--tmpfs", coordination_tmpfs(None)]
    _run_coordination_case(tmp_path_factory, indexer_image, mount, (501, 20), [
        "--init", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "501:20",
        *mounts,
    ], None)
