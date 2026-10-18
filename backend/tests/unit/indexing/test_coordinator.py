from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import nullcontext
from threading import Event, Thread
from typing import cast
from uuid import UUID, uuid4

import pytest
from aegis_apps.indexing.database import ScanLease
from aegis_apps.indexing.runner import ScanCoordinator
from aegis_apps.indexing.supervisor import ReaderState, ScanSupervisor


def lease(root: UUID | None = None) -> ScanLease:
    return ScanLease(uuid4(), uuid4(), root or uuid4(), uuid4(), str(uuid4()), 1, 1, 1, 1, 1, 1,
                     0, "a" * 64)


class FakeSupervisor:
    """Slots exist from start until the test settles them; nothing else happens."""

    def __init__(self) -> None:
        self.slots: dict[UUID, ReaderState] = {}
        self.discarded: list[ScanLease] = []
        self.stopping = False

    @property
    def states(self) -> dict[UUID, ReaderState]:
        return dict(self.slots)

    def tick(self) -> None:
        pass

    def start(self, candidate: ScanLease) -> None:
        self.slots[candidate.root_id] = ReaderState.STARTING

    def discard(self, candidate: ScanLease) -> None:
        self.discarded.append(candidate)
        self.slots[candidate.root_id] = ReaderState.STOPPING

    def stop(self) -> None:
        self.stopping = True

    def settle(self, root: UUID) -> None:
        del self.slots[root]


class Loop:
    """A coordinator on a fake clock whose admissions the test resolves by hand."""

    def __init__(self, readers: int = 1, wake: Event | None = None, idle: float = .05) -> None:
        self.now = 0.0
        self.supervisor = FakeSupervisor()
        self.admissions: list[tuple[float, frozenset[UUID], Future[ScanLease | None]]] = []
        self.forgotten: list[ScanLease] = []
        self.stop = False
        self.coordinator = ScanCoordinator(
            cast(ScanSupervisor, self.supervisor), self.admit, self.forgotten.append,
            readers=readers, stop_requested=lambda: self.stop, start_gate=nullcontext,
            wake=wake, idle=idle, monotonic=lambda: self.now,
        )

    def admit(self, excluded: frozenset[UUID]) -> Future[ScanLease | None]:
        future: Future[ScanLease | None] = Future()
        self.admissions.append((self.now, excluded, future))
        return future

    def step(self, at: float | None = None) -> bool:
        if at is not None:
            self.now = at
        return self.coordinator.step()

    def resolve(self, value: ScanLease | None) -> None:
        self.admissions[-1][2].set_result(value)


def test_a_settled_slot_is_admitted_again_on_the_same_pass() -> None:
    loop = Loop()
    loop.step(0)
    first = lease()
    loop.resolve(first)
    loop.step(0.01)
    assert loop.supervisor.slots == {first.root_id: ReaderState.STARTING}
    assert len(loop.admissions) == 1, "a full reader pool admits nothing"
    loop.supervisor.settle(first.root_id)
    loop.step(0.02)
    assert loop.forgotten == [first]
    assert [at for at, _, _ in loop.admissions] == [0, 0.02]


def test_an_admitted_lease_lets_the_next_admission_run_at_once() -> None:
    loop = Loop(readers=2)
    loop.step(0)
    first = lease()
    loop.resolve(first)
    loop.step(0.01)
    assert [(at, excluded) for at, excluded, _ in loop.admissions] == [
        (0, frozenset()), (0.01, frozenset({first.root_id})),
    ]


def test_only_an_admission_that_found_nothing_backs_off() -> None:
    loop = Loop()
    loop.step(0)
    loop.resolve(None)
    for now in (0.01, 0.5, 0.99):
        loop.step(now)
    assert len(loop.admissions) == 1
    loop.step(1.01)
    assert [at for at, _, _ in loop.admissions] == [0, 1.01]


def test_a_settling_slot_ends_an_empty_admission_backoff() -> None:
    loop = Loop(readers=2)
    loop.step(0)
    first = lease()
    loop.resolve(first)
    loop.step(0.01)
    loop.resolve(None)
    loop.step(0.02)
    loop.step(0.2)
    assert len(loop.admissions) == 2
    loop.supervisor.settle(first.root_id)
    loop.step(0.3)
    assert [at for at, _, _ in loop.admissions] == [0, 0.01, 0.3]


def test_a_slot_settling_during_an_empty_admission_skips_its_backoff() -> None:
    loop = Loop(readers=2)
    loop.step(0)
    first = lease()
    loop.resolve(first)
    loop.step(0.01)
    loop.supervisor.settle(first.root_id)
    loop.step(0.2)
    loop.resolve(None)
    loop.step(0.3)
    assert [(at, excluded) for at, excluded, _ in loop.admissions] == [
        (0, frozenset()), (0.01, frozenset({first.root_id})), (0.3, frozenset()),
    ]


def test_a_stop_discards_a_late_lease_and_ends_after_it_settles() -> None:
    loop = Loop()
    loop.step(0)
    loop.stop = True
    late = lease()
    loop.resolve(late)
    assert loop.step(0.01) is True
    assert loop.supervisor.stopping and loop.supervisor.discarded == [late]
    assert len(loop.admissions) == 1
    loop.supervisor.settle(late.root_id)
    assert loop.step(0.02) is False
    assert loop.forgotten == [late]


def _eventually(condition: Callable[[], bool], seconds: float) -> float:
    started = time.monotonic()
    while not condition():
        assert time.monotonic() - started < seconds, "condition not reached"
        time.sleep(.005)
    return time.monotonic() - started


def test_a_wake_event_ends_the_wait_long_before_its_timeout() -> None:
    wake = Event()
    loop = Loop(wake=wake, idle=30)
    runner = Thread(target=loop.coordinator.run, daemon=True)
    runner.start()
    try:
        _eventually(lambda: len(loop.admissions) == 1, 5)
        first = lease()
        # What a lane's done-callback does: resolve the future, then only set the event.
        loop.admissions[-1][2].set_result(first)
        wake.set()
        assert _eventually(lambda: first.root_id in loop.supervisor.slots, 5) < 1
        loop.stop = True
        loop.supervisor.settle(first.root_id)
        wake.set()
        runner.join(5)
        assert not runner.is_alive()
        assert loop.forgotten == [first]
    finally:
        loop.stop = True
        wake.set()


def test_an_admission_failure_still_propagates() -> None:
    loop = Loop()
    loop.step(0)
    loop.admissions[-1][2].set_exception(RuntimeError("scan database operation failed"))
    with pytest.raises(RuntimeError, match="scan database"):
        loop.step(0.01)
