from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from django.db import transaction

from aegis_apps.common.middleware import REQUEST_ID
from aegis_apps.common.redaction import redact
from aegis_apps.identity.models import User

from .models import AuditEvent, _audit_write_capability

_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*){1,15}$")
_OUTCOMES = frozenset({"success", "failure", "denied"})
_MAX_METADATA_BYTES = 16 * 1024


def _identifier(value: UUID | str | None, field_name: str) -> UUID | None:
    if value is None:
        return None
    try:
        return value if isinstance(value, UUID) else UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise ValueError(f"{field_name} must be a UUID") from None


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("audit metadata must be an object")
    candidate = dict(value)
    try:
        raw = json.dumps(
            candidate,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("audit metadata must be valid JSON") from None
    if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("audit metadata exceeds safe bounds")

    safe = redact(candidate)
    if not isinstance(safe, dict):
        raise ValueError("audit metadata must be an object")
    encoded = json.dumps(safe, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("audit metadata exceeds safe bounds")
    return safe


def record_event(
    *,
    event_type: str,
    outcome: str,
    actor: User | None,
    request_id: str,
    root_id: UUID | str | None = None,
    object_id: UUID | str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AuditEvent:
    if (
        not isinstance(event_type, str)
        or not 1 <= len(event_type) <= 96
        or _EVENT_TYPE.fullmatch(event_type) is None
    ):
        raise ValueError("invalid audit event type")
    if not isinstance(outcome, str) or not 1 <= len(outcome) <= 24 or outcome not in _OUTCOMES:
        raise ValueError("invalid audit outcome")
    if (
        not isinstance(request_id, str)
        or not 8 <= len(request_id) <= 64
        or REQUEST_ID.fullmatch(request_id) is None
    ):
        raise ValueError("invalid audit request ID")
    if actor is not None and (not isinstance(actor, User) or actor.pk is None):
        raise ValueError("invalid audit actor")

    event = AuditEvent(
        event_type=event_type,
        outcome=outcome,
        actor=actor,
        request_id=request_id,
        root_id=_identifier(root_id, "root_id"),
        object_id=_identifier(object_id, "object_id"),
        metadata=_metadata(metadata),
    )
    with transaction.atomic():
        token = _audit_write_capability.set(event)
        try:
            event.save()
        finally:
            _audit_write_capability.reset(token)
    return event
