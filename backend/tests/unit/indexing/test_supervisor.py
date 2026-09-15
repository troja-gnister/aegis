from __future__ import annotations

import os
import select
import struct
import subprocess
import sys
from collections import deque
from concurrent.futures import Future
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import uuid4

import pytest
from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind, Observation, SourceState
from aegis_apps.catalog.names import source_name
from aegis_apps.indexing.checkpoints import BatchResult, FinalizeResult
from aegis_apps.indexing.database import ScanLease
from aegis_apps.indexing.protocol import (
    ReaderBatch,
    ReaderComplete,
    ReaderMessage,
    encode_message,
    read_message,
)
from aegis_apps.indexing.supervisor import ReaderHandle, ReaderState, ScanSupervisor


def resolved(value: Any) -> Future[Any]:
    future: Future[Any] = Future()
    future.set_result(value)
    return future


def batch(sequence: int = 1) -> ReaderBatch:
    return ReaderBatch(sequence, (Observation(
        source_name(b"entry"), EntryKind.FILE, SourceState.PRESENT, 12, 1, 1, 1, 2,
    ),))


class FakeReader:
    """A controllable process boundary; packets still cross the real decoder."""

    def __init__(self) -> None:
        self.frames: deque[bytes] = deque()
        self.acknowledged: list[int] = []
        self.alive = True
        self.reapable = True
        self.terminated = False
        self.killed = False
        self.closed = False
        self.delivery_paused = False

    def send(self, message: ReaderMessage) -> None:
        self.frames.append(encode_message(message))

    def receive(self) -> ReaderMessage | None:
        if self.delivery_paused:
            return None
        return read_message(BytesIO(self.frames.popleft())) if self.frames else None

    def acknowledge(self, sequence: int) -> None:
        assert not self.closed
        self.acknowledged.append(sequence)

    def close_credits(self) -> None:
        self.closed = True

    def terminate(self) -> None:
        self.terminated = True
        if self.reapable:
            self.alive = False

    def kill(self) -> None:
        self.killed = True
        if self.reapable:
            self.alive = False

    def reaped(self) -> bool:
        return not self.alive


class SupervisorFixture:
    def __init__(self) -> None:
        self.now = 0.0
        self.lease = ScanLease(
            uuid4(), uuid4(), uuid4(), uuid4(), str(uuid4()), 1, 1, 1, 1, 1, 1, 0, "a" * 64,
        )
        self.readers: list[FakeReader] = []
        self.renewals = 0
        self.heartbeats: list[dict[str, int]] = []
        self.operations: list[tuple[str, object]] = []
        self.pending: Future[Any] | None = None
        self.renew_result = True
        self.pending_renewal: Future[bool] | None = None
        self.batch_result = BatchResult(1, 1, 0)
        self.finalize_result = FinalizeResult(0, True)
        self.cancelled: list[ScanLease] = []
        self.supervisor = ScanSupervisor(
            spawn=self.spawn, execute=self.execute, renew=self.renew, heartbeat=self.heartbeat,
            cancel=self.cancelled.append, monotonic=lambda: self.now,
        )

    def spawn(self, lease: ScanLease) -> Future[ReaderHandle]:
        reader = FakeReader()
        self.readers.append(reader)
        return resolved(reader)

    def execute(self, lease: ScanLease, operation: str, payload: object) -> Future[Any]:
        self.operations.append((operation, payload))
        if operation == "record":
            return self.pending if self.pending is not None else resolved(self.batch_result)
        if operation == "finalize":
            return resolved(self.finalize_result)
        return resolved(True)

    def renew(self, lease: ScanLease) -> Future[bool]:
        self.renewals += 1
        if self.pending_renewal is not None:
            return self.pending_renewal
        return resolved(self.renew_result)

    def heartbeat(self, metrics: dict[str, int]) -> Future[None]:
        self.heartbeats.append(dict(metrics))
        return resolved(None)

    def start(self) -> FakeReader:
        self.supervisor.start(self.lease)
        self.tick()
        return self.readers[-1]

    def tick(self) -> None:
        for _ in range(4):
            self.supervisor.tick()

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.tick()


@pytest.fixture
def supervisor_fixture() -> SupervisorFixture:
    return SupervisorFixture()


def test_reader_progress_does_not_block_renewal(supervisor_fixture: SupervisorFixture) -> None:
    control = supervisor_fixture
    reader = control.start()
    reader.send(batch())
    control.tick()
    control.advance(16)
    assert control.renewals >= 1 and len(control.heartbeats) >= 1
    control.advance(120)
    assert ("fail", "reader_timeout") in control.operations
    assert not any(name == "finalize" for name, _ in control.operations)
    assert reader.terminated


