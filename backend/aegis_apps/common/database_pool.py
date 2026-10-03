"""Bounded-lifetime maintenance for the web server's pooled database logins.

psycopg's pool retires a connection past ``max_lifetime`` only when it is returned or
when ``check()`` runs; an idle connection is otherwise handed out unchecked. Each web
process therefore runs ``check()`` from ``request_started`` at most once per interval,
before the request takes a connection. Expired logins are closed and broken ones are
replaced, so a revoked or rotated web credential stops serving requests within
``max_lifetime`` plus one interval (plus any single request already in flight), even
after an idle period.
"""

from __future__ import annotations

import atexit
import threading
import time
from collections.abc import Callable
from typing import Any, Protocol

from aegis.config import WEB_DATABASE_POOL_CHECK_INTERVAL_SECONDS
from django.core.signals import request_started
from django.db import DEFAULT_DB_ALIAS, connections

_DISPATCH_UID = "aegis.web-database-pool-lifetime"


class _Pool(Protocol):
    @property
    def closed(self) -> bool: ...

    def check(self) -> None: ...

    def close(self, timeout: float = ...) -> None: ...


class PoolLifetimeEnforcer:
    """Run ``pool.check()`` at most once per interval, never concurrently."""

    def __init__(
        self,
        interval: float = WEB_DATABASE_POOL_CHECK_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not interval > 0:
            raise ValueError("pool check interval must be positive")
        self._interval = interval
        self._clock = clock
        self._lock = threading.Lock()
        self._next_check = 0.0

    def __call__(self, pool: _Pool | None) -> bool:
        if pool is None or pool.closed or self._clock() < self._next_check:
            return False
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._clock() < self._next_check:
                return False
            pool.check()
            self._next_check = self._clock() + self._interval
            return True
        finally:
            self._lock.release()


def _default_pool() -> _Pool | None:
    pool: _Pool | None = getattr(connections[DEFAULT_DB_ALIAS], "pool", None)
    return pool


_enforcer = PoolLifetimeEnforcer()


def enforce_pool_lifetime(**_kwargs: Any) -> None:
    _enforcer(_default_pool())


def close_pool() -> None:
    pool = _default_pool()
    if pool is not None and not pool.closed:
        pool.close(timeout=2.0)


def install() -> None:
    request_started.connect(enforce_pool_lifetime, dispatch_uid=_DISPATCH_UID)
    atexit.register(close_pool)
