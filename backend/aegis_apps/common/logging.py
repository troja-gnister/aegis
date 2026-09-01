from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .middleware import REQUEST_ID
from .redaction import redact
from .request_context import request_id_var

_MAX_JSON_BYTES = 64 * 1024
_MAX_METADATA_BYTES = 24 * 1024
_MAX_MESSAGE_CHARS = 2_048
_EVENT = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+){0,15}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        class_name = re.sub(r"[^A-Za-z0-9_]", "_", type(value).__name__)[:64]
        return f"[UNSAFE:{class_name or 'object'}]"
    return value[:limit]


def _encoded(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _uuid(value: object) -> str | None:
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


class BoundedJSONFormatter(logging.Formatter):
    """Format a LogRecord without interpolating untrusted arguments or exceptions."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).isoformat(
            timespec="milliseconds"
        )
        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": _text(record.levelname, 32),
            "logger": _text(record.name, 256),
            "message": _text(record.msg, _MAX_MESSAGE_CHARS),
        }

        request_id_value = getattr(record, "request_id", None)
        if request_id_value is None:
            request_id_value = request_id_var.get("")
        if isinstance(request_id_value, str) and REQUEST_ID.fullmatch(request_id_value):
            payload["request_id"] = request_id_value

        actor_id = _uuid(getattr(record, "actor_id", None))
        if actor_id is not None:
            payload["actor_id"] = actor_id

        event = getattr(record, "event", None)
        if isinstance(event, str) and _EVENT.fullmatch(event):
            payload["event"] = event

        if hasattr(record, "safe_metadata"):
            metadata = redact(record.safe_metadata)
            try:
                metadata_encoded = _encoded(metadata)
            except (TypeError, ValueError):
                metadata = {"invalid": True}
            else:
                if len(metadata_encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
                    metadata = {"truncated": True}
            payload["metadata"] = metadata

        if record.exc_info is not None:
            exception_type = record.exc_info[0]
            exception_class = exception_type.__name__ if exception_type is not None else "Exception"
            if not _CLASS_NAME.fullmatch(exception_class):
                exception_class = "Exception"
            exception: dict[str, str] = {"class": exception_class}
            error_code = getattr(record, "error_code", None)
            if isinstance(error_code, str) and _ERROR_CODE.fullmatch(error_code):
                exception["error_code"] = error_code
            payload["exception"] = exception

        rendered = _encoded(payload)
        if len(rendered.encode("utf-8")) <= _MAX_JSON_BYTES:
            return rendered

        payload["metadata"] = {"truncated": True}
        payload["message"] = _text(record.msg, 256)
        rendered = _encoded(payload)
        if len(rendered.encode("utf-8")) <= _MAX_JSON_BYTES:
            return rendered

        fallback = {
            "timestamp": timestamp,
            "level": _text(record.levelname, 32),
            "logger": "aegis",
            "message": "log record exceeded safe bounds",
            "truncated": True,
        }
        return _encoded(fallback)
