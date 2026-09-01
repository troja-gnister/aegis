from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from types import TracebackType
from typing import cast

from aegis_apps.common.logging import BoundedJSONFormatter


def _record(**values: object) -> logging.LogRecord:
    args = cast(
        tuple[object, ...] | Mapping[str, object] | None,
        values.pop("args", ()),
    )
    exc_info = cast(
        tuple[type[BaseException], BaseException, TracebackType | None]
        | tuple[None, None, None]
        | None,
        values.pop("exc_info", None),
    )
    record = logging.LogRecord(
        name="aegis.test",
        level=logging.ERROR,
        pathname="/private/source.py",
        lineno=9,
        msg=values.pop("msg", "safe message"),
        args=args,
        exc_info=exc_info,
        func=None,
    )
    for key, value in values.items():
        setattr(record, key, value)
    return record


def test_json_formatter_redacts_metadata_and_does_not_interpolate_args() -> None:
    record = _record(
        msg="raw-message-credential %s",
        args=("raw-credential",),
        safe_metadata={"api_key": "private-key", "ok": 4},
        request_id="request_ID-1234",
        actor_id="01234567-89ab-cdef-0123-456789abcdef",
    )

    rendered = BoundedJSONFormatter().format(record)
    value = json.loads(rendered)

    assert "\n" not in rendered
    assert "raw-credential" not in rendered
    assert "private-key" not in rendered
    assert "raw-message-credential" not in rendered
    assert value["message"] == "untrusted log message omitted"
    assert value["metadata"] == {"api_key": "[REDACTED]", "ok": 4}
    assert value["request_id"] == "request_ID-1234"
    assert value["actor_id"] == "01234567-89ab-cdef-0123-456789abcdef"


def test_exception_output_contains_only_class_and_allowlisted_code() -> None:
    try:
        raise RuntimeError("SQL /private/path raw-credential")
    except RuntimeError:
        exc_info = sys.exc_info()

    rendered = BoundedJSONFormatter().format(
        _record(
            msg="raw-operation-message",
            exc_info=exc_info,
            error_code="DATABASE_UNAVAILABLE",
            event="health.readiness.database",
            actor_id="not-a-uuid",
        )
    )
    value = json.loads(rendered)

    assert value["exception"] == {
        "class": "RuntimeError",
        "error_code": "DATABASE_UNAVAILABLE",
    }
    assert value["event"] == "health.readiness.database"
    assert value["message"] == "database readiness check failed"
    assert "SQL" not in rendered
    assert "/private/path" not in rendered
    assert "raw-credential" not in rendered
    assert "actor_id" not in value


def test_every_formatter_output_is_valid_json_within_64_kib() -> None:
    rendered = BoundedJSONFormatter().format(
        _record(
            msg="\U0001f4a5" * 100_000,
            safe_metadata={"values": ["\U0001f4a5" * 10_000 for _ in range(1_000)]},
            request_id="invalid request id",
            error_code="unsafe-code",
        )
    )

    value = json.loads(rendered)
    assert isinstance(value, dict)
    assert "\n" not in rendered
    assert len(rendered.encode("utf-8")) <= 64 * 1024


def test_unknown_or_pathological_structured_values_are_never_echoed() -> None:
    event_canary = "event-canary-" + "x" * 1_000_000
    code_canary = "UNKNOWN_CODE_" + "Y" * 1_000_000
    message_canary = "message-canary-" + "z" * 1_000_000

    rendered = BoundedJSONFormatter().format(
        _record(msg=message_canary, event=event_canary, error_code=code_canary)
    )
    value = json.loads(rendered)

    assert message_canary not in rendered
    assert "message-canary" not in rendered
    assert "event-canary" not in rendered
    assert "UNKNOWN_CODE" not in rendered
    assert value["message"] == "untrusted log message omitted"
    assert "event" not in value
    assert "error_code" not in value


def test_uvicorn_access_record_uses_static_message_without_path_or_query() -> None:
    canary = "/private/path?credential=raw-secret"
    rendered = BoundedJSONFormatter().format(
        logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname="uvicorn/protocols/http/h11_impl.py",
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=(("127.0.0.1", 1234), "GET", canary, "1.1", 200),
            exc_info=None,
        )
    )

    value = json.loads(rendered)
    assert canary not in rendered
    assert value["logger"] == "uvicorn.access"
    assert value["message"] == "HTTP request completed"
