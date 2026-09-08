from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from aegis.config import ConfigurationError

WORKER_ROLES = ("operations", "indexer", "media")
DEVELOPMENT_RELEASE_ID = "development"
_SAFE_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,95}$")


def validated_release_identity(value: object, *, production: bool) -> str:
    if not isinstance(value, str) or _SAFE_IDENTITY_RE.fullmatch(value) is None:
        raise ConfigurationError("AEGIS_RELEASE_ID is invalid")
    if production and value.casefold() == DEVELOPMENT_RELEASE_ID:
        raise ConfigurationError("AEGIS_RELEASE_ID is required in production")
    return value


def _bounded_float(
    environ: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
    allow_zero: bool = False,
) -> float:
    raw = environ.get(name, str(default))
    if not isinstance(raw, str) or raw != raw.strip():
        raise ConfigurationError(f"{name} is invalid")
    try:
        value = float(raw)
    except ValueError:
        raise ConfigurationError(f"{name} must be numeric") from None
    lower_valid = value >= minimum if allow_zero else value > minimum
    if not math.isfinite(value) or not lower_valid or value > maximum:
        raise ConfigurationError(f"{name} is outside its safe bounds")
    return value


def _bounded_integer(
    environ: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = environ.get(name, str(default))
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        raise ConfigurationError(f"{name} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} is outside its safe bounds")
    return value


@dataclass(frozen=True, slots=True)
class WorkerRuntimeConfig:
    release_id: str
    process_role: str | None
    required_roles: tuple[str, ...]
    lease_seconds: float
    retry_base_seconds: float
    retry_max_seconds: float
    heartbeat_seconds: float
    heartbeat_fresh_seconds: float
    heartbeat_retention_seconds: float
    heartbeat_slots_per_role: int
    poll_seconds: float
    poll_jitter_seconds: float

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> WorkerRuntimeConfig:
        environment = environ.get("AEGIS_ENV", "development").strip().lower()
        release_id = validated_release_identity(
            environ.get("AEGIS_RELEASE_ID", DEVELOPMENT_RELEASE_ID),
            production=environment == "production",
        )

        process_role = environ.get("AEGIS_PROCESS_ROLE")
        if process_role is not None and process_role not in WORKER_ROLES:
            raise ConfigurationError("AEGIS_PROCESS_ROLE is invalid")

        raw_roles = environ.get("AEGIS_REQUIRED_WORKER_ROLES", ",".join(WORKER_ROLES))
        if not isinstance(raw_roles, str):
            raise ConfigurationError("AEGIS_REQUIRED_WORKER_ROLES is invalid")
        required_roles = tuple(part.strip() for part in raw_roles.split(","))
        if (
            not required_roles
            or any(role not in WORKER_ROLES for role in required_roles)
            or len(set(required_roles)) != len(required_roles)
        ):
            raise ConfigurationError("AEGIS_REQUIRED_WORKER_ROLES is invalid")

        lease_seconds = _bounded_float(
            environ,
            "AEGIS_JOB_LEASE_SECONDS",
            30,
            minimum=0,
            maximum=300,
        )
        retry_base_seconds = _bounded_float(
            environ,
            "AEGIS_JOB_RETRY_BASE_SECONDS",
            1,
            minimum=0,
            maximum=300,
        )
        retry_max_seconds = _bounded_float(
            environ,
            "AEGIS_JOB_RETRY_MAX_SECONDS",
            300,
            minimum=0,
            maximum=86_400,
        )
        if retry_base_seconds > retry_max_seconds:
            raise ConfigurationError("job retry bounds are invalid")

        heartbeat_seconds = _bounded_float(
            environ,
            "AEGIS_WORKER_HEARTBEAT_SECONDS",
            10,
            minimum=0,
            maximum=15,
        )
        heartbeat_fresh_seconds = _bounded_float(
            environ,
            "AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS",
            45,
            minimum=0,
            maximum=300,
        )
        if heartbeat_fresh_seconds < heartbeat_seconds:
            raise ConfigurationError("worker heartbeat freshness is invalid")
        heartbeat_retention_seconds = _bounded_float(
            environ,
            "AEGIS_WORKER_HEARTBEAT_RETENTION_SECONDS",
            3600,
            minimum=0,
            maximum=604_800,
        )
        if heartbeat_retention_seconds < heartbeat_fresh_seconds:
            raise ConfigurationError("worker heartbeat retention is invalid")
        heartbeat_slots_per_role = _bounded_integer(
            environ,
            "AEGIS_WORKER_HEARTBEAT_SLOTS_PER_ROLE",
            64,
            minimum=1,
            maximum=1_024,
        )

        poll_seconds = _bounded_float(
            environ,
            "AEGIS_QUEUE_POLL_SECONDS",
            1,
            minimum=0,
            maximum=30,
        )
        poll_jitter_seconds = _bounded_float(
            environ,
            "AEGIS_QUEUE_POLL_JITTER_SECONDS",
            0.25,
            minimum=0,
            maximum=30,
            allow_zero=True,
        )
        if poll_jitter_seconds > poll_seconds:
            raise ConfigurationError("queue poll jitter is invalid")

        return cls(
            release_id=release_id,
            process_role=process_role,
            required_roles=required_roles,
            lease_seconds=lease_seconds,
            retry_base_seconds=retry_base_seconds,
            retry_max_seconds=retry_max_seconds,
            heartbeat_seconds=heartbeat_seconds,
            heartbeat_fresh_seconds=heartbeat_fresh_seconds,
            heartbeat_retention_seconds=heartbeat_retention_seconds,
            heartbeat_slots_per_role=heartbeat_slots_per_role,
            poll_seconds=poll_seconds,
            poll_jitter_seconds=poll_jitter_seconds,
        )
