from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

REDACTED = "[REDACTED]"
_MAX_DEPTH = 8
_MAX_ITEMS = 256
_MAX_TEXT = 2_048
_SENSITIVE_MARKERS = (
    "password",
    "secret",
    "token",
    "authorization",
    "cookie",
    "path",
    "filename",
    "content",
    "apikey",
    "username",
    "email",
)


def _type_name(value: object) -> str:
    name = type(value).__name__
    return re.sub(r"[^A-Za-z0-9_]", "_", name)[:64] or "object"


def _bounded_text(value: str) -> str:
    if len(value) <= _MAX_TEXT:
        return value
    return f"{value[:_MAX_TEXT]}[TRUNCATED]"


def _sensitive(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(marker in normalized for marker in _SENSITIVE_MARKERS)


@dataclass
class _State:
    remaining: int = _MAX_ITEMS


def redact(value: object) -> Any:
    """Return a bounded, JSON-safe copy with recursively redacted sensitive fields."""
    return _redact(value, state=_State(), ancestors=set(), depth=0)


def _redact(
    value: object, *, state: _State, ancestors: set[int], depth: int
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value)
    if isinstance(value, bytes):
        decoded = value[:_MAX_TEXT].decode("utf-8", errors="replace")
        return decoded if len(value) <= _MAX_TEXT else f"{decoded}[TRUNCATED]"
    if depth >= _MAX_DEPTH:
        return "[MAX_DEPTH]"

    if isinstance(value, Mapping):
        identity = id(value)
        if identity in ancestors:
            return "[CYCLE]"
        ancestors.add(identity)
        result: dict[str, Any] = {}
        try:
            for raw_key, child in value.items():
                if state.remaining <= 0:
                    result["[TRUNCATED]"] = "[TRUNCATED]"
                    break
                state.remaining -= 1
                if not isinstance(raw_key, str):
                    key = f"[UNSAFE_KEY:{_type_name(raw_key)}]"
                else:
                    key = _bounded_text(raw_key)
                result[key] = (
                    REDACTED
                    if _sensitive(key)
                    else _redact(
                        child,
                        state=state,
                        ancestors=ancestors,
                        depth=depth + 1,
                    )
                )
        except Exception:
            return f"[UNSAFE:{_type_name(value)}]"
        finally:
            ancestors.remove(identity)
        return result

    if isinstance(value, Sequence):
        identity = id(value)
        if identity in ancestors:
            return "[CYCLE]"
        ancestors.add(identity)
        result_list: list[Any] = []
        try:
            for child in value:
                if state.remaining <= 0:
                    result_list.append("[TRUNCATED]")
                    break
                state.remaining -= 1
                result_list.append(
                    _redact(
                        child,
                        state=state,
                        ancestors=ancestors,
                        depth=depth + 1,
                    )
                )
        except Exception:
            return f"[UNSAFE:{_type_name(value)}]"
        finally:
            ancestors.remove(identity)
        return result_list

    return f"[UNSAFE:{_type_name(value)}]"