def test_unreaped_reader_prevents_replacement(supervisor_fixture: SupervisorFixture) -> None:
    control = supervisor_fixture
    reader = control.start()
    reader.reapable = False
    control.supervisor.stop()
    control.advance(10)
    assert reader.terminated and reader.killed
    assert control.supervisor.has_unreaped_reader(control.lease.root_id)
    assert control.supervisor.states[control.lease.root_id] == ReaderState.UNREAPED
    with pytest.raises(RuntimeError):
        control.supervisor.start(replace(control.lease, attempt=2))
    assert len(control.readers) == 1


def test_unreaped_stop_keeps_safe_heartbeat_live(supervisor_fixture: SupervisorFixture) -> None:
    control = supervisor_fixture
    control.start().reapable = False
    control.supervisor.stop()
    control.advance(16)
    assert control.heartbeats[-1]["scanDegradedDirectories"] == 1


def test_late_claim_discard_never_spawns_or_waits(supervisor_fixture: SupervisorFixture) -> None:
    control = supervisor_fixture
    control.supervisor.stop()
    control.supervisor.discard(control.lease)
    assert control.readers == [] and control.operations == []
    control.tick()
    assert control.operations == [("fail", "source_unavailable")]
    assert control.supervisor.states == {}


def test_unsupported_source_chain_has_explicit_failure(
    supervisor_fixture: SupervisorFixture,
) -> None:
    from aegis_apps.indexing.runner import UnsupportedTraversal

    control = supervisor_fixture
    failed: Future[ReaderHandle] = Future()
    failed.set_exception(UnsupportedTraversal())
    control.supervisor._spawn = lambda lease: failed
    control.supervisor.start(control.lease)
    control.tick()
    assert control.operations == [("fail", "unsupported_entry")]


