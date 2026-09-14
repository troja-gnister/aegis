from __future__ import annotations

import unicodedata

import pytest
from aegis_apps.catalog.names import source_name
from hypothesis import given
from hypothesis import strategies as st


@pytest.mark.parametrize("raw", [b"", b".", b"..", b"a/b", b"a\x00b", b"a" * 256])
def test_invalid_child_names_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError, match="invalid source name"):
        source_name(raw)


@pytest.mark.parametrize("raw", ["name", bytearray(b"name"), None, 42])
def test_only_bytes_are_accepted(raw: object) -> None:
    with pytest.raises(ValueError, match="invalid source name"):
        source_name(raw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "display", "hint"),
    [
        (b"photo-\xff.jpg", "photo-\\xff.jpg", "jpg"),
        (b"literal\\xff.TXT", "literal\\\\xff.TXT", "txt"),
        (b"line\n\t\x7f", "line\\u000a\\u0009\\u007f", None),
        ("a\u202eb\u200d".encode(), "a\\u202eb\\u200d", None),
        (b"<script>alert(1)</script>", None, None),
        (b"<img onerror=alert(1)>", "<img onerror=alert(1)>", None),
        (b".hidden", ".hidden", "hidden"),
        (b"file.", "file.", None),
        (b"file.a-b", "file.a-b", None),
        ("file.\u00e9".encode(), "file.\u00e9", None),
        (b"file.1234567890123456", "file.1234567890123456", "1234567890123456"),
        (b"file.12345678901234567", "file.12345678901234567", None),
    ],
)
def test_display_is_inert_plain_text_and_hint_is_ascii_only(
    raw: bytes, display: str | None, hint: str | None,
) -> None:
    if display is None:
        with pytest.raises(ValueError, match="invalid source name"):
            source_name(raw)
        return
    name = source_name(raw)
    assert name.raw == raw
    assert name.display == display
    assert name.type_hint == hint


@pytest.mark.parametrize(
    ("left", "right", "key"),
    [
        ("É.TXT", "e\u0301.txt", "é.txt"),
        ("Straße", "STRASSE", "strasse"),
        ("\u03a3", "\u03c2", "\u03c3"),
        ("A.txt", "a.txt", "a.txt"),
    ],
)
def test_order_collisions_do_not_collapse_raw_identity(left: str, right: str, key: str) -> None:
    first, second = source_name(left.encode()), source_name(right.encode())
    assert first.raw != second.raw
    assert first.order_key == second.order_key == key.encode()


components = st.binary(min_size=1, max_size=255).filter(
    lambda raw: b"\0" not in raw and b"/" not in raw and raw not in (b".", b"..")
)


@given(components)
def test_supported_byte_components_preserve_identity_with_safe_bounded_display(raw: bytes) -> None:
    name = source_name(raw)
    assert name.raw == raw
    assert all(not unicodedata.category(character).startswith("C") for character in name.display)
    assert name.order_key.decode("utf-8")
    assert len(name.order_key) <= 6 * len(raw) <= 1530


@pytest.mark.parametrize(
    ("raw", "length"),
    [
        (b"a" * 255, 255),
        (b"\xff" * 255, 1020),
        (b"\x01" * 255, 1530),
        (b"\\" * 255, 510),
        ("\u0130".encode() * 127 + b"a", 382),
    ],
)
def test_maximum_linux_components_fit_the_stored_key_budget(raw: bytes, length: int) -> None:
    assert len(raw) == 255
    assert len(source_name(raw).order_key) == length


def test_unicode_expansion_bound_covers_every_supported_scalar() -> None:
    # Canonical decomposition bounds NFC after concatenation, including combining boundaries.
    # Invalid bytes expand 4:1, backslashes 2:1, and controls at most 6:1.
    for point in range(0x110000):
        if 0xD800 <= point <= 0xDFFF:
            continue
        character = chr(point)
        if unicodedata.category(character).startswith("C") or character in ("/", "\\", "."):
            continue
        expanded = unicodedata.normalize("NFD", character.casefold()).encode()
        assert len(expanded) <= 6 * len(character.encode()), hex(point)
