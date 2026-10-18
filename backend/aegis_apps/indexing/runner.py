"""Attested scanner admission and separately connected database execution."""

from __future__ import annotations

import os
import select
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager, suppress
from copy import deepcopy
from threading import BoundedSemaphore, Lock, local
from typing import TYPE_CHECKING, Any
from uuid import UUID

from django.db import connection
from psycopg.pq import TransactionStatus

from aegis_apps.common.database_privileges import require_runtime_database_login

from .checkpoints import CheckpointCancellation, checkpoint_cancellation
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
    """A fixed number of sessions, with no unbounded executor submission queue.

    Each worker thread keeps its indexer login between submissions: no SCRAM exchange,
    login check or session settings per call, and PL/pgSQL plans and relation caches stay
    warm. A thread replaces its session before the next use after any error, after a cancel
    was dispatched to it (``retire``), when the server sent anything while it was idle (a
    restart or termination notice), and once it is ``max_age`` seconds old, which bounds how
    long an out-of-band password change or NOLOGIN takes to apply.
    """

    def __init__(
        self, capacity: int, *, wake: Callable[[], None] | None = None, max_age: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 1 <= capacity <= 4:
            raise ValueError("invalid database lane capacity")
        self._settings = deepcopy(connection.settings_dict)
        self._settings["OPTIONS"] = self._settings.get("OPTIONS", {}) | {
            "connect_timeout": 3, "keepalives": 1, "keepalives_idle": 5,
            "keepalives_interval": 1, "keepalives_count": 3, "tcp_user_timeout": 10000,
        }
        self._capacity = BoundedSemaphore(capacity)
        self._pool = ThreadPoolExecutor(max_workers=capacity, thread_name_prefix="scan-database")
        self._wake, self._max_age, self._clock = wake, max_age, clock
        self._local = local()
        self._mutex = Lock()
        # Open sessions by identity (kept alive, so an identity is never reused while listed).
        self._sessions: dict[int, Any] = {}
        self._retired: set[int] = set()

    def submit[T](self, function: Callable[[], T]) -> Future[T]:
        if not self._capacity.acquire(blocking=False):
            raise RuntimeError("scan database lane capacity exhausted")

        def execute() -> T:
            try:
                self._session()
                return function()
            except BaseException:
                self._discard()
                raise
            finally:
                # Before the future completes: whoever sees it done may submit at once.
                self._capacity.release()

        def settled(completed: Future[T]) -> None:
            if completed.cancelled():  # it never ran, so it never released its slot
                self._capacity.release()
            if self._wake is not None:
                self._wake()

        try:
            future = self._pool.submit(execute)
        except BaseException:
            self._capacity.release()
            raise
        future.add_done_callback(settled)
        return future

    def retire(self, session: object) -> None:
        """Never reuse ``session``: a cancel dispatched to it may still arrive."""
        with self._mutex:
            if id(session) in self._sessions:
                self._retired.add(id(session))

    def _reusable(self, session: Any) -> bool:
        with self._mutex:
            if id(session) in self._retired:
                return False
        if (self._clock() - self._local.opened >= self._max_age or session.closed
                or session.broken or not session.autocommit or connection.in_atomic_block
                or session.info.transaction_status != TransactionStatus.IDLE):
            return False
        try:
            # An idle session receives nothing unless the server is ending it.
            return not select.select([session.fileno()], [], [], 0)[0]
        except (OSError, ValueError):
            return False

    def _session(self) -> None:
        session = connection.connection
        if (session is not None and session is getattr(self._local, "session", None)
                and self._reusable(session)):
            return
        self._discard()
        connection.settings_dict = deepcopy(self._settings)
        connection.ensure_connection()
        session = connection.connection
        with self._mutex:
            self._sessions[id(session)] = session
        self._local.session, self._local.opened = session, self._clock()
        require_runtime_database_login("indexer")
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '5s'")
            cursor.execute("SET lock_timeout = '1s'")
            cursor.execute("SET idle_in_transaction_session_timeout = '5s'")

    def _discard(self) -> None:
        session = getattr(self._local, "session", None)
        self._local.session = None
        with suppress(Exception):
            connection.close()
        if session is not None:
            with self._mutex:
                self._sessions.pop(id(session), None)
                self._retired.discard(id(session))

    def close(self) -> None:
        self._pool.shutdown(wait=True, cancel_futures=True)
        with self._mutex:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._retired.clear()
        for session in sessions:
            with suppress(Exception):
                session.close()


# The bounded parent walk of one directory: each step joins the previous row's source parent
# in the same root, never a root or a descendant tree, and the walk stops after the deepest
# chain a reader accepts (MAX_COMPONENTS components, then the anchor).
_SOURCE_CHAIN = """
WITH RECURSIVE chain AS (
    SELECT 0 AS depth, entry.id, entry.source_parent_id, entry.source_revision,
           entry.source_parent_revision, entry.kind, entry.source_state, entry.raw_name
      FROM catalog_catalogentry AS entry
     WHERE entry.id = %(directory)s AND entry.root_id = %(root)s
    UNION ALL
    SELECT chain.depth + 1, parent.id, parent.source_parent_id, parent.source_revision,
           parent.source_parent_revision, parent.kind, parent.source_state, parent.raw_name
      FROM chain JOIN catalog_catalogentry AS parent
        ON parent.id = chain.source_parent_id AND parent.root_id = %(root)s
     WHERE chain.depth < %(limit)s
)
SELECT id, source_parent_id, source_revision, source_parent_revision, kind, source_state,
       raw_name
  FROM chain ORDER BY depth
"""


def source_components(lease: ScanLease) -> tuple[bytes, ...]:
    """Resolve only the bounded, same-root source chain with captured revisions."""
    from aegis_apps.catalog.names import source_name

    with connection.cursor() as cursor:
        cursor.execute(_SOURCE_CHAIN, {
            "directory": lease.directory_id, "root": lease.root_id, "limit": MAX_COMPONENTS,
        })
        rows = cursor.fetchall()
    current = lease.directory_id
    expected_revision: int | None = lease.parent_revision
    components: list[bytes] = []
    seen = set()
    size = 0
    # One statement; the rows then meet the checks of a row-at-a-time walk, in its order.
    for entry_id, parent_id, revision, parent_revision, kind, state, raw_name in rows:
        if entry_id != current or current in seen or expected_revision is None:
            raise UnsupportedTraversal
        seen.add(current)
        if kind != "directory" or state != "present" or revision != expected_revision:
            raise UnsupportedTraversal
        if parent_id is None:
            if bytes(raw_name) != b"":
                raise UnsupportedTraversal
            return tuple(reversed(components))
        raw = bytes(raw_name)
        source_name(raw)
        size += len(raw)
        if size > MAX_COMPONENT_BYTES or len(components) == MAX_COMPONENTS:
            raise UnsupportedTraversal
        components.append(raw)
        current = parent_id
        expected_revision = parent_revision
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
        self._gates: dict[UUID, CheckpointCancellation] = {}
        self._connections: dict[UUID, Any] = {}
        self._mutex = Lock()
        self._service: dict[UUID, int] = {}
        self._turn = 0

    def _gate(self, lease: ScanLease) -> CheckpointCancellation:
        with self._mutex:
            return self._gates.setdefault(lease.work_id, CheckpointCancellation())

    def forget(self, lease: ScanLease) -> None:
        with self._mutex:
            self._gates.pop(lease.work_id, None)

    def cancel(self, lease: ScanLease) -> None:
        interrupt = self._gate(lease).cancel()
        with self._mutex:
            active = self._connections.get(lease.work_id)
            if active is not None and interrupt:
                # The cancel is asynchronous and may land after this lease's statement has
                # finished. Retired while still registered to this lease, the session is
                # replaced before its thread runs anything else, so no later statement
                # (another lease's) can be the one it aborts.
                self.mutations.retire(active)
        if active is not None and interrupt:
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
                with checkpoint_cancellation(None if operation == "fail" else gate):
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


class ScanCoordinator:
    """The control loop around one supervisor: tick, settle, admit, one pass at a time.

    A root whose slot settles is admitted again on the same pass. Only an admission that
    returned no lease, with no slot settling while it ran, waits a second for the next one.
    """

    def __init__(
        self, supervisor: ScanSupervisor,
        admit: Callable[[frozenset[UUID]], Future[ScanLease | None]],
        forget: Callable[[ScanLease], None], *, readers: int, stop_requested: Callable[[], bool],
        start_gate: Callable[[], AbstractContextManager[object]],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._supervisor, self._admit, self._forget = supervisor, admit, forget
        self._readers, self._stop_requested = readers, stop_requested
        self._start_gate, self._clock = start_gate, monotonic
        self.leases: dict[UUID, ScanLease] = {}
        self.admission: Future[ScanLease | None] | None = None
        self.stopping = False
        self._next_admission = 0.0
        self._settled = self._settled_at_admission = 0

    def step(self) -> bool:
        """One pass; False once stopping has settled every slot and the admission."""
        now = self._clock()
        if self._stop_requested() and not self.stopping:
            self.stopping = True
            self._supervisor.stop()
        self._supervisor.tick()
        self.stopping = self.stopping or self._supervisor.stopping
        for root in set(self.leases) - self._supervisor.states.keys():
            self._forget(self.leases.pop(root))
            # The root's next directory may be claimable at once: no backoff applies.
            self._settled += 1
            self._next_admission = now
        if self.admission is not None and self.admission.done():
            lease = self.admission.result()
            self.admission = None
            if lease is None:
                if self._settled == self._settled_at_admission:
                    self._next_admission = now + 1
            else:
                # Serialize against the existing signal/shutdown start gate.
                with self._start_gate():
                    if self.stopping or self._stop_requested():
                        self._supervisor.discard(lease)
                    else:
                        self._supervisor.start(lease)
                    self.leases[lease.root_id] = lease
        if self.stopping and not self._supervisor.states and self.admission is None:
            return False
        if (not self.stopping and self.admission is None and len(self.leases) < self._readers
                and now >= self._next_admission):
            self._settled_at_admission = self._settled
            self.admission = self._admit(frozenset(self.leases))
        return True

    def run(self) -> None:
        while self.step():
            time.sleep(.05)


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

    def admit(excluded: frozenset[UUID]) -> ScanLease | None:
        # Retain the actor-bound foundation path without pretending directory work
        # is an Operation. Its start gate and authorization checks remain intact.
        job = claim_next_job("indexer", identity.worker_id, timezone.now())
        if job is not None:
            if stop_requested():
                relinquish_job(job, now=timezone.now())
                return None
            run_role._execute_claim(identity, job)
        return None if stop_requested() else runtime.claim_next(set(excluded))

    coordinator = ScanCoordinator(
        supervisor, lambda excluded: runtime.admissions.submit(lambda: admit(excluded)),
        runtime.forget, readers=policy.readers, stop_requested=stop_requested,
        start_gate=run_role._serialized_execution_start,
    )
    try:
        coordinator.run()
    finally:
        supervisor.stop()
        while supervisor.states:
            supervisor.tick()
            time.sleep(.05)
        runtime.close()
        coordination.close()
