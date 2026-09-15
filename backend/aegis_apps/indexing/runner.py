"""Attested scanner admission and separately connected database execution."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from threading import BoundedSemaphore, Event, Lock
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import connection

from aegis_apps.common.database_privileges import require_runtime_database_login

from .config import ScanPolicy
from .database import ScanLease, claim_directory, renew_directory
from .processes import Coordination, ProcessReader, ReaderSource
from .protocol import ReaderBatch, ReaderComplete
from .reader import MAX_COMPONENT_BYTES, MAX_COMPONENTS
from .supervisor import ReaderHandle, ScanSupervisor
from .supervisor import UnsupportedTraversal as UnsupportedTraversal

if TYPE_CHECKING:
    from aegis_apps.operations.management.commands.run_role import WorkerIdentity


class DatabaseLane:
    """A fixed number of sessions, with no unbounded executor submission queue."""

    def __init__(self, capacity: int) -> None:
        if not 1 <= capacity <= 4:
            raise ValueError("invalid database lane capacity")
        self._settings = deepcopy(connection.settings_dict)
        self._settings["OPTIONS"] = self._settings.get("OPTIONS", {}) | {
            "connect_timeout": 3, "keepalives": 1, "keepalives_idle": 5,
            "keepalives_interval": 1, "keepalives_count": 3, "tcp_user_timeout": 10000,
        }
        self._capacity = BoundedSemaphore(capacity)
        self._pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="scan-database")

    def submit[T](self, function: Callable[[], T]) -> Future[T]:
        if not self._capacity.acquire(blocking=False):
            raise RuntimeError("scan database lane capacity exhausted")

        def execute() -> T:
            connection.close()
            connection.settings_dict = deepcopy(self._settings)
            try:
                connection.ensure_connection()
                require_runtime_database_login("indexer")
                with connection.cursor() as cursor:
                    cursor.execute("SET statement_timeout = '5s'")
                    cursor.execute("SET lock_timeout = '1s'")
                    cursor.execute("SET idle_in_transaction_session_timeout = '5s'")
                return function()
            finally:
                connection.close()

        try:
            future = self._pool.submit(execute)
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(lambda completed: self._capacity.release())
        return future

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)


def source_components(lease: ScanLease) -> tuple[bytes, ...]:
    """Resolve only the bounded, same-root source chain with captured revisions."""
    from aegis_apps.catalog.models import CatalogEntry
    from aegis_apps.catalog.names import source_name

    current = lease.directory_id
    expected_revision: int | None = lease.parent_revision
    components: list[bytes] = []
    seen = set()
    size = 0
    while len(components) <= MAX_COMPONENTS:
        if current in seen or expected_revision is None:
            raise UnsupportedTraversal
        seen.add(current)
        # One bounded row at a time: never materialize a root or a descendant tree.
        row = CatalogEntry.objects.filter(pk=current, root_id=lease.root_id).values(
            "source_parent_id", "source_revision", "source_parent_revision",
            "kind", "source_state", "raw_name",
        ).first()
        if (row is None or row["kind"] != "directory" or row["source_state"] != "present"
                or row["source_revision"] != expected_revision):
            raise UnsupportedTraversal
        if row["source_parent_id"] is None:
            if bytes(row["raw_name"]) != b"":
                raise UnsupportedTraversal
            return tuple(reversed(components))
        raw = bytes(row["raw_name"])
        source_name(raw)
        size += len(raw)
        if size > MAX_COMPONENT_BYTES or len(components) == MAX_COMPONENTS:
            raise UnsupportedTraversal
        components.append(raw)
        current = row["source_parent_id"]
        expected_revision = row["source_parent_revision"]
    raise UnsupportedTraversal


class ScanRuntime:
    """Fixed database lanes keep checkpoint latency outside the control loop."""

    def __init__(
        self, identity: WorkerIdentity, policy: ScanPolicy,
        spawn: Callable[[ReaderSource, UUID], ReaderHandle],
    ) -> None:
        self.identity, self.policy, self._spawn = identity, policy, spawn
        self.mutations = DatabaseLane(policy.readers)
        self.controls = DatabaseLane(policy.readers)
        self.publications = DatabaseLane(1)
        self.admissions = DatabaseLane(1)
        # These persistent launcher threads must outlive their PDEATHSIG children.
        self.launches = DatabaseLane(policy.readers)
        self._cancel_pool = ThreadPoolExecutor(max_workers=policy.readers)
        self._gates: dict[UUID, Event] = {}
        self._connections: dict[UUID, Any] = {}
        self._mutex = Lock()
        self._service: dict[UUID, int] = {}
        self._turn = 0

    def _gate(self, lease: ScanLease) -> Event:
        with self._mutex:
            return self._gates.setdefault(lease.work_id, Event())

    def forget(self, lease: ScanLease) -> None:
        with self._mutex:
            self._gates.pop(lease.work_id, None)

    def cancel(self, lease: ScanLease) -> None:
        self._gate(lease).set()
        with self._mutex:
            active = self._connections.get(lease.work_id)
        if active is not None:
            self._cancel_pool.submit(active.cancel_safe, timeout=1)

    def execute(self, lease: ScanLease, operation: str, payload: object) -> Future[Any]:
        from .checkpoints import fail_directory, finalize_directory, record_batch, seal_directory

        gate = self._gate(lease)

        def checkpoint() -> Any:
            with self._mutex:
                if operation != "fail" and gate.is_set():
                    raise RuntimeError("scan cancelled")
                self._connections[lease.work_id] = connection.connection
            try:
                if operation == "record" and isinstance(payload, ReaderBatch):
                    return record_batch(lease, payload)
                if operation == "seal" and isinstance(payload, ReaderComplete):
                    return seal_directory(lease, payload)
                if operation == "finalize" and payload == 500:
                    return finalize_directory(lease, 500)
                if operation == "fail" and isinstance(payload, str):
                    return fail_directory(lease, payload)
                raise ValueError("invalid scan mutation")
            finally:
                with self._mutex:
                    self._connections.pop(lease.work_id, None)

        return self.mutations.submit(checkpoint)

    def renew(self, lease: ScanLease) -> Future[bool]:
        return self.controls.submit(lambda: renew_directory(lease))

    def _manifest(self) -> Any:
        from aegis_apps.roots.manifest import configured_manifest

        manifest = configured_manifest()
        if manifest is None and self.identity.manifest_identity == "unconfigured:v1":
            return None
        if manifest is None or manifest.digest != self.identity.manifest_identity:
            raise RuntimeError("scan manifest changed")
        return manifest

    def spawn(self, lease: ScanLease) -> Future[ReaderHandle]:
        from aegis_apps.roots.models import Root

        from .models import RootIndexState

        gate = self._gate(lease)

        def launch() -> ReaderHandle:
            manifest = self._manifest()
            if manifest is None:
                raise RuntimeError("scan manifest unavailable")
            slot_id = Root.objects.filter(
                pk=lease.root_id, active=True, authorization_epoch=lease.root_epoch,
            ).order_by("id").values_list("slot_id", flat=True).first()
            state = RootIndexState.objects.filter(
                root_id=lease.root_id, active_run_id=lease.run_id,
                binding_epoch=lease.binding_epoch, policy_epoch=lease.policy_epoch,
            ).first()
            slot = None if slot_id is None else manifest.get(slot_id)
            if (state is None or slot is None or lease.manifest_identity != manifest.digest
                    or lease.worker_id != self.identity.worker_id or gate.is_set()):
                raise RuntimeError("scan source authority changed")
            components = source_components(lease)
            if gate.is_set():
                raise RuntimeError("scan cancelled")
            return self._spawn(ReaderSource(str(slot.container_path), slot.mount_fingerprint,
                                           components, self.policy.batch_records), lease.root_id)

        return self.launches.submit(launch)

    def heartbeat(self, metrics: dict[str, int], *, stopping: bool = False) -> Future[None]:
        from aegis_apps.operations.heartbeats import publish_heartbeat
        from aegis_apps.operations.selectors import current_schema_identity

        def publish() -> None:
            if not stopping:
                self._manifest()
                if current_schema_identity() != self.identity.schema_identity:
                    raise RuntimeError("scan schema changed")
            publish_heartbeat(
                role="indexer", worker_id=self.identity.worker_id,
                release_id=self.identity.release_id, schema_identity=self.identity.schema_identity,
                manifest_identity=self.identity.manifest_identity, current_job_id=None,
                status="stopping" if stopping else "running", metrics=metrics,
            )

        return self.publications.submit(publish)

    def claim_next(self, excluded: set[UUID]) -> ScanLease | None:
        from django.db.models import Q
        from django.db.models.functions import Now

        from aegis_apps.roots.models import Root

        from .scheduling import schedule_root_scan

        manifest = self._manifest()
        if manifest is None:
            return None
        roots = list(Root.objects.filter(active=True, slot_id__in=manifest.slots).filter(
            Q(index_state__isnull=True) | Q(index_state__active_run__isnull=False)
            | Q(index_state__due_at__lte=Now()),
        ).exclude(pk__in=excluded).order_by("index_state__due_at", "id").values_list(
            "pk", flat=True,
        )[:128])
        # Bounded last-service history; roots with no service are admitted first,
        # then due-time order from SQL breaks ties. A huge root cannot monopolize.
        self._service = {root: turn for root, turn in self._service.items()
                         if root in roots or root in excluded}
        for root_id in sorted(roots, key=lambda root: self._service.get(root, 0)):
            run = schedule_root_scan(root_id, self.identity.worker_id, manifest.digest)
            if run is not None:
                lease = claim_directory(run, self.identity.worker_id)
                if lease is not None:
                    self._turn += 1
                    self._service[root_id] = self._turn
                    return lease
        return None

    def close(self) -> None:
        for lane in (self.admissions, self.controls, self.mutations,
                     self.publications, self.launches):
            lane.close()
        self._cancel_pool.shutdown(wait=True, cancel_futures=True)


def run_indexer(identity: WorkerIdentity, stop_requested: Callable[[], bool]) -> None:
    from django.utils import timezone

    from aegis_apps.operations.leases import claim_next_job, relinquish_job
    from aegis_apps.operations.management.commands import run_role

    policy = ScanPolicy.from_environment(os.environ)
    coordination = Coordination()
    runtime = ScanRuntime(
        identity, policy, lambda source, root: ProcessReader(source, coordination, root),
    )
    supervisor = ScanSupervisor(
        spawn=runtime.spawn, execute=runtime.execute, renew=runtime.renew,
        heartbeat=lambda metrics: runtime.heartbeat(metrics, stopping=supervisor.stopping),
        cancel=runtime.cancel,
        timeout=policy.idle_timeout_seconds, max_readers=policy.readers,
    )
    leases: dict[UUID, ScanLease] = {}
    admission: Future[ScanLease | None] | None = None
    next_admission = 0.0
    stopping = False

    def admit() -> ScanLease | None:
        # Retain the actor-bound foundation path without pretending directory work
        # is an Operation. Its start gate and authorization checks remain intact.
        job = claim_next_job("indexer", identity.worker_id, timezone.now())
        if job is not None:
            if stop_requested():
                relinquish_job(job, now=timezone.now())
                return None
            run_role._execute_claim(identity, job)
        return None if stop_requested() else runtime.claim_next(set(leases))

    try:
        while True:
            now = time.monotonic()
            if stop_requested() and not stopping:
                stopping = True
                supervisor.stop()
            supervisor.tick()
            stopping = stopping or supervisor.stopping
            for root in set(leases) - supervisor.states.keys():
                runtime.forget(leases.pop(root))
            if admission is not None and admission.done():
                lease = admission.result()
                admission = None
                if lease is not None:
                    # Serialize against the existing signal/shutdown start gate.
                    with run_role._serialized_execution_start():
                        if stopping or stop_requested():
                            supervisor.discard(lease)
                        else:
                            supervisor.start(lease)
                        leases[lease.root_id] = lease
            if stopping and not supervisor.states and admission is None:
                break
            if (not stopping and admission is None and len(leases) < policy.readers
                    and now >= next_admission):
                admission = runtime.admissions.submit(admit)
                next_admission = now + 1
            time.sleep(.05)
    finally:
        supervisor.stop()
        while supervisor.states:
            supervisor.tick()
            time.sleep(.05)
        runtime.close()
        coordination.close()
