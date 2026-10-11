from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from aegis_apps.catalog.cursors import CURSOR_SALT, CursorRestartRequired
from aegis_apps.media.timeline import (
    MAX_TIMELINE_CURSOR_BYTES,
    TIMELINE_CURSOR_MAX_AGE,
    TIMELINE_CURSOR_SALT,
    TimelineCursorContext,
    decode_timeline_cursor,
    encode_timeline_cursor,
    sort_key_datetime,
    sort_key_microseconds,
)
from django.core import signing

CONTEXT = TimelineCursorContext(
    user_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
    cache_namespace="a" * 40,
    user_epoch=3,
    root_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
    root_epoch=5,
    kind="all",
    year=None,
    limit=60,
)
ENTRY = uuid.UUID("33333333-3333-4333-8333-333333333333")
MOMENT = datetime(2024, 5, 1, 10, 30, 0, 123456, tzinfo=UTC)


def _key() -> tuple[int, uuid.UUID]:
    return sort_key_microseconds(MOMENT), ENTRY


def test_cursor_round_trips_the_keyset_boundary() -> None:
    token = encode_timeline_cursor(CONTEXT, _key())

    assert len(token) <= MAX_TIMELINE_CURSOR_BYTES
    assert decode_timeline_cursor(token, CONTEXT) == _key()
    assert sort_key_datetime(_key()[0]) == MOMENT


def test_salt_is_dedicated() -> None:
    assert TIMELINE_CURSOR_SALT == "aegis.media.timeline.v1"
    assert TIMELINE_CURSOR_SALT != CURSOR_SALT
    assert TIMELINE_CURSOR_MAX_AGE == 900


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": uuid.UUID("44444444-4444-4444-8444-444444444444")},
        {"cache_namespace": "b" * 40},
        {"user_epoch": 4},
        {"root_id": uuid.UUID("55555555-5555-4555-8555-555555555555")},
        {"root_epoch": 6},
        {"kind": "photo"},
        {"year": 2024},
        {"limit": 61},
    ],
)
def test_cursor_is_bound_to_every_context_field(change: dict[str, Any]) -> None:
    token = encode_timeline_cursor(CONTEXT, _key())

    with pytest.raises(CursorRestartRequired):
        decode_timeline_cursor(token, dataclasses.replace(CONTEXT, **change))


def _flipped_last_character() -> str:
    token = encode_timeline_cursor(CONTEXT, _key())
    return token[:-1] + ("B" if token[-1] == "A" else "A")


def _signed(payload: object, *, salt: str = TIMELINE_CURSOR_SALT) -> str:
    return signing.dumps(payload, salt=salt, compress=False)


def _payload(**overrides: object) -> dict[str, object]:
    token = encode_timeline_cursor(CONTEXT, _key())
    payload = signing.loads(token, salt=TIMELINE_CURSOR_SALT)
    assert isinstance(payload, dict)
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "tampered",
    [
        _flipped_last_character,
        lambda: "x" + encode_timeline_cursor(CONTEXT, _key()),
        lambda: _signed(_payload(), salt=CURSOR_SALT),
        lambda: _signed(_payload(v=2)),
        lambda: _signed(_payload(v=True)),
        lambda: _signed(_payload(context="0" * 64)),
        lambda: _signed(_payload(extra=1)),
        lambda: _signed({"v": 1, "context": _payload()["context"]}),
        lambda: _signed(_payload(key=[str(_key()[0])])),
        lambda: _signed(_payload(key=[_key()[0], str(ENTRY)])),
        lambda: _signed(_payload(key=["0" + str(_key()[0]), str(ENTRY)])),
        lambda: _signed(_payload(key=["+" + str(_key()[0]), str(ENTRY)])),
        lambda: _signed(_payload(key=["\u0661\u0662", str(ENTRY)])),
        lambda: _signed(_payload(key=[str(10**20), str(ENTRY)])),
        lambda: _signed(_payload(key=[str(_key()[0]), str(ENTRY).upper()])),
        lambda: _signed(_payload(key=[str(_key()[0]), "not-a-uuid"])),
        lambda: _signed([1, 2, 3]),
        lambda: "",
        lambda: ".starts-with-a-dot",
        lambda: "x" * (MAX_TIMELINE_CURSOR_BYTES + 1),
        lambda: "caf\u00e9",
    ],
)
def test_tampered_or_foreign_cursors_require_a_restart(tampered: Any) -> None:
    with pytest.raises(CursorRestartRequired):
        decode_timeline_cursor(tampered(), CONTEXT)


def test_cursor_expiry_accepts_899_seconds_and_rejects_901() -> None:
    with patch("django.core.signing.time.time", return_value=10_000.0):
        token = encode_timeline_cursor(CONTEXT, _key())
    with patch(
        "django.core.signing.time.time", return_value=10_000.0 + TIMELINE_CURSOR_MAX_AGE - 1,
    ):
        assert decode_timeline_cursor(token, CONTEXT) == _key()
    with (
        patch(
            "django.core.signing.time.time",
            return_value=10_000.0 + TIMELINE_CURSOR_MAX_AGE + 1,
        ),
        pytest.raises(CursorRestartRequired),
    ):
        decode_timeline_cursor(token, CONTEXT)


@pytest.mark.parametrize(
    "key",
    [
        (True, ENTRY),
        ("1", ENTRY),
        (-62_135_596_800_000_001, ENTRY),
        (253_402_300_800_000_000, ENTRY),
        (0, str(ENTRY)),
        [0, ENTRY],
        (0,),
    ],
)
def test_invalid_boundaries_are_never_signed(key: Any) -> None:
    with pytest.raises(ValueError):
        encode_timeline_cursor(CONTEXT, key)


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "audio"},
        {"year": 0},
        {"year": 10_000},
        {"limit": 0},
        {"limit": 121},
        {"user_epoch": -1},
        {"cache_namespace": ""},
    ],
)
def test_invalid_contexts_are_never_signed(change: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        encode_timeline_cursor(dataclasses.replace(CONTEXT, **change), _key())


def test_sort_keys_cover_the_whole_datetime_range() -> None:
    for moment in (
        datetime(1, 1, 1, tzinfo=UTC),
        datetime(1969, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
    ):
        key = (sort_key_microseconds(moment), ENTRY)
        token = encode_timeline_cursor(CONTEXT, key)
        assert sort_key_datetime(decode_timeline_cursor(token, CONTEXT)[0]) == moment
