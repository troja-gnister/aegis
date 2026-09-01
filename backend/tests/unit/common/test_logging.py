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
        msg="request failed: %s",
        args=("raw-credential",),
        safe_metadata={"api_key": "private-key", "ok": 4},
        request_id="request_ID-1234",
        actor_id="01234567-89ab-cdef-0123-456789abcdef",
        event="health.readiness.database",
    )

    rendered = BoundedJSONFormatter().format(record)
    value = json.loads(rendered)

    assert "\n" not in rendered
    assert "raw-credential" not in rendered
    assert "private-key" not in rendered
    assert value["message"] == "request failed: %s"
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
            msg="operation failed",
            exc_info=exc_info,
            error_code="DATABASE_UNAVAILABLE",
            actor_id="not-a-uuid",
        )
    )
    value = json.loads(rendered)

    assert value["exception"] == {
        "class": "RuntimeError",
        "error_code": "DATABASE_UNAVAILABLE",
    }
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
