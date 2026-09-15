"""Metadata checkpoints owning top-level transactions; every return follows COMMIT.

Call on a dedicated autocommit-enabled Django connection with no outer transaction.
Do not change constraint timing inside these controlled transactions. Direct SQL
clients that deliberately force deferred checks early are outside the approved
absolute lease-deadline guarantee; they gain no filesystem or table-DML authority.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Lock
from typing import Any, Final
from uuid import UUID

from django.db import DatabaseError, connection, transaction
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from .database import ScanLease, parse_scan_lease, vars_from_lease
from .payloads import checkpoint_payload
from .protocol import ReaderBatch, ReaderComplete, ReaderFailure, encode_message


@dataclass(frozen=True, slots=True)
class BatchResult:
    observed: int
    inserted: int
    changed: int


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    affected: int
    complete: bool


class CheckpointCancellation:
    """One attempt's cancellation versus final commit-admission ordering.

    The mutex protects only local state, never SQL or network I/O. Cancellation
    before admit_commit forces rollback. A commit admitted first may still be
    in flight; cancellation closes all later admission but must await its outcome.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._cancelled = False
        self._committing = False

    def is_set(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> bool:
        with self._lock:
            self._cancelled = True
            return not self._committing

    def check(self) -> None:
        with self._lock:
            if self._cancelled:
                raise RuntimeError("scan cancelled")

    def admit_commit(self) -> None:
        with self._lock:
            if self._cancelled:
                raise RuntimeError("scan cancelled")
            self._committing = True

    def resolved(self) -> None:
        with self._lock:
            self._committing = False


_CANCELLATION: ContextVar[CheckpointCancellation | None] = ContextVar(
    "scan_checkpoint_cancellation", default=None,
)


@contextmanager
def checkpoint_cancellation(gate: CheckpointCancellation | None) -> Iterator[None]:
    token = _CANCELLATION.set(gate)
    try:
        yield
    finally:
        _CANCELLATION.reset(token)


_STATEMENTS: Final = {
    "record": "SELECT public.aegis_record_scan_batch(%s,%s)",
    "seal": "SELECT public.aegis_seal_scan_directory(%s,%s)",
    "finalize": "SELECT public.aegis_finalize_scan_directory(%s,%s)",
    "fail": "SELECT public.aegis_fail_scan_directory(%s,%s)",
}


def _lease_payload(lease: ScanLease) -> dict[str, object]:
    if type(lease) is not ScanLease:
        raise ValueError("invalid scan lease")
    payload = {key: str(value) if isinstance(value, UUID) else value
               for key, value in vars_from_lease(lease).items()}
    parse_scan_lease(payload)
    return payload


def _call(name: str, lease: ScanLease, argument: object) -> Any:
    payload = _lease_payload(lease)
    parameters: list[Any] = [Jsonb(payload), argument]
    connection.ensure_connection()
    if (
        connection.in_atomic_block or not connection.get_autocommit()
        or not connection.connection.autocommit
        or connection.connection.info.transaction_status != TransactionStatus.IDLE
    ):
        raise RuntimeError("scan checkpoint requires a top-level transaction")
    gate = _CANCELLATION.get()
    try:
        if gate is not None:
            gate.check()
        with transaction.atomic(durable=True):
            with connection.cursor() as cursor:
                if gate is not None:
                    gate.check()
                cursor.execute("SET CONSTRAINTS public.aegis_guard_directory_commit DEFERRED")
                if gate is not None:
                    gate.check()
                cursor.execute(_STATEMENTS[name], parameters)
                row = cursor.fetchone()
            if gate is not None:
                # Linearization point, immediately before atomic exits/COMMIT.
                # No further statement or callback may precede that commit.
                gate.admit_commit()
    except DatabaseError as error:
        state = getattr(error.__cause__, "sqlstate", None)
        if state == "22023":
            raise ValueError("invalid scan checkpoint") from None
        if state == "42501":
            raise PermissionError("scan authority denied") from None
        raise RuntimeError("scan checkpoint rejected") from None
    finally:
        if gate is not None:
            gate.resolved()
    value = None if row is None else row[0]
    if isinstance(value, str):
        if len(value) > 4096:
            raise RuntimeError("invalid scan checkpoint result")
        value = json.loads(value)
    return value


def record_batch(lease: ScanLease, batch: ReaderBatch) -> BatchResult:
    if type(batch) is not ReaderBatch:
        raise ValueError("invalid scan batch")
    value = _call("record", lease, Jsonb(checkpoint_payload(batch)))
    return BatchResult(**value) if value is not None else BatchResult(0, 0, 0)


def seal_directory(lease: ScanLease, complete: ReaderComplete) -> bool:
    if type(complete) is not ReaderComplete:
        raise ValueError("invalid scan completion")
    payload = json.loads(encode_message(complete)[4:])["identity"]
    return _call("seal", lease, Jsonb(payload)) is True


def finalize_directory(lease: ScanLease, limit: int = 500) -> FinalizeResult:
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("invalid scan finalization limit")
    value = _call("finalize", lease, limit)
    return FinalizeResult(**value) if value is not None else FinalizeResult(0, False)


def fail_directory(lease: ScanLease, code: str) -> bool:
    # Use the same safe failure-code set as the reader protocol.
    encode_message(ReaderFailure(code))  # type: ignore[arg-type]
    return _call("fail", lease, code) is True
