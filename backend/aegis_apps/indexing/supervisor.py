"""Bounded scan coordination; IO and database calls complete outside this loop."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol
from uuid import UUID

from .protocol import (
    ReaderBatch,
    ReaderChannelState,
    ReaderComplete,
    ReaderFailure,
    ReaderMessage,
)

if TYPE_CHECKING:
    from .database import ScanLease


class ReaderState(StrEnum):
    STARTING = "starting"
    READING = "reading"
    FINALIZING = "finalizing"
    STOPPING = "stopping"
    UNREAPED = "unreaped"
    SETTLED = "settled"


class UnsupportedTraversal(ValueError):
    def __init__(self) -> None:
        super().__init__("unsupported_entry")


def progress_expired(*, now: float, last_progress: float, timeout: int) -> bool:
    return now - last_progress >= timeout


class ReaderHandle(Protocol):
    def receive(self) -> ReaderMessage | None: ...
    def acknowledge(self, sequence: int) -> None: ...
    def close_credits(self) -> None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def reaped(self) -> bool: ...


@dataclass(slots=True)
class _Slot:
    lease: ScanLease
    launch: Future[ReaderHandle]
    last_progress: float
    next_renewal: float
    state: ReaderState = ReaderState.STARTING
    reader: ReaderHandle | None = None
    channel: ReaderChannelState = field(default_factory=ReaderChannelState)
    renewal: Future[bool] | None = None
    mutation: Future[Any] | None = None
    operation: str | None = None
    message: ReaderMessage | None = None
    stop_time: float | None = None
    failed: bool = False
    launch_failed: bool = False
    eof_time: float | None = None
    failure_code: str = "source_unavailable"


class ScanSupervisor:
    """One slot per physical reader, retained until launch and reap have settled.

    Callbacks must return immediately with a bounded, independently executed
    future. Checkpoint futures resolve only after the accepted wrapper commits.
    Only this coordinator thread accepts packets, grants credits and changes state.
    """

    def __init__(
        self, *, spawn: Callable[[ScanLease], Future[ReaderHandle]],
        execute: Callable[[ScanLease, str, object], Future[Any]],
        renew: Callable[[ScanLease], Future[bool]],
        heartbeat: Callable[[dict[str, int]], Future[None]],
        cancel: Callable[[ScanLease], None],
        monotonic: Callable[[], float] = time.monotonic,
        timeout: int = 120, max_readers: int = 2,
    ) -> None:
        if type(timeout) is not int or not 30 <= timeout <= 3600:
            raise ValueError("invalid scan timeout")
        if type(max_readers) is not int or not 1 <= max_readers <= 4:
            raise ValueError("invalid reader limit")
        self._spawn, self._execute, self._renew = spawn, execute, renew
        self._heartbeat, self._cancel, self._clock = heartbeat, cancel, monotonic
        self._timeout, self._limit = timeout, max_readers
        self._slots: dict[UUID, _Slot] = {}
        self._stopping = False
        self._next_heartbeat = monotonic() + 15
        self._publication: Future[None] | None = None
        self._metrics = {
            "scanObservedEntries": 0, "scanCompletedDirectories": 0, "scanDegradedDirectories": 0,
        }

    @property
    def metrics(self) -> dict[str, int]:
        return dict(self._metrics)

    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def states(self) -> dict[UUID, ReaderState]:
        return {root: slot.state for root, slot in self._slots.items()}

    def has_unreaped_reader(self, root_id: UUID) -> bool:
        slot = self._slots.get(root_id)
        return slot is not None and not slot.launch_failed and (
            slot.reader is None or not slot.reader.reaped()
        )

    def start(self, lease: ScanLease) -> None:
        if self._stopping or lease.root_id in self._slots or len(self._slots) >= self._limit:
            raise RuntimeError("scan reader slot unavailable")
        now = self._clock()
        self._slots[lease.root_id] = _Slot(lease, self._spawn(lease), now, now + 15)

    def discard(self, lease: ScanLease) -> None:
        """Settle a late claim asynchronously without opening any source."""
        if lease.root_id in self._slots or len(self._slots) >= self._limit:
            raise RuntimeError("scan reader slot unavailable")
        now = self._clock()
        slot = _Slot(lease, Future(), now, now + 15, launch_failed=True)
        self._slots[lease.root_id] = slot
        self._stop_slot(slot, "source_unavailable")

    def _increment(self, key: str, count: int = 1) -> None:
        self._metrics[key] = min(9223372036854775807, self._metrics[key] + count)

    def _stop_slot(self, slot: _Slot, code: str) -> None:
        if slot.state in (ReaderState.STOPPING, ReaderState.UNREAPED, ReaderState.SETTLED):
            return
        slot.state = ReaderState.STOPPING
        slot.stop_time = self._clock()
        slot.failure_code = code
        self._cancel(slot.lease)
        self._increment("scanDegradedDirectories")
        if slot.reader is not None:
            slot.reader.close_credits()
            slot.reader.terminate()

    def stop(self) -> None:
        self._stopping = True
        for slot in self._slots.values():
            self._stop_slot(slot, "source_unavailable")

    def _submit(self, slot: _Slot, operation: str, payload: object = None) -> None:
        slot.operation = operation
        slot.mutation = self._execute(slot.lease, operation, payload)

    def _finished_mutation(self, slot: _Slot, now: float) -> None:
        if slot.mutation is None or not slot.mutation.done():
            return
        future, operation = slot.mutation, slot.operation
        slot.mutation = None
        if slot.state in (ReaderState.STOPPING, ReaderState.UNREAPED):
            # Resolve errors without treating a late commit as authority to send credits.
            with suppress(Exception):
                future.result()
            return
        result = future.result()
        if operation == "record":
            assert isinstance(slot.message, ReaderBatch) and slot.reader is not None
            if result.observed > 0:
                slot.last_progress = now
                self._increment("scanObservedEntries", result.observed)
            slot.channel.acknowledge(slot.message.sequence)
            slot.reader.acknowledge(slot.message.sequence)
            slot.message = None
        elif operation == "seal":
            if result is not True:
                self._stop_slot(slot, "source_unavailable")
            else:
                slot.state = ReaderState.FINALIZING
        elif operation == "finalize":
            if result.complete:
                self._increment("scanCompletedDirectories")
                slot.state = ReaderState.SETTLED
            elif result.affected == 0:
                self._stop_slot(slot, "source_unavailable")

    def _tick_slot(self, slot: _Slot, now: float) -> None:
        if slot.reader is None and not slot.launch_failed and slot.launch.done():
            try:
                slot.reader = slot.launch.result()
            except UnsupportedTraversal:
                slot.launch_failed = True
                self._stop_slot(slot, "unsupported_entry")
            except Exception:
                slot.launch_failed = True
                self._stop_slot(slot, "source_unavailable")
            else:
                if slot.state == ReaderState.STARTING:
                    slot.state = ReaderState.READING
                else:
                    slot.reader.close_credits()
                    slot.reader.terminate()
        if slot.state in (ReaderState.STARTING, ReaderState.READING) and progress_expired(
            now=now, last_progress=slot.last_progress, timeout=self._timeout,
        ):
            self._stop_slot(slot, "reader_timeout")
        if slot.state in (ReaderState.STARTING, ReaderState.READING, ReaderState.FINALIZING):
            # An outstanding renewal closes the local commit admission gate. It
            # is resolved before any queued checkpoint or reader packet is used.
            if slot.renewal is None and now >= slot.next_renewal:
                slot.renewal = self._renew(slot.lease)
            if slot.renewal is not None:
                if not slot.renewal.done():
                    if now - slot.next_renewal < 15:
                        return
                    self._stop_slot(slot, "source_unavailable")
                else:
                    renewed = slot.renewal.result()
                    slot.renewal = None
                    if not renewed:
                        self._stop_slot(slot, "source_unavailable")
                    slot.next_renewal = now + 15
        self._finished_mutation(slot, now)
        if slot.state in (ReaderState.STOPPING, ReaderState.UNREAPED):
            if slot.mutation is None and not slot.failed:
                slot.failed = True
                self._submit(slot, "fail", slot.failure_code)
            if slot.reader is not None and not slot.reader.reaped():
                assert slot.stop_time is not None
                if now - slot.stop_time >= 5:
                    slot.reader.kill()
                    slot.state = ReaderState.UNREAPED
            if ((slot.launch_failed or (slot.reader is not None and slot.reader.reaped()))
                    and slot.mutation is None and slot.failed):
                slot.state = ReaderState.SETTLED
            return
        if slot.mutation is not None:
            return
        if slot.state == ReaderState.FINALIZING:
            assert slot.reader is not None and slot.eof_time is not None
            if not slot.reader.reaped():
                if now - slot.eof_time >= 5:
                    self._stop_slot(slot, "reader_timeout")
                return
            self._submit(slot, "finalize", 500)
        elif slot.state == ReaderState.READING:
            assert slot.reader is not None
            message = slot.reader.receive()
            if message is None:
                # Process exit does not imply its pipe has been fully decoded.
                # The transport reports EOF/protocol failure after draining bytes.
                return
            slot.channel.accept(message)
            slot.message = message
            if isinstance(message, ReaderBatch):
                self._submit(slot, "record", message)
            elif isinstance(message, ReaderComplete):
                slot.eof_time = now
                self._submit(slot, "seal", message)
            elif isinstance(message, ReaderFailure):
                self._stop_slot(slot, message.code)

    def tick(self) -> None:
        now = self._clock()
        if self._publication is not None and self._publication.done():
            try:
                self._publication.result()
            except Exception:
                self.stop()
            self._publication = None
        if now >= self._next_heartbeat and self._publication is None:
            self._publication = self._heartbeat(self.metrics)
            self._next_heartbeat = now + 15
        # Each root receives at most one new packet/checkpoint per tick.
        for root, slot in tuple(self._slots.items()):
            try:
                self._tick_slot(slot, now)
            except Exception:
                self._stop_slot(slot, "reader_protocol_error")
            if slot.state == ReaderState.SETTLED and (
                slot.launch_failed or (slot.reader is not None and slot.reader.reaped())
            ):
                if slot.reader is not None:
                    slot.reader.close_credits()
                del self._slots[root]
