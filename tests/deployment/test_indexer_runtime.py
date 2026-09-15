"""Task 7 Linux process evidence using only owned synthetic sources/resources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.database_roles import RoleDatabase

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def indexer_image() -> Iterator[str]:
    name = f"aegis-task7-{uuid.uuid4().hex}"
    try:
        built = subprocess.run(
            ["docker", "build", "--tag", name, "--label", f"aegis.indexer.owner={name}",
             "--build-arg", "AEGIS_UID=501", "--build-arg", "AEGIS_GID=20",
             "--file", "docker/backend.Dockerfile", "."], cwd=REPOSITORY,
            capture_output=True, text=True, timeout=300, check=False,
        )
        assert built.returncode == 0, built.stderr[-5000:]
        yield name
    finally:
        inspected = subprocess.run(["docker", "image", "inspect", name], capture_output=True,
                                   text=True, timeout=20, check=False)
        if inspected.returncode == 0:
            info = json.loads(inspected.stdout)[0]
            assert info["Config"]["Labels"]["aegis.indexer.owner"] == name
            subprocess.run(["docker", "image", "rm", name], capture_output=True,
                           timeout=30, check=True)


def test_new_coordination_volume_supports_nondefault_uid_gid(indexer_image: str) -> None:
    volume = f"aegis-task7-coordination-{uuid.uuid4().hex}"
    subprocess.run(["docker", "volume", "create", "--label", f"aegis.indexer.owner={volume}",
                    volume], capture_output=True, timeout=20, check=True)
    try:
        probe = (
            "import os; from aegis_apps.indexing.processes import Coordination; "
            "s=os.stat('/srv/aegis/indexer-coordination'); "
            "assert (s.st_uid,s.st_gid)==(501,20); c=Coordination(); c.close(); print('owned')"
        )
        result = subprocess.run(
            ["docker", "run", "--rm", "--init", "--network", "none", "--read-only",
             "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "501:20",
             "--mount", f"type=volume,src={volume},dst=/srv/aegis/indexer-coordination",
             "--entrypoint", "python", indexer_image, "-c", probe],
            capture_output=True, text=True, timeout=30, check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "owned"
    finally:
        info = json.loads(subprocess.run(["docker", "volume", "inspect", volume],
                          capture_output=True, text=True, timeout=20, check=True).stdout)[0]
        assert info["Labels"]["aegis.indexer.owner"] == volume
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True,
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


def test_actual_linux_reader_transport(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for number in range(1101):
        (source / f"keep{number}").write_bytes(b"preserve")
    before = {item.name: (item.stat().st_size, item.stat().st_mtime_ns)
              for item in source.iterdir()}
    name = f"aegis-indexer-{uuid.uuid4().hex}"
    command = [
        "docker", "run", "--init", "--name", name,
        "--label", f"aegis.indexer.owner={name}", "--network", "none", "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--user", f"{os.geteuid()}:{os.getegid()}", "--memory", "128m", "--pids-limit", "32",
        "--tmpfs", f"/srv/aegis/indexer-coordination:rw,nosuid,nodev,size=1m,"
        f"uid={os.geteuid()},gid={os.getegid()},mode=0700",
        "--mount", f"type=bind,src={source},dst=/srv/aegis/roots/synthetic,readonly",
        "--mount", f"type=bind,src={REPOSITORY / 'backend/aegis_apps'},"
        "dst=/app/backend/aegis_apps,readonly",
        "--mount", f"type=bind,src={REPOSITORY / 'backend/aegisctl'},"
        "dst=/app/backend/aegisctl,readonly",
        "--entrypoint", "python", "aegis-backend", "-c", TRANSPORT_PROBE,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {"observed": 1101, "complete": True, "reaped": True}
    finally:
        inspected = subprocess.run(["docker", "inspect", name], capture_output=True, text=True,
                                   timeout=20, check=False)
        if inspected.returncode == 0:
            owned = json.loads(inspected.stdout)[0]
            assert owned["Config"]["Labels"]["aegis.indexer.owner"] == name
            subprocess.run(["docker", "rm", "--force", owned["Id"]], capture_output=True,
                           timeout=20, check=True)
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
from aegis_apps.indexing.processes import Coordination, CoordinationError
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
def spawn(source, coordination, root):
    child = original_reader(source, coordination, root)
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
            if mode != 'healthy' and large is not None and large.status == 'degraded':
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
    tmp_path: Path, role_database: RoleDatabase, indexer_image: str, mode: str,
) -> None:
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
        mounts += ["--mount", f"type=bind,src={source},"
                   f"dst=/srv/aegis/roots/{root.slot_id},readonly"]
    name = f"aegis-indexer-runtime-{uuid.uuid4().hex}"
    host = "host.docker.internal" if sys.platform == "darwin" else "127.0.0.1"
    network = [] if sys.platform == "darwin" else ["--network", "host"]
    command = [
        "docker", "run", "--interactive", "--init", "--name", name,
        "--label", f"aegis.indexer.owner={name}", *network, "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "501:20", "--memory", "256m",
        "--pids-limit", "64", "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m,mode=1777",
        "--tmpfs", "/srv/aegis/indexer-coordination:rw,nosuid,nodev,size=1m,"
        "uid=501,gid=20,mode=0700", *mounts,
        "--entrypoint", "python", indexer_image, "-c", RUNTIME_PROBE,
    ]
    schema_thread = None
    migration = None
    try:
        with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
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
        inspected = subprocess.run(["docker", "inspect", name], capture_output=True,
                                   text=True, timeout=20, check=False)
        if inspected.returncode == 0:
            owned = json.loads(inspected.stdout)[0]
            assert owned["Config"]["Labels"]["aegis.indexer.owner"] == name
            subprocess.run(["docker", "rm", "--force", owned["Id"]], capture_output=True,
                           timeout=20, check=True)
        assert all(item.read_bytes() == b"preserve"
                   for source in sources for item in source.iterdir())


@pytest.mark.django_db(transaction=True)
def test_minute_plus_scan_keeps_lease_heartbeat_and_second_root_progress(
    tmp_path: Path, role_database: RoleDatabase, indexer_image: str,
) -> None:
    _run_runtime_case(tmp_path, role_database, indexer_image, "healthy")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mode", ["sigterm", "kill", "pause", "database", "manifest", "schema"])
def test_actual_runtime_interruption_never_finalizes_missing(
    tmp_path: Path, role_database: RoleDatabase, indexer_image: str, mode: str,
) -> None:
    _run_runtime_case(tmp_path, role_database, indexer_image, mode)


RESTART_PROBE = r'''
import ctypes, json, os, signal, subprocess, sys, time
from aegis_apps.indexing.processes import Coordination, CoordinationError
libc = ctypes.CDLL(None)
owned_reaper = sys.argv[1] == 'owned'
if owned_reaper:
    assert libc.prctl(36, 1, 0, 0, 0) == 0
else:
    assert 'docker-init' in open('/proc/1/comm').read()
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
    tmp_path: Path, indexer_image: str, reaper: str,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    for number in range(1501):
        (source / f"keep{number}").write_bytes(b"preserve")
    name = f"aegis-indexer-restart-{uuid.uuid4().hex}"
    command = [
        "docker", "run", "--init", "--name", name, "--label", f"aegis.indexer.owner={name}",
        "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true", "--user", "501:20",
        "--tmpfs", "/srv/aegis/indexer-coordination:rw,nosuid,nodev,size=1m,"
        "uid=501,gid=20,mode=0700",
        "--mount", f"type=bind,src={source},dst=/srv/aegis/roots/synthetic,readonly",
        "--entrypoint", "python", indexer_image, "-c", RESTART_PROBE, reaper,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=40, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout) == {
            "parent_death": "SIGKILL", "child_reaped": True, "replacement": True,
            "reaper": reaper,
        }
    finally:
        inspected = subprocess.run(["docker", "inspect", name], capture_output=True,
                                   text=True, timeout=20, check=False)
        if inspected.returncode == 0:
            owned = json.loads(inspected.stdout)[0]
            assert owned["Config"]["Labels"]["aegis.indexer.owner"] == name
            subprocess.run(["docker", "rm", "--force", owned["Id"]], capture_output=True,
                           timeout=20, check=True)
        assert all(item.read_bytes() == b"preserve" for item in source.iterdir())


@pytest.mark.parametrize("mount", ["absent", "misowned"])
def test_coordination_fails_closed_without_owned_mount(indexer_image: str, mount: str) -> None:
    probe = (
        "from aegis_apps.indexing.processes import Coordination, CoordinationError\n"
        "try:\n Coordination()\n"
        "except CoordinationError:\n print('rejected')\n"
        "else:\n raise AssertionError('unowned or absent mount accepted')\n"
    )
    mounts = ([] if mount == "absent" else ["--tmpfs",
              "/srv/aegis/indexer-coordination:rw,nosuid,nodev,uid=0,gid=0,mode=0777"])
    result = subprocess.run(
        ["docker", "run", "--rm", "--init", "--network", "none", "--read-only",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", "501:20",
         *mounts, "--entrypoint", "python", indexer_image, "-c", probe],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "rejected"
