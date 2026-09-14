"""Typed values at the four fixed scan database boundaries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from typing import Any, Final
from uuid import UUID

from django.db import DatabaseError, connection
from psycopg.types.json import Jsonb

DIRECTORY_LEASE_SECONDS: Final = 60
DIRECTORY_RENEW_INTERVAL_SECONDS: Final = 15
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_STATEMENTS: Final = {
    "schedule": "SELECT public.aegis_schedule_root_scan(%s,%s,%s)",
    "request": "SELECT public.aegis_request_root_scan(%s,%s,%s,%s)",
    "claim": "SELECT public.aegis_claim_scan_directory(%s,%s)",
    "renew": "SELECT public.aegis_renew_scan_directory(%s)",
}


class ScanRequestThrottled(ValueError):
    retry_after = 60

    def __init__(self) -> None:
        super().__init__("scan request rate limit exceeded")


@dataclass(frozen=True, slots=True)
class ScanLease:
    run_id: UUID
    work_id: UUID
    root_id: UUID
    directory_id: UUID
    worker_id: str
    attempt: int
    generation: int
    start_epoch: int
    binding_epoch: int
    policy_epoch: int
    root_epoch: int
    parent_revision: int
    manifest_identity: str


def vars_from_lease(lease: ScanLease) -> dict[str, object]:
    return {field.name: getattr(lease, field.name) for field in fields(lease)}


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("invalid scan identifier")
    try:
        result = UUID(value)
    except ValueError:
        raise ValueError("invalid scan identifier") from None
    if str(result) != value:
        raise ValueError("invalid scan identifier")
    return result


def parse_scan_lease(value: object) -> ScanLease:
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(ScanLease)}:
        raise ValueError("invalid scan lease")
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("invalid scan lease") from None
    if len(encoded.encode()) > 8192:
        raise ValueError("invalid scan lease")
    parsed: dict[str, Any] = dict(value)
    for name in ("run_id", "work_id", "root_id", "directory_id"):
        parsed[name] = _uuid(value[name])
    _uuid(value["worker_id"])
    for name in (
        "attempt",
        "generation",
        "start_epoch",
        "binding_epoch",
        "policy_epoch",
        "root_epoch",
        "parent_revision",
    ):
        if type(value[name]) is not int or not 0 <= value[name] <= 9223372036854775807:
            raise ValueError("invalid scan lease")
    if not isinstance(value["manifest_identity"], str) or not _DIGEST.fullmatch(
        value["manifest_identity"]
    ):
        raise ValueError("invalid scan lease")
    return ScanLease(**parsed)


def _call(function: str, parameters: list[Any]) -> Any:
    # Keys select only this private, closed registry; callers cannot supply SQL identifiers.
    try:
        with connection.cursor() as cursor:
            cursor.execute(_STATEMENTS[function], parameters)
            row = cursor.fetchone()
    except DatabaseError as error:
        state = getattr(error.__cause__, "sqlstate", None)
        if state == "22023":
            raise ValueError("invalid scan request") from None
        if state == "42501":
            raise PermissionError("scan authority denied") from None
        if state == "P0001":
            raise ScanRequestThrottled from None
        raise RuntimeError("scan database operation failed") from None
    return None if row is None else row[0]


def claim_directory(run_id: UUID, worker_id: str) -> ScanLease | None:
    if not isinstance(run_id, UUID):
        raise ValueError("invalid scan identifier")
    _uuid(worker_id)
    value = _call("claim", [run_id, worker_id])
    # Django's PostgreSQL backend deliberately loads raw cursor jsonb as text.
    if isinstance(value, str):
        if len(value.encode()) > 8192:
            raise ValueError("invalid scan lease")
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            raise ValueError("invalid scan lease") from None
    return None if value is None else parse_scan_lease(value)


def renew_directory(lease: ScanLease) -> bool:
    if not isinstance(lease, ScanLease):
        raise ValueError("invalid scan lease")
    payload = {
        name: str(value) if isinstance(value, UUID) else value
        for name, value in vars_from_lease(lease).items()
    }
    parse_scan_lease(payload)
    return _call("renew", [Jsonb(payload)]) is True
