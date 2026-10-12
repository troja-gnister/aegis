from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Mapping
from typing import Any, Final

from rest_framework import serializers

from aegis_apps.roots.permissions import Permission, validate_permission_mask

MAX_OPERATION_JSON_BYTES: Final = 16_384
MEDIA_REQUEST_PAYLOAD_VERSION: Final = 1
# An interactive media request needs both bits on its single root.
MEDIA_REQUEST_PERMISSIONS: Final = int(Permission.BROWSE | Permission.PREVIEW)
MAX_HEARTBEAT_METRICS_BYTES: Final = 1_024
MAX_POSTGRES_BIGINT: Final = 9_223_372_036_854_775_807
MAX_ROOTS_PER_OPERATION: Final = 128

_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,95}$")
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
_MEDIA_REQUEST_FIELDS = frozenset({"v", "rootId", "entryId", "fingerprint"})
_METRIC_KEYS = (
    "queueAgeSeconds",
    "scanProgress",
    "diskPressure",
    "diskCapacityBytes",
    "scanObservedEntries",
    "scanCompletedDirectories",
    "scanDegradedDirectories",
    # Media worker counts (aegis_apps.media.worker.media_metrics) and its last safe error
    # code: never a name or a path.
    "mediaInteractiveQueued",
    "mediaBackgroundPending",
    "mediaInFlight",
    "mediaAssets",
    "mediaAssetsComplete",
    # Ready thumbnails and posters: two heartbeats apart, they give the backfill rate.
    "mediaThumbnailsReady",
    "mediaPostersReady",
    "mediaProcessorFailures",
    "mediaLastErrorCode",
)
_METRIC_CODE_KEYS = frozenset({"mediaLastErrorCode"})
_METRIC_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


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


def _exact_uuid(value: object) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise ValueError("invalid media request payload")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise ValueError("invalid media request payload") from None
    if str(parsed) != value:
        raise ValueError("invalid media request payload")
    return value


def normalize_media_request_payload(value: object, *, intent: object) -> dict[str, object]:
    """The job payload of an interactive media request (``media.preview``/``transcode``).

    ``{"v": 1, "rootId": <uuid>, "entryId": <uuid>, "fingerprint": <64 hex>}``. The
    payload is bound to its operation intent: the intent names exactly that one root and
    requires BROWSE and PREVIEW on it, so the worker's authorization re-check (which only
    reads the intent and snapshot) covers the entry it is asked to process. The worker
    must still check that the entry belongs to ``rootId`` and that the asset's current
    fingerprint equals ``fingerprint``.
    """
    normalized_intent = normalize_probe_intent(intent)
    if not isinstance(value, Mapping) or set(value) != _MEDIA_REQUEST_FIELDS:
        raise ValueError("invalid media request payload")
    version = value["v"]
    if type(version) is not int or version != MEDIA_REQUEST_PAYLOAD_VERSION:
        raise ValueError("invalid media request payload")
    root_id = _exact_uuid(value["rootId"])
    entry_id = _exact_uuid(value["entryId"])
    fingerprint = value["fingerprint"]
    if not isinstance(fingerprint, str) or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise ValueError("invalid media request payload")
    roots = normalized_intent["roots"]
    if (
        not isinstance(roots, list)
        or len(roots) != 1
        or roots[0]["id"] != root_id
        or type(roots[0]["permissions"]) is not int
        or roots[0]["permissions"] & MEDIA_REQUEST_PERMISSIONS != MEDIA_REQUEST_PERMISSIONS
    ):
        raise ValueError("media request payload does not match its intent")
    normalized: dict[str, object] = {
        "v": MEDIA_REQUEST_PAYLOAD_VERSION,
        "rootId": root_id,
        "entryId": entry_id,
        "fingerprint": fingerprint,
    }
    canonical_json_bytes(normalized, maximum_bytes=MAX_OPERATION_JSON_BYTES)
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


def validate_heartbeat_metrics(value: object) -> dict[str, int | float | str]:
    if not isinstance(value, Mapping) or any(key not in _METRIC_KEYS for key in value):
        raise ValueError("invalid heartbeat metrics")
    normalized: dict[str, int | float | str] = {}
    for key in _METRIC_KEYS:
        if key not in value:
            continue
        metric = value[key]
        if key in _METRIC_CODE_KEYS:
            if not isinstance(metric, str) or _METRIC_CODE_RE.fullmatch(metric) is None:
                raise ValueError("invalid heartbeat metric")
            normalized[key] = metric
        elif key == "queueAgeSeconds":
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


class OperationRoleStatusSerializer(serializers.Serializer[Any]):
    role = serializers.ChoiceField(choices=("operations", "indexer", "media"), read_only=True)
    state = serializers.ChoiceField(choices=("healthy", "stale", "missing"), read_only=True)
    jobCount = serializers.IntegerField(min_value=0, max_value=2_147_483_647, read_only=True)
    oldestQueueAgeSeconds = serializers.IntegerField(
        min_value=0,
        max_value=2_147_483_647,
        allow_null=True,
        read_only=True,
    )
    scanProgress = serializers.FloatField(
        min_value=0,
        max_value=1,
        allow_null=True,
        read_only=True,
    )
    diskPressure = serializers.ChoiceField(
        choices=("ok", "warning", "critical", "unknown"),
        read_only=True,
    )
