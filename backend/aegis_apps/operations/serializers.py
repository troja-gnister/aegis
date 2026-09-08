from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping
from typing import Final

from aegis_apps.roots.permissions import validate_permission_mask

MAX_OPERATION_JSON_BYTES: Final = 16_384
MAX_HEARTBEAT_METRICS_BYTES: Final = 1_024
MAX_POSTGRES_BIGINT: Final = 9_223_372_036_854_775_807
MAX_ROOTS_PER_OPERATION: Final = 128

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,95}$")
_METRIC_KEYS = (
    "queueAgeSeconds",
    "scanProgress",
    "diskPressure",
    "diskCapacityBytes",
)


def canonical_json_bytes(value: object, *, maximum_bytes: int) -> bytes:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise ValueError("invalid bounded JSON value") from exc
    if len(encoded) > maximum_bytes:
        raise ValueError("JSON value exceeds size limit")
    return encoded


def _canonical_uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("invalid operation intent root ID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid operation intent root ID") from None
    canonical = str(parsed)
    if value.lower() != canonical:
        raise ValueError("invalid operation intent root ID")
    return canonical


def normalize_probe_intent(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"roots"}:
        raise ValueError("invalid operation intent schema")
    roots = value["roots"]
    if isinstance(roots, (str, bytes)) or not isinstance(roots, list):
        raise ValueError("invalid operation intent roots")
    if len(roots) > MAX_ROOTS_PER_OPERATION:
        raise ValueError("operation intent has too many roots")

    normalized_roots: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in roots:
        if not isinstance(item, Mapping) or set(item) != {"id", "permissions"}:
            raise ValueError("invalid operation intent root schema")
        root_id = _canonical_uuid(item["id"])
        try:
            permissions = validate_permission_mask(item["permissions"])
        except ValueError:
            raise ValueError("invalid operation intent permission mask") from None
        if not permissions:
            raise ValueError("invalid operation intent permission mask")
        if root_id in seen:
            raise ValueError("duplicate operation intent root")
        seen.add(root_id)
        normalized_roots.append({"id": root_id, "permissions": int(permissions)})

    normalized_roots.sort(key=lambda item: str(item["id"]))
    normalized: dict[str, object] = {"roots": normalized_roots}
    try:
        canonical_json_bytes(normalized, maximum_bytes=MAX_OPERATION_JSON_BYTES)
    except ValueError as exc:
        raise ValueError("invalid or oversized operation intent") from exc
    return normalized


def validate_authorization_snapshot(
    value: object,
    *,
    intent: object,
) -> dict[str, object]:
    normalized_intent = normalize_probe_intent(intent)
    if not isinstance(value, Mapping) or set(value) != {"userEpoch", "rootEpochs"}:
        raise ValueError("invalid authorization snapshot")
    user_epoch = value["userEpoch"]
    root_epochs = value["rootEpochs"]
    if type(user_epoch) is not int or not 0 <= user_epoch <= MAX_POSTGRES_BIGINT:
        raise ValueError("invalid authorization snapshot")
    if not isinstance(root_epochs, Mapping):
        raise ValueError("invalid authorization snapshot")
    normalized_roots = normalized_intent["roots"]
    if not isinstance(normalized_roots, list):
        raise ValueError("invalid authorization snapshot")
    expected_root_ids = [str(item["id"]) for item in normalized_roots]
    if set(root_epochs) != set(expected_root_ids):
        raise ValueError("invalid authorization snapshot")
    normalized_epochs: dict[str, int] = {}
    for root_id in expected_root_ids:
        epoch = root_epochs[root_id]
        if type(epoch) is not int or not 0 <= epoch <= MAX_POSTGRES_BIGINT:
            raise ValueError("invalid authorization snapshot")
        normalized_epochs[root_id] = epoch
    normalized: dict[str, object] = {
        "userEpoch": user_epoch,
        "rootEpochs": normalized_epochs,
    }
    canonical_json_bytes(normalized, maximum_bytes=MAX_OPERATION_JSON_BYTES)
    return normalized


def validate_probe_result(value: object) -> dict[str, bool]:
    if not isinstance(value, Mapping) or set(value) != {"ok"} or value["ok"] is not True:
        raise ValueError("invalid foundation probe result")
    normalized = {"ok": True}
    canonical_json_bytes(normalized, maximum_bytes=MAX_OPERATION_JSON_BYTES)
    return normalized


def _finite_number(value: object, *, minimum: float, maximum: float | None) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid heartbeat metric")
    number = float(value)
    if not math.isfinite(number) or number < minimum:
        raise ValueError("invalid heartbeat metric")
    if maximum is not None and number > maximum:
        raise ValueError("invalid heartbeat metric")
    return value


def validate_heartbeat_metrics(value: object) -> dict[str, int | float]:
    if not isinstance(value, Mapping) or any(key not in _METRIC_KEYS for key in value):
        raise ValueError("invalid heartbeat metrics")
    normalized: dict[str, int | float] = {}
    for key in _METRIC_KEYS:
        if key not in value:
            continue
        metric = value[key]
        if key == "queueAgeSeconds":
            normalized[key] = _finite_number(metric, minimum=0, maximum=None)
        elif key in {"scanProgress", "diskPressure"}:
            normalized[key] = _finite_number(metric, minimum=0, maximum=1)
        elif (
            type(metric) is not int
            or metric < 0
            or metric > MAX_POSTGRES_BIGINT
        ):
            raise ValueError("invalid heartbeat metric")
        else:
            normalized[key] = metric
    try:
        canonical_json_bytes(normalized, maximum_bytes=MAX_HEARTBEAT_METRICS_BYTES)
    except ValueError as exc:
        raise ValueError("invalid heartbeat metrics") from exc
    return normalized


def validate_safe_identity(value: object, *, field_name: str, maximum_length: int = 96) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum_length
        or _IDENTITY_RE.fullmatch(value) is None
    ):
        raise ValueError(f"invalid {field_name}")
    return value


def canonical_worker_id(value: object) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("invalid worker ID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid worker ID") from None
    canonical = str(parsed)
    if value != canonical:
        raise ValueError("invalid worker ID")
    return canonical
