from __future__ import annotations

import json

from aegis_apps.common.redaction import redact


def test_recursive_redaction_removes_paths_and_credentials() -> None:
    value = redact(
        {
            "password": "secret",
            "API-Key": "key",
            "nested": [{"Auth_orization": "bearer"}, {"file-name": "/private"}],
            "ok": 3,
        }
    )

    assert value == {
        "password": "[REDACTED]",
        "API-Key": "[REDACTED]",
        "nested": [
            {"Auth_orization": "[REDACTED]"},
            {"file-name": "[REDACTED]"},
        ],
        "ok": 3,
    }


def test_redaction_bounds_cycles_depth_count_strings_and_bytes() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    value = redact(
        {
            "cycle": cyclic,
            "deep": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}},
            "many": list(range(1_000)),
            "text": "x" * 20_000,
            "bytes": b"y" * 20_000,
        }
    )

    encoded = json.dumps(value, ensure_ascii=True)
    assert "[CYCLE]" in encoded
    assert "[MAX_DEPTH]" in encoded
    assert "[TRUNCATED]" in encoded
    assert len(encoded.encode("utf-8")) < 64 * 1024


def test_redaction_never_calls_unknown_object_text() -> None:
    class Unsafe:
        def __str__(self) -> str:
            raise AssertionError("must not stringify unknown objects")

    assert redact({"value": Unsafe()}) == {"value": "[UNSAFE:Unsafe]"}
