"""Indexer scan throughput: a synthetic photo-library tree through the real indexer.

``uv run --locked python -m scripts.benchmarks.scan_throughput [--directories N] [--seconds S]
[--browse-workers W] [--scenario NAME] [--image TAG]``

A local measurement, not a gate; Linux only, with ``AEGIS_CONTAINER_ENGINE`` selecting the
engine as for the other harnesses. It builds the backend image from this checkout under a
run-unique tag and owner label (or uses ``--image``), starts the disposable PostgreSQL 18 of
``scripts/verify.py``, migrates it, creates the runtime roles with the actual-role test support
and generates a synthetic tree shaped like a photo library in a private temporary directory:
year and month folders, many small event folders (about five files each, some with nested
folders), one deep chain and a few big folders. Every name is synthetic and every file empty.

Each scenario scans the tree as a fresh root with the real indexer (``run_role --role
indexer``: runner, supervisor, database lanes and reader children) in one disposable,
read-only, capability-free container of that image, which reaches the database through a
loopback-only port:

- ``unloaded``: the scan alone;
- ``loaded``: the same scan while ``--browse-workers`` threads browse the root as the web
  role through the real authorization and catalog code: directory pages, descending into
  random subfolders, and entry details (the root lock every entries or media request takes),
  back to back or ``--browse-pause`` seconds apart.

A scenario ends when its run settles or after ``--seconds`` (0: until it settles). It reports
directories and entries per second; how many admissions, schedule calls and claims the indexer
made and how many returned nothing (with two readers and one root, every other admission looks
for another root and finds none; a skipped busy root shows as empty schedules or claims); slot
stops by code, retried and degraded directories; ``loaded`` adds the browse request rate,
latency percentiles and errors. Before the scenarios it reports what one reader child's start
and imports cost in
the image. The database setup (migrations, privileges, scan functions) comes from the
checkout that runs the script, so run it on a checkout of each revision to compare them.
Owned containers and images are removed by ID (images with ``--no-prune``);
``scripts/verify.py`` removes its database container.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Any

from aegisctl.container_engine import container_command, sanitized_environment, selected_engine
from aegisctl.container_launch import controlled_container_argv

REPOSITORY = Path(__file__).resolve().parents[2]
OWNER_LABEL = "aegis.verify.owner"
COORDINATION = "/srv/aegis/indexer-coordination"
RUNTIME_USER = "10001:10001"
SCENARIOS = ("unloaded", "loaded")

# Runs inside the indexer container: writes the manifest of its one read-only root, waits for
# the host to bind it, then runs the unchanged worker. The two wrappers only count calls.
PROBE = r'''
import hashlib, json, os, sys, threading
from datetime import datetime, timezone
data = json.loads(sys.stdin.readline())
with open('/tmp/database-password', 'x') as secret:
    os.fchmod(secret.fileno(), 0o600)
    secret.write(data['password'])
os.environ.update({
    'DJANGO_SETTINGS_MODULE': 'aegis.settings.test', 'AEGIS_ENV': 'test',
    'AEGIS_PROCESS_ROLE': 'indexer', 'AEGIS_RELEASE_ID': 'scan-benchmark',
    'AEGIS_DB_HOST': data['host'], 'AEGIS_DB_PORT': str(data['port']),
    'AEGIS_DB_NAME': data['database'], 'AEGIS_DB_USER': 'aegis_indexer',
    'AEGIS_DB_PASSWORD_FILE': '/tmp/database-password',
    'AEGIS_SCAN_READERS': str(data['readers']), 'AEGIS_SCAN_BATCH_RECORDS': '500',
})
from aegisctl.mounts import parse_mountinfo
path = '/srv/aegis/roots/' + data['slot']
record = parse_mountinfo(open('/proc/self/mountinfo', 'rb').read(1048577))[path]
assert record.effective_mode == 'read_only', 'the benchmark root is not read-only'
info = os.stat(path)
raw = json.dumps({'version': 1, 'generatedAt': datetime.now(timezone.utc).isoformat()
                  .replace('+00:00', 'Z'), 'slots': [{
    'slotId': data['slot'], 'containerPath': path, 'mode': 'read_only',
    'filesystemId': info.st_dev, 'rootInode': info.st_ino,
    'expectedIdentity': f'local:{info.st_dev}:{info.st_ino}',
    'mountFingerprint': record.mount_fingerprint}]}).encode()
with open('/tmp/manifest.json', 'xb') as manifest:
    os.fchmod(manifest.fileno(), 0o600)
    manifest.write(raw)
os.environ['AEGIS_MOUNT_MANIFEST'] = '/tmp/manifest.json'
os.environ['AEGIS_MOUNT_MANIFEST_SHA256'] = hashlib.sha256(raw).hexdigest()
print(json.dumps({'phase': 'ready', 'manifest': raw.decode()}), flush=True)
assert sys.stdin.readline().strip() == 'go'
import django
django.setup()
from django.conf import settings
settings.AEGIS_ENVIRONMENT = 'development'
from aegis_apps.indexing import runner, scheduling, supervisor
from aegis_apps.operations.management.commands import run_role
counts = {'admissions': 0, 'empty_admissions': 0, 'schedules': 0, 'empty_schedules': 0,
          'claims': 0, 'empty_claims': 0, 'stops': {}}
claim_next, stop_slot = runner.ScanRuntime.claim_next, supervisor.ScanSupervisor._stop_slot
schedule_root_scan, claim_directory = scheduling.schedule_root_scan, runner.claim_directory
def counted(name, function):
    def call(*args):
        result = function(*args)
        counts[name + 's'] += 1
        counts['empty_' + name + 's'] += result is None
        return result
    return call
counted_claim = counted('admission', claim_next)
scheduling.schedule_root_scan = counted('schedule', schedule_root_scan)
runner.claim_directory = counted('claim', claim_directory)
def counted_stop(self, slot, code):
    if slot.state not in ('stopping', 'unreaped', 'settled') and not self.stopping:
        counts['stops'][code] = counts['stops'].get(code, 0) + 1
    return stop_slot(self, slot, code)
runner.ScanRuntime.claim_next = counted_claim
supervisor.ScanSupervisor._stop_slot = counted_stop
def stop_on_request():
    sys.stdin.readline()
    run_role.request_shutdown()
run_role._reset_shutdown()
threading.Thread(target=stop_on_request, daemon=True).start()
with run_role._installed_signal_handlers():
    run_role.run_worker(role='indexer', once=False, worker_id=data['worker'])
print(json.dumps({'phase': 'stopped', **counts}), flush=True)
'''

# Runs inside the image as the indexer would: what one reader child's interpreter start and
# imports cost (every directory pays it), with the image's read-only, no-write bytecode rules.
STARTUP = r'''
import json, os, re, statistics, subprocess, sys, time
env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': '/app/backend'}
samples = []
for _ in range(15):
    started = time.perf_counter()
    subprocess.run([sys.executable, '-m', 'aegis_apps.indexing.reader', '--help'], env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    samples.append(time.perf_counter() - started)
imports = subprocess.run([sys.executable, '-X', 'importtime', '-c',
                          'import aegis_apps.indexing.reader'], env=env, capture_output=True,
                         text=True, check=False).stderr
total = re.search(r'\|\s+(\d+) \| aegis_apps\.indexing\.reader$', imports, re.M)
print(json.dumps({'reader_spawn_ms': round(statistics.median(samples) * 1000, 1),
                  'reader_import_ms': round(int(total[1]) / 1000, 1) if total else None}))
'''

_EXTENSIONS = (".JPG", ".JPG", ".JPG", ".HEIC", ".PNG", ".MOV", ".MP4", ".CR2", ".XMP")


def _files(directory: Path, count: int, rng: random.Random, prefix: str = "IMG_") -> int:
    start = rng.randrange(1, 9000)
    for number in range(start, start + count):
        name = f"{prefix}{number:05d}{rng.choice(_EXTENSIONS)}"
        descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        os.close(descriptor)
    return count


def _small_count(rng: random.Random) -> int:
    """About five files: geometric, capped so only the big folders are big."""
    count = 0
    while count < 30 and rng.random() > 1 / 6:
        count += 1
    return count


def generate_tree(base: Path, directories: int, seed: int = 2026) -> tuple[int, int]:
    """A deterministic synthetic photo library; returns (directories, entries).

    Directories include the root itself, as the scan counts it. Entries are every file and
    folder below the root, which is what a completed scan observes.
    """
    rng = random.Random(seed)
    base.mkdir()
    made, files = 1, 0

    def folder(path: Path) -> Path:
        nonlocal made
        path.mkdir()
        made += 1
        return path

    years = [folder(base / str(year)) for year in range(2006, 2026)]
    months = [folder(year / f"{year.name}-{month:02d}") for year in years for month in range(1, 13)]
    for month in months:
        files += _files(month, rng.randrange(0, 3), rng)
    big = (
        (base, "Camera Uploads", 2400),
        (months[13 * 12 + 7], "WhatsApp Images", 1600),
        (months[17 * 12], "Screenshots", 1200),
    )
    for parent, name, count in big:
        files += _files(folder(parent / name), count, rng, prefix="IMG-")
    deep = folder(base / "Archive")
    for depth in range(24):
        deep = folder(deep / f"Imported {depth:02d}")
        files += _files(deep, 2, rng, prefix="SCAN_")
    if made > directories:
        raise ValueError(f"the fixed layout needs at least {made} directories")
    events = 0
    while made < directories:
        month = rng.choice(months)
        events += 1
        event = folder(month / f"{month.name}-{rng.randrange(1, 29):02d} Event {events:05d}")
        files += _files(event, _small_count(rng), rng)
        if rng.random() < 0.15:
            for parts in (("RAW",), ("Edited",), ("Edited", "Web")):
                if made >= directories:
                    break
                files += _files(folder(event.joinpath(*parts)), _small_count(rng), rng,
                                prefix="DSC")
    return made, files + made - 1


def _run(command: Sequence[str], *, timeout: int, check: bool = True,
         cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    environment = sanitized_environment(os.environ)
    argv = controlled_container_argv(
        list(command), environment=environment, canonical_base=REPOSITORY / "compose.yaml",
        working_directory=cwd,
    )
    completed = subprocess.run(argv, env=environment, cwd=cwd, capture_output=True, text=True,
                               timeout=timeout, check=False)
    if check and completed.returncode != 0:
        raise SystemExit(f"container command failed: {completed.stderr[-2000:]}")
    return completed


def _owned_id(kind: str, name: str) -> str | None:
    """The ID behind ``name`` if this run created it (its owner label is the name)."""
    inspected = _run([*container_command(), kind, "inspect", name], timeout=60, check=False)
    if inspected.returncode != 0:
        return None
    info = json.loads(inspected.stdout)[0]
    labels = (info.get("Config") or {}).get("Labels") or {}
    identity = info.get("Id")
    if labels.get(OWNER_LABEL) != name or not isinstance(identity, str) or not identity:
        return None
    return identity


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] * 1000, 1)


class Browser(threading.Thread):
    """One web-role browsing session over the scanned root, on its own connection."""

    def __init__(self, user: Any, root_id: uuid.UUID, password: str, stop: threading.Event,
                 seed: int, pause: float) -> None:
        super().__init__(daemon=True)
        self.user, self.root_id, self.password, self.stop = user, root_id, password, stop
        self.rng, self.pause = random.Random(seed), pause
        self.latencies: list[float] = []
        self.errors: dict[str, int] = {}

    def run(self) -> None:
        from aegis_apps.catalog.authorization import browse_context
        from aegis_apps.catalog.filters import FileFilter
        from aegis_apps.catalog.queries import directory_page, entry_details
        from django.db import connection

        connection.settings_dict = {**connection.settings_dict, "USER": "aegis_web",
                                    "PASSWORD": self.password}
        namespace = "b" * 40
        parent: str | None = None
        try:
            while not self.stop.is_set():
                started = time.monotonic()
                try:
                    with browse_context(self.user, self.root_id, namespace) as context:
                        page = directory_page(
                            context, parent_id=None if parent is None else uuid.UUID(parent),
                            filters=FileFilter(), sort="name", order="asc", limit=100,
                            cursor=None,
                        )
                    self.latencies.append(time.monotonic() - started)
                    entries = page["entries"]
                    folders = [entry["id"] for entry in entries if entry["kind"] == "directory"]
                    if entries and self.rng.random() < 0.3:
                        started = time.monotonic()
                        entry_details(self.user, uuid.UUID(self.rng.choice(entries)["id"]),
                                      namespace)
                        self.latencies.append(time.monotonic() - started)
                    parent = (self.rng.choice(folders) if folders and self.rng.random() < 0.7
                              else None)
                except Exception as error:  # every failure is a measured outcome
                    name = type(error).__name__
                    self.errors[name] = self.errors.get(name, 0) + 1
                    parent = None
                    connection.close()
                self.stop.wait(self.pause)
        finally:
            connection.close()


class Indexer:
    """The indexer container's stdin/stdout handshake, with stderr kept for diagnosis."""

    def __init__(self, argv: list[str], log: IO[str]) -> None:
        environment = sanitized_environment(os.environ)
        self.process = subprocess.Popen(argv, env=environment, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=log, text=True)
        self.lines: queue.Queue[str | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def send(self, line: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def phase(self, name: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise SystemExit(f"the indexer did not reach {name!r}") from None
            if line is None:
                raise SystemExit(f"the indexer exited before {name!r}")
            if line.startswith("{"):
                message: dict[str, Any] = json.loads(line)
                if message.get("phase") == name:
                    return message


def _scenario(name: str, image: str, tree: Path, workdir: Path, database: Any, user: Any,
              arguments: argparse.Namespace, directories: int) -> dict[str, Any]:
    from aegis_apps.catalog.models import CatalogEntry
    from aegis_apps.indexing.models import DirectoryWork, IndexDeployment, RootIndexState
    from aegis_apps.operations.models import WorkerHeartbeat
    from aegis_apps.roots.models import Root, RootGrant
    from django.db import connection
    from django.db.models import Count, Q
    from django.db.models.functions import Now

    slot = f"bench-{uuid.uuid4().hex[:12]}"
    root = Root.objects.create(slot_id=slot, display_name="Synthetic scan benchmark",
                               mode=Root.Mode.READ_ONLY, active=True)
    CatalogEntry.objects.create(root=root, raw_name=b"", display_name="", name_key=b"",
                                kind="directory")
    RootGrant.objects.create(root=root, user=user, permissions=1)
    IndexDeployment.objects.update_or_create(pk=1, defaults={
        "manifest_identity": "a" * 64, "slot_ids": [slot], "interval_seconds": 3600,
        "idle_timeout_seconds": 120, "batch_records": 500, "readers": arguments.readers,
    })
    worker = str(uuid.uuid4())
    WorkerHeartbeat.objects.create(
        role="indexer", worker_id=worker, release_id="scan-benchmark",
        schema_identity="benchmark", manifest_identity="a" * 64, last_seen_at=Now(),
        status="idle", metrics={},
    )
    container = f"aegis-scan-bench-{uuid.uuid4().hex[:12]}"
    if selected_engine() == "podman":
        network = ["--network", f"pasta:-T,{database.port}"]
        coordination = f"{COORDINATION}:rw,nosuid,nodev,size=1m,U,notmpcopyup,mode=0700"
    else:
        network = ["--network", "host"]
        coordination = f"{COORDINATION}:rw,nosuid,nodev,size=1m,uid=10001,gid=10001,mode=0700"
    command = [
        *container_command(), "run", "--rm", "--interactive", "--name", container,
        "--label", f"{OWNER_LABEL}={container}", "--init", *network, "--read-only",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--user", RUNTIME_USER,
        "--memory", "512m", "--pids-limit", "128",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m,mode=1777", "--tmpfs", coordination,
        "--mount", f"type=bind,src={tree},dst=/srv/aegis/roots/{slot},readonly",
        "--entrypoint", "python", image, "-c", PROBE,
    ]
    argv = controlled_container_argv(command, environment=sanitized_environment(os.environ))
    stop = threading.Event()
    browsers: list[Browser] = []
    samples: list[tuple[float, int, int]] = []
    log_path = workdir / f"{name}.log"
    with log_path.open("w", encoding="utf-8") as log:
        indexer = Indexer(argv, log)
        try:
            indexer.send(json.dumps({
                "host": "127.0.0.1", "port": database.port, "database": database.database_name,
                "password": database.passwords["aegis_indexer"], "worker": worker,
                "slot": slot, "readers": arguments.readers,
            }))
            manifest = indexer.phase("ready", 60)["manifest"].encode()
            manifest_path = workdir / f"{name}-manifest.json"
            with manifest_path.open("xb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(manifest)
            digest = hashlib.sha256(manifest).hexdigest()
            os.environ["AEGIS_MOUNT_MANIFEST"] = str(manifest_path)
            os.environ["AEGIS_MOUNT_MANIFEST_SHA256"] = digest
            IndexDeployment.objects.filter(pk=1).update(manifest_identity=digest)
            indexer.send("go")
            started = time.monotonic()
            if name == "loaded":
                browsers = [Browser(user, root.pk, database.passwords["aegis_web"], stop,
                                    seed, arguments.browse_pause)
                            for seed in range(arguments.browse_workers)]
                for browser in browsers:
                    browser.start()
            limit = arguments.seconds or 7200
            settled = False
            while not settled and time.monotonic() - started < limit:
                if indexer.process.poll() is not None:
                    raise SystemExit("the indexer exited early")
                time.sleep(0.5)
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT EXTRACT(EPOCH FROM COALESCE(run.settled_at, clock_timestamp())"
                        " - run.started_at), "
                        "state.completed_directories + state.degraded_directories, "
                        "state.observed_entries, run.state "
                        "FROM indexing_rootindexstate AS state "
                        "JOIN indexing_scanrun AS run ON run.root_id = state.root_id "
                        "WHERE state.root_id = %s ORDER BY run.started_at LIMIT 1",
                        [root.pk],
                    )
                    row = cursor.fetchone()
                if row is not None:
                    samples.append((float(row[0]), int(row[1]), int(row[2])))
                    settled = row[3] in ("complete", "degraded")
            stop.set()
            window = time.monotonic() - started
            indexer.send("stop")
            counts = indexer.phase("stopped", 120)
            indexer.process.wait(timeout=60)
        except SystemExit as failure:
            log.flush()
            raise SystemExit(f"{failure}; its output ends with:\n"
                             f"{log_path.read_text(errors='replace')[-4000:]}") from None
        finally:
            stop.set()
            for browser in browsers:
                browser.join(timeout=30)
            if indexer.process.poll() is None:
                indexer.process.kill()
                indexer.process.wait(timeout=60)
            identity = _owned_id("container", container)
            if identity is not None:
                _run([*container_command(), "rm", "--force", identity], timeout=60)
    # No state at all means no admission ever scheduled the root.
    state = RootIndexState.objects.filter(root=root).first()
    work = DirectoryWork.objects.filter(run__root=root).aggregate(
        retried=Count("pk", filter=Q(attempt__gt=1)),
        degraded=Count("pk", filter=Q(state="degraded")),
    )
    # From the run's start (its settling, if it settled); the whole window if none started.
    elapsed, done, observed = samples[-1] if samples else (window, 0, 0)
    latencies = [value for browser in browsers for value in browser.latencies]
    errors: dict[str, int] = {}
    for browser in browsers:
        for key, value in browser.errors.items():
            errors[key] = errors.get(key, 0) + value
    report: dict[str, Any] = {
        "scenario": name, "readers": arguments.readers, "directories": directories,
        "run_started": state is not None,
        "settled": state is not None and state.active_run_id is None,
        "seconds": round(elapsed, 1), "directories_done": done, "entries_observed": observed,
        "directories_per_second": round(done / elapsed, 2),
        "entries_per_second": round(observed / elapsed, 1),
        **{key: counts[key] for key in (
            "admissions", "empty_admissions", "schedules", "empty_schedules", "claims",
            "empty_claims",
        )},
        "slot_stops": counts["stops"], "retried_directories": work["retried"],
        "degraded_directories": work["degraded"],
    }
    if browsers:
        report |= {
            "browse_workers": len(browsers), "browse_pause": arguments.browse_pause,
            "browse_requests": len(latencies),
            "browse_per_second": round(len(latencies) / window, 1),
            "browse_p50_ms": _percentile(latencies, 0.5),
            "browse_p95_ms": _percentile(latencies, 0.95),
            "browse_max_ms": _percentile(latencies, 1.0),
            "browse_errors": errors,
        }
    Root.objects.filter(pk=root.pk).update(active=False)
    return report


def _reader_startup(image: str) -> dict[str, Any]:
    container = f"aegis-scan-bench-{uuid.uuid4().hex[:12]}"
    try:
        completed = _run([
            *container_command(), "run", "--rm", "--name", container,
            "--label", f"{OWNER_LABEL}={container}", "--network", "none", "--read-only",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--user", RUNTIME_USER, "--entrypoint", "python", image, "-c", STARTUP,
        ], timeout=300)
    finally:
        identity = _owned_id("container", container)
        if identity is not None:
            _run([*container_command(), "rm", "--force", identity], timeout=60)
    report: dict[str, Any] = json.loads(completed.stdout.strip().splitlines()[-1])
    return report


def _measure(arguments: argparse.Namespace, image: str, workdir: Path, tree: Path,
             directories: int) -> list[dict[str, Any]]:
    from scripts.verify import test_database

    with test_database() as environment:
        os.environ.update(environment)
        os.environ["DJANGO_SETTINGS_MODULE"] = "aegis.settings.test"
        import django

        django.setup()
        from aegis_apps.identity.models import User
        from django.test.utils import setup_databases, teardown_databases

        from tests.support.database_roles import managed_role_database

        old = setup_databases(verbosity=0, interactive=False, aliases={"default"},
                              serialized_aliases=set())
        try:
            with managed_role_database() as database:
                user = User.objects.create_user(username=f"bench-{uuid.uuid4().hex[:12]}")
                return [
                    _scenario(name, image, tree, workdir, database, user, arguments,
                              directories)
                    for name in (arguments.scenario or SCENARIOS)
                ]
        finally:
            teardown_databases(old, verbosity=0)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.benchmarks.scan_throughput")
    parser.add_argument("--directories", type=int, default=6000)
    parser.add_argument("--seconds", type=int, default=300,
                        help="the cap per scenario; 0 scans until the run settles")
    parser.add_argument("--browse-workers", type=int, default=4)
    parser.add_argument("--browse-pause", type=float, default=0.0,
                        help="seconds each browse worker waits between requests")
    parser.add_argument("--readers", type=int, default=2)
    parser.add_argument("--scenario", choices=SCENARIOS, action="append")
    parser.add_argument("--image", help="a backend image to use instead of building one")
    # Internal: one scenario on an existing tree, in a process of its own (see below).
    parser.add_argument("--tree", type=Path, help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if not 400 <= arguments.directories <= 50_000:
        parser.error("--directories must be from 400 to 50000")
    if not 0 <= arguments.seconds <= 7200 or not 1 <= arguments.browse_workers <= 16:
        parser.error("--seconds must be from 0 to 7200 and --browse-workers from 1 to 16")
    if not 1 <= arguments.readers <= 4 or not 0 <= arguments.browse_pause <= 60:
        parser.error("--readers must be from 1 to 4 and --browse-pause from 0 to 60")
    if not sys.platform.startswith("linux"):
        parser.error("the scan benchmark needs a Linux host")
    if arguments.tree is not None:
        if arguments.image is None or len(arguments.scenario or ()) != 1:
            parser.error("--tree takes --image and exactly one --scenario")
        (report,) = _measure(arguments, arguments.image, arguments.tree.parent, arguments.tree,
                             arguments.directories)
        print(json.dumps(report, sort_keys=True))
        return 0
    workdir = Path(tempfile.mkdtemp(prefix="aegis-scan-bench-"))
    image = arguments.image
    built = None
    reports = []
    try:
        tree = workdir / "library"
        directories, entries = generate_tree(tree, arguments.directories)
        if selected_engine() == "podman":
            # As the deployment tests label their synthetic binds: SELinux confines Podman.
            subprocess.run(["chcon", "-R", "--no-dereference", "--type", "container_file_t",
                            "--", str(tree)], check=True, timeout=300)
        print(json.dumps({"tree_directories": directories, "tree_entries": entries}),
              flush=True)
        if image is None:
            built = image = f"aegis-scan-bench-{uuid.uuid4().hex[:12]}"
            _run([*container_command(), "build", "--label", f"{OWNER_LABEL}={built}", "--tag",
                  built, "--file", "docker/backend.Dockerfile", "."], timeout=3600,
                 cwd=REPOSITORY)
        print(json.dumps(_reader_startup(image)), flush=True)
        # One process and one disposable database per scenario: a database holds one scan
        # of the tree in its memory-only storage, and Django binds a process to one server.
        for name in arguments.scenario or SCENARIOS:
            completed = subprocess.run([
                sys.executable, "-m", "scripts.benchmarks.scan_throughput", "--tree", str(tree),
                "--image", image, "--scenario", name, "--directories", str(directories),
                "--seconds", str(arguments.seconds), "--readers", str(arguments.readers),
                "--browse-workers", str(arguments.browse_workers),
                "--browse-pause", str(arguments.browse_pause),
            ], cwd=REPOSITORY, stdout=subprocess.PIPE, text=True, check=False)
            if completed.returncode != 0:
                raise SystemExit(f"the {name} scenario failed")
            reports.append(json.loads(completed.stdout.strip().splitlines()[-1]))
            print(json.dumps(reports[-1], sort_keys=True), flush=True)
    finally:
        identity = None if built is None else _owned_id("image", built)
        if identity is not None:
            # By its ID, so a re-pointed tag can never remove another image.
            _run([*container_command(), "image", "rm", "--no-prune", identity], timeout=120)
        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
