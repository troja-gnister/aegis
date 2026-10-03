"""Rate-limited, non-overlapping lifetime checks for the web server's pool."""
from __future__ import annotations

import threading

import pytest
from aegis_apps.common.database_pool import PoolLifetimeEnforcer


class FakePool:
    def __init__(self, *, closed: bool = False) -> None:
        self.closed = closed
        self.checks = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False

    def check(self) -> None:
        self.checks += 1
        if self.block:
            self.entered.set()
            assert self.release.wait(5)

    def close(self, timeout: float = 0.0) -> None:
        self.closed = True


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_checks_at_most_once_per_interval_and_again_after_it() -> None:
    clock, pool = Clock(), FakePool()
    enforcer = PoolLifetimeEnforcer(5.0, clock)

    assert enforcer(pool) is True
    clock.now += 4.9
    assert enforcer(pool) is False
    clock.now += 0.2
    assert enforcer(pool) is True
    assert pool.checks == 2


def test_an_idle_period_triggers_a_check_before_the_next_request() -> None:
    clock, pool = Clock(), FakePool()
    enforcer = PoolLifetimeEnforcer(5.0, clock)
    enforcer(pool)

    clock.now += 3600
    assert enforcer(pool) is True
    assert pool.checks == 2


def test_missing_or_unopened_pools_are_not_checked() -> None:
    enforcer = PoolLifetimeEnforcer(5.0, Clock())
    closed = FakePool(closed=True)

    assert enforcer(None) is False
    assert enforcer(closed) is False
    assert closed.checks == 0


def test_a_failed_check_is_retried_by_the_next_request() -> None:
    class Failing(FakePool):
        def check(self) -> None:
            self.checks += 1
            raise RuntimeError("check failed")

    clock, pool = Clock(), Failing()
    enforcer = PoolLifetimeEnforcer(5.0, clock)

    with pytest.raises(RuntimeError):
        enforcer(pool)
    with pytest.raises(RuntimeError):
        enforcer(pool)
    assert pool.checks == 2


def test_concurrent_requests_do_not_wait_for_or_repeat_a_running_check() -> None:
    clock, pool = Clock(), FakePool()
    pool.block = True
    enforcer = PoolLifetimeEnforcer(5.0, clock)
    results: list[bool] = []
    first = threading.Thread(target=lambda: results.append(enforcer(pool)))
    first.start()
    assert pool.entered.wait(5)

    assert enforcer(pool) is False
    pool.release.set()
    first.join(5)
    assert results == [True]
    assert pool.checks == 1


@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_interval_must_be_positive(interval: float) -> None:
    with pytest.raises(ValueError):
        PoolLifetimeEnforcer(interval)
