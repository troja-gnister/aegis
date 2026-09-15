from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast
from uuid import UUID

import pytest
from aegis_apps.catalog.cursors import (
    CURSOR_MAX_AGE,
    CURSOR_SALT,
    CursorContext,
    CursorKey,
    CursorRestartRequired,
    decode_cursor,
    encode_cursor,
)
from aegis_apps.catalog.filters import canonical_filter_bytes, parse_filters
from django.core import signing
from hypothesis import given
from hypothesis import strategies as st

ENTRY_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")


def make_context(**changes: object) -> CursorContext:
    values: dict[str, object] = {
        "user_id": UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        "cache_namespace": "c" * 64,
        "user_epoch": 3,
        "root_id": UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        "root_epoch": 5,
        "parent_id": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
        "parent_revision": 7,
        "filter_digest": hashlib.sha256(
            canonical_filter_bytes(parse_filters({"v": 1, "kind": ["file"]}))
        ).hexdigest(),
        "sort": "name",
        "order": "asc",
        "limit": 100,
    }
    values.update(changes)
    return CursorContext(**values)  # type: ignore[arg-type]


def unsigned(value: str) -> dict[str, Any]:
    payload = signing.loads(value, salt=CURSOR_SALT)
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def resigned(payload: object, *, compress: bool = False) -> str:
    return signing.dumps(payload, salt=CURSOR_SALT, compress=compress)


def test_cursor_round_trips_complete_name_boundary_and_uuid() -> None:
    context = make_context()
    key = (0, 0, b"complete-name-key", b"complete-name-key", ENTRY_ID)
    token = encode_cursor(context, key, "next")
    position = decode_cursor(token, context)
    assert position.key == key
    assert position.travel == "next"


@given(st.binary(min_size=1, max_size=2048))
def test_name_cursor_base64_round_trip_preserves_every_key_byte(name_key: bytes) -> None:
    context = make_context()
    key = (1, 0, name_key, name_key, ENTRY_ID)
    assert decode_cursor(encode_cursor(context, key, "next"), context).key == key


@pytest.mark.parametrize(
    ("sort", "key"),
    [
        ("name", (1, 0, b"name", b"name", ENTRY_ID)),
        ("size", (1, 0, 2**64 - 1, b"name", ENTRY_ID)),
        ("size", (1, 1, 0, b"name", ENTRY_ID)),
        ("modified", (1, 0, -(2**63), b"name", ENTRY_ID)),
        ("modified", (1, 0, 2**63 - 1, b"name", ENTRY_ID)),
        ("modified", (1, 1, 0, b"name", ENTRY_ID)),
    ],
)
def test_cursor_round_trips_every_sort_value_and_numeric_null_sentinel(
    sort: str, key: tuple[object, ...]
) -> None:
    context = make_context(sort=sort)
    typed_key = cast(CursorKey, key)
    assert decode_cursor(encode_cursor(context, typed_key, "previous"), context).key == key


def test_cursor_preserves_maximum_complete_name_key() -> None:
    key = b"n" * 2048
    context = make_context()
    token = encode_cursor(context, (1, 0, key, key, ENTRY_ID), "next")
    assert token.isascii()
    assert len(token.encode("ascii")) <= 8192
    position = decode_cursor(token, context)
    assert position.key[2] == key
    assert position.key[3] == key


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("user_id", UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")),
        ("cache_namespace", "d" * 64),
        ("user_epoch", 4),
        ("root_id", UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")),
        ("root_epoch", 6),
        ("parent_id", UUID("11111111-1111-4111-8111-111111111111")),
        ("parent_revision", 8),
        ("filter_digest", "f" * 64),
        ("sort", "modified"),
        ("order", "desc"),
        ("limit", 101),
    ],
)
def test_every_context_field_invalidates_a_cursor(field: str, changed: object) -> None:
    context = make_context()
    token = encode_cursor(context, (0, 0, b"name", b"name", ENTRY_ID), "next")
    with pytest.raises(CursorRestartRequired, match=r"^cursor_restart_required$"):
        decode_cursor(token, replace(context, **{field: changed}))  # type: ignore[arg-type]


def test_equivalent_filters_reuse_context_but_changed_filters_do_not() -> None:
    first = parse_filters({"v": 1, "kind": ["file", "directory", "file"]})
    second = parse_filters({"v": 1, "kind": ["directory", "file"]})
    changed = parse_filters({"v": 1, "kind": ["file"]})
    first_digest = hashlib.sha256(canonical_filter_bytes(first)).hexdigest()
    second_digest = hashlib.sha256(canonical_filter_bytes(second)).hexdigest()
    changed_digest = hashlib.sha256(canonical_filter_bytes(changed)).hexdigest()
    assert first_digest == second_digest

    context = make_context(filter_digest=first_digest)
    token = encode_cursor(context, (0, 0, b"name", b"name", ENTRY_ID), "next")
    assert decode_cursor(token, replace(context, filter_digest=second_digest))
    with pytest.raises(CursorRestartRequired):
        decode_cursor(token, replace(context, filter_digest=changed_digest))


def test_context_values_are_digested_and_plaintext_namespace_is_not_in_payload() -> None:
    context = make_context(cache_namespace="plaintext-session-key")
    token = encode_cursor(context, (0, 0, b"name", b"name", ENTRY_ID), "next")
    assert "plaintext-session-key" not in token
    assert "plaintext-session-key" not in repr(unsigned(token))


def test_signature_tampering_is_a_safe_restartable_error() -> None:
    context = make_context()
    token = encode_cursor(context, (0, 0, b"name", b"name", ENTRY_ID), "next")
    replacement = "A" if token[-1] != "A" else "B"
    with pytest.raises(CursorRestartRequired, match=r"^cursor_restart_required$"):
        decode_cursor(token[:-1] + replacement, context)


