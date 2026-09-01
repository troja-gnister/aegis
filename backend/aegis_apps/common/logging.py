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
_CLASS_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_EVENT_MESSAGES = {
    "health.readiness.database": "database readiness check failed",
}
_LOGGER_MESSAGES = {
    "uvicorn.access": "HTTP request completed",
    "uvicorn.error": "ASGI server event",
    "django.request": "HTTP response",
    "django.server": "HTTP response",
}
_SAFE_ERROR_CODES = frozenset({"DATABASE_UNAVAILABLE"})
_SAFE_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _encoded(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _uuid(value: object) -> str | None:
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str):
        return None
    if len(value) > 36:
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
        level = record.levelname if record.levelname in _SAFE_LEVELS else "ERROR"
        raw_logger_name = record.name
        trusted_logger_name = (
            raw_logger_name
            if type(raw_logger_name) is str
            and len(raw_logger_name) <= 64
            and raw_logger_name in _LOGGER_MESSAGES
            else None
        )
        logger_name = trusted_logger_name or "application"
        event_value = getattr(record, "event", None)
        event = (
            event_value
            if isinstance(event_value, str)
            and len(event_value) <= 96
            and event_value in _EVENT_MESSAGES
            else None
        )
        if event is not None:
            message = _EVENT_MESSAGES[event]
        else:
            message = (
                _LOGGER_MESSAGES[trusted_logger_name]
                if trusted_logger_name is not None
                else "untrusted log message omitted"
            )

        payload: dict[str, Any] = {
            "timestamp": timestamp,
            "level": level,
            "logger": logger_name,
            "message": message,
        }

        request_id_value = getattr(record, "request_id", None)
        if request_id_value is None:
            request_id_value = request_id_var.get("")
        if (
            isinstance(request_id_value, str)
            and len(request_id_value) <= 64
            and REQUEST_ID.fullmatch(request_id_value)
        ):
            payload["request_id"] = request_id_value

        actor_id = _uuid(getattr(record, "actor_id", None))
        if actor_id is not None:
            payload["actor_id"] = actor_id

        if event is not None:
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
            if len(exception_class) > 128 or not _CLASS_NAME.fullmatch(exception_class):
                exception_class = "Exception"
            exception: dict[str, str] = {"class": exception_class}
            error_code = getattr(record, "error_code", None)
            if (
                isinstance(error_code, str)
                and len(error_code) <= 64
                and error_code in _SAFE_ERROR_CODES
            ):
                exception["error_code"] = error_code
            payload["exception"] = exception

        rendered = _encoded(payload)
        if len(rendered.encode("utf-8")) <= _MAX_JSON_BYTES:
            return rendered

        payload["metadata"] = {"truncated": True}
        rendered = _encoded(payload)
        if len(rendered.encode("utf-8")) <= _MAX_JSON_BYTES:
            return rendered

        fallback = {
            "timestamp": timestamp,
            "level": level,
            "logger": "aegis",
            "message": "log record exceeded safe bounds",
            "truncated": True,
        }
        return _encoded(fallback)
