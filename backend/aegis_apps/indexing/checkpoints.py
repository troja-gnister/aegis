"""Metadata checkpoints owning top-level transactions; every return follows COMMIT.

Call on a dedicated autocommit-enabled Django connection with no outer transaction.
Do not change constraint timing inside these controlled transactions. Direct SQL
clients that deliberately force deferred checks early are outside the approved
absolute lease-deadline guarantee; they gain no filesystem or table-DML authority.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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
    try:
        with transaction.atomic(durable=True), connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS public.aegis_guard_directory_commit DEFERRED")
            cursor.execute(_STATEMENTS[name], parameters)
            row = cursor.fetchone()
    except DatabaseError as error:
        state = getattr(error.__cause__, "sqlstate", None)
        if state == "22023":
            raise ValueError("invalid scan checkpoint") from None
        if state == "42501":
            raise PermissionError("scan authority denied") from None
        raise RuntimeError("scan checkpoint rejected") from None
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