def test_credit_waits_for_commit_without_blocking_liveness(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    control.pending = Future()
    reader = control.start()
    reader.send(batch())
    reader.send(batch(2))
    control.tick()
    control.advance(16)
    assert reader.acknowledged == []
    assert control.renewals >= 1 and control.heartbeats
    assert [name for name, _ in control.operations].count("record") == 1
    control.pending.set_result(BatchResult(1, 1, 0))
    control.pending = None
    control.tick()
    assert reader.acknowledged == [1, 2]


def test_failed_renewal_discards_queued_batch_before_checkpoint(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    reader = control.start()
    reader.send(batch())
    control.renew_result = False
    control.advance(16)
    assert not any(name == "record" for name, _ in control.operations)
    assert control.cancelled == [control.lease]
    assert reader.closed


def test_zero_delta_does_not_refresh_progress_timeout(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    control.batch_result = BatchResult(0, 0, 0)
    reader = control.start()
    control.advance(110)
    reader.send(batch())
    control.tick()
    control.advance(10)
    assert ("fail", "reader_timeout") in control.operations


def test_healthy_observation_progress_can_exceed_timeout(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    reader = control.start()
    for sequence in range(1, 6):
        control.advance(60)
        reader.send(batch(sequence))
        control.tick()
    assert not reader.terminated
    assert control.supervisor.metrics["scanObservedEntries"] == 5


def test_eof_finalization_is_bounded_and_stale_result_stops(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    control.finalize_result = FinalizeResult(0, False)
    reader = control.start()
    reader.send(ReaderComplete(DirectoryIdentity(1, 2, 1, 1)))
    reader.alive = False
    control.tick()
    control.tick()
    assert [name for name, _ in control.operations].count("finalize") == 1
    assert control.supervisor.metrics["scanCompletedDirectories"] == 0


def test_global_pool_is_bounded_and_other_root_progresses(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    control.start()
    second = replace(control.lease, root_id=uuid4(), work_id=uuid4(), directory_id=uuid4())
    control.supervisor.start(second)
    control.tick()
    with pytest.raises(RuntimeError):
        control.supervisor.start(replace(second, root_id=uuid4()))
    reader = control.readers[1]
    reader.send(batch())
    reader.send(ReaderComplete(DirectoryIdentity(1, 2, 1, 1)))
    reader.alive = False
    control.tick()
    control.tick()
    assert control.supervisor.metrics["scanCompletedDirectories"] == 1
    assert not control.readers[0].terminated


def test_stop_is_monotonic_and_does_not_apply_queued_observations(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    reader = control.start()
    reader.send(batch())
    control.supervisor.stop()
    control.tick()
    assert not any(name == "record" for name, _ in control.operations)
    with pytest.raises(RuntimeError):
        control.supervisor.start(replace(control.lease, root_id=uuid4()))


def test_pending_renewal_cannot_disable_process_deadlines(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    reader = control.start()
    control.pending_renewal = Future()
    control.advance(16)
    control.advance(120)
    assert reader.terminated
    assert control.cancelled == [control.lease]


def test_eof_reader_must_reap_before_missing_finalization(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    reader = control.start()
    reader.reapable = False
    reader.send(ReaderComplete(DirectoryIdentity(1, 2, 1, 1)))
    control.tick()
    control.advance(10)
    control.advance(10)
    assert reader.terminated and reader.killed
    assert not any(name == "finalize" for name, _ in control.operations)
    assert control.supervisor.has_unreaped_reader(control.lease.root_id)


def test_reaped_process_does_not_discard_a_terminal_frame_still_being_delivered(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    reader = control.start()
    reader.send(ReaderComplete(DirectoryIdentity(1, 2, 1, 1)))
    reader.alive = False
    reader.delivery_paused = True
    control.tick()
    assert not control.cancelled
    reader.delivery_paused = False
    control.tick()
    control.tick()
    assert control.supervisor.metrics["scanCompletedDirectories"] == 1


def test_failed_launch_settles_without_claiming_a_physical_reader(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    failed: Future[ReaderHandle] = Future()
    failed.set_exception(OSError("synthetic launch failure"))
    control.supervisor = ScanSupervisor(
        spawn=lambda lease: failed, execute=control.execute, renew=control.renew,
        heartbeat=control.heartbeat, cancel=control.cancelled.append, monotonic=lambda: control.now,
    )
    control.supervisor.start(control.lease)
    control.tick()
    control.tick()
    assert not control.supervisor.has_unreaped_reader(control.lease.root_id)
    assert not control.supervisor.states
    assert [name for name, _ in control.operations].count("fail") == 1


@pytest.mark.parametrize("stopped_while_launching", [False, True])
def test_partial_launch_failure_retains_unreapable_reader(
    supervisor_fixture: SupervisorFixture, stopped_while_launching: bool,
) -> None:
    from aegis_apps.indexing.processes import ReaderLaunchFailure

    control = supervisor_fixture
    reader = FakeReader()
    reader.reapable = False
    failed: Future[ReaderHandle] = Future()
    control.supervisor._spawn = lambda lease: failed
    control.supervisor.start(control.lease)
    if stopped_while_launching:
        control.supervisor.stop()
    failed.set_exception(ReaderLaunchFailure(reader))
    control.tick()
    assert reader.closed and reader.terminated
    control.advance(6)
    assert control.supervisor.states[control.lease.root_id] == ReaderState.UNREAPED
    assert control.supervisor.has_unreaped_reader(control.lease.root_id)
    with pytest.raises(RuntimeError, match="slot unavailable"):
        control.supervisor.start(control.lease)
    reader.reapable = True
    control.tick()
    control.tick()
    assert control.supervisor.states == {}


def test_stopping_waits_for_admitted_commit_without_late_credit(
    supervisor_fixture: SupervisorFixture,
) -> None:
    control = supervisor_fixture
    control.pending = Future()
    reader = control.start()
    reader.send(batch())
    control.tick()
    control.supervisor.stop()
    control.advance(16)
    assert control.supervisor.states[control.lease.root_id] == ReaderState.STOPPING
    assert control.heartbeats and reader.acknowledged == []
    control.pending.set_result(BatchResult(1, 1, 0))
    control.tick()
    assert control.supervisor.states == {}
    assert reader.acknowledged == []


def test_actual_sender_has_only_two_unacknowledged_pipe_batches() -> None:
    child = r'''
import sys
from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind, Observation, SourceState
from aegis_apps.catalog.names import source_name
from aegis_apps.indexing.reader import send_reader_messages
from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete
item = Observation(source_name(b"entry"), EntryKind.FILE, SourceState.PRESENT, 1, 1, 1, 1, 1)
def messages():
    for sequence in range(1, 4):
        yield ReaderBatch(sequence, (item,))
    yield ReaderComplete(DirectoryIdentity(1, 1, 1, 1))
send_reader_messages(messages(), sys.stdin.buffer, sys.stdout.buffer)
assert "django" not in sys.modules
'''
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(Path(__file__).resolve().parents[3]),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    with subprocess.Popen(
        [sys.executable, "-c", child], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=environment, close_fds=True, bufsize=0,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        output = cast(BinaryIO, process.stdout)
        try:
            assert select.select([process.stdout], [], [], 5)[0], "sender produced no first frame"
            if process.poll() is not None:
                assert process.stderr is not None
                pytest.fail(process.stderr.read().decode("utf-8", "replace"))
            first, second = read_message(output), read_message(output)
            assert isinstance(first, ReaderBatch) and first.sequence == 1
            assert isinstance(second, ReaderBatch) and second.sequence == 2
            assert select.select([process.stdout], [], [], 0.2)[0] == []
            process.stdin.write(struct.pack("!Q", 1))
            third = read_message(output)
            assert isinstance(third, ReaderBatch) and third.sequence == 3
            assert select.select([process.stdout], [], [], 0.2)[0] == []
            process.stdin.write(struct.pack("!Q", 2))
            assert isinstance(read_message(output), ReaderComplete)
            process.stdin.write(struct.pack("!Q", 3))
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