@pytest.mark.parametrize("value", [None, b"cursor", 3, "x" * 8193, ".compressed"])
def test_invalid_encoded_cursor_is_a_safe_restartable_error(value: object) -> None:
    with pytest.raises(CursorRestartRequired, match=r"^cursor_restart_required$"):
        decode_cursor(value, make_context())  # type: ignore[arg-type]


def test_multibyte_cursor_is_rejected_before_signing_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unexpected_decode(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("signing.loads must not receive a non-ASCII cursor")

    monkeypatch.setattr(signing, "loads", unexpected_decode)
    value = "\U0001f642" * 2049
    assert len(value) < 8192
    assert len(value.encode()) > 8192
    with pytest.raises(CursorRestartRequired, match=r"^cursor_restart_required$"):
        decode_cursor(value, make_context())
    assert calls == 0


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: {**payload, "v": 2},
        lambda payload: {**payload, "sv": 2},
        lambda payload: {**payload, "travel": "sideways"},
        lambda payload: {**payload, "travel": 1},
        lambda payload: {**payload, "extra": True},
        lambda payload: {key: value for key, value in payload.items() if key != "context"},
        lambda payload: {**payload, "context": 1},
        lambda payload: {**payload, "key": []},
        lambda payload: {**payload, "key": payload["key"][:-1]},
        lambda payload: {**payload, "key": [True, *payload["key"][1:]]},
        lambda payload: {**payload, "key": [2, *payload["key"][1:]]},
        lambda payload: {**payload, "key": [payload["key"][0], 2, *payload["key"][2:]]},
        lambda payload: {
            **payload,
            "key": [*payload["key"][:3], "not-base64!", payload["key"][4]],
        },
        lambda payload: {**payload, "key": [*payload["key"][:4], "NOT-A-UUID"]},
    ],
)
def test_correctly_signed_malformed_payloads_are_restartable(
    mutate: Callable[[dict[str, Any]], object],
) -> None:
    context = make_context()
    token = encode_cursor(context, (0, 0, b"name", b"name", ENTRY_ID), "next")
    payload = unsigned(token)
    malformed = mutate(payload)
    with pytest.raises(CursorRestartRequired, match=r"^cursor_restart_required$"):
        decode_cursor(resigned(malformed), context)


@pytest.mark.parametrize(
    ("sort", "key"),
    [
        ("name", (0, 1, b"name", b"name", ENTRY_ID)),
        ("name", (0, 0, b"other", b"name", ENTRY_ID)),
        ("name", (0, 0, "name", b"name", ENTRY_ID)),
        ("size", (1, 0, -1, b"name", ENTRY_ID)),
        ("size", (1, 0, 2**64, b"name", ENTRY_ID)),
        ("size", (1, 1, 1, b"name", ENTRY_ID)),
        ("size", (1, 0, True, b"name", ENTRY_ID)),
        ("modified", (1, 0, -(2**63) - 1, b"name", ENTRY_ID)),
        ("modified", (1, 0, 2**63, b"name", ENTRY_ID)),
        ("modified", (1, 1, -1, b"name", ENTRY_ID)),
        ("modified", (1, 0, 1.0, b"name", ENTRY_ID)),
        ("modified", (1, 0, float("nan"), b"name", ENTRY_ID)),
        ("modified", (1, 0, 1, "name", ENTRY_ID)),
        ("modified", (1, 0, 1, b"n" * 2049, ENTRY_ID)),
        ("modified", (1, 0, 1, b"name", str(ENTRY_ID))),
    ],
)
def test_sort_specific_tuple_types_and_ranges_are_enforced(
    sort: str, key: tuple[object, ...]
) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        encode_cursor(make_context(sort=sort), key, "next")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"cache_namespace": ""},
        {"user_epoch": True},
        {"user_epoch": -1},
        {"root_epoch": 2**63},
        {"parent_revision": -1},
        {"filter_digest": "not-a-digest"},
        {"sort": "path"},
        {"order": "sideways"},
        {"limit": True},
        {"limit": 0},
        {"limit": 251},
    ],
)
def test_invalid_context_and_page_limits_are_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="invalid cursor"):
        encode_cursor(make_context(**changes), (0, 0, b"name", b"name", ENTRY_ID), "next")


def test_correctly_signed_compressed_object_is_rejected_before_decoding() -> None:
    context = make_context()
    name = b"a" * 2048
    payload = unsigned(encode_cursor(context, (1, 0, name, name, ENTRY_ID), "next"))
    token = resigned(payload, compress=True)
    assert token.startswith(".")
    with pytest.raises(CursorRestartRequired, match=r"^cursor_restart_required$"):
        decode_cursor(token, context)


@pytest.mark.parametrize("payload", [None, [], "object", 1, True])
def test_signed_non_object_payload_is_a_safe_restartable_error(payload: object) -> None:
    with pytest.raises(CursorRestartRequired, match=r"^cursor_restart_required$"):
        decode_cursor(resigned(payload), make_context())


def test_cursor_expiry_accepts_899_seconds_and_rejects_901(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = make_context()
    key = (0, 0, b"name", b"name", ENTRY_ID)
    monkeypatch.setattr("django.core.signing.time.time", lambda: 10_000.0)
    token = encode_cursor(context, key, "next")

    monkeypatch.setattr(
        "django.core.signing.time.time", lambda: 10_000.0 + CURSOR_MAX_AGE - 1
    )
    assert decode_cursor(token, context).key == key

    monkeypatch.setattr(
        "django.core.signing.time.time", lambda: 10_000.0 + CURSOR_MAX_AGE + 1
    )
    with pytest.raises(CursorRestartRequired, match=r"^cursor_restart_required$"):
        decode_cursor(token, context)
