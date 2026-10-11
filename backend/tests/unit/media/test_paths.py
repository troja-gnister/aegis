from __future__ import annotations

from urllib.parse import unquote_to_bytes

import pytest
from aegis_apps.media.paths import (
    MAX_PATH_DEPTH,
    MAX_REDIRECT_URI_BYTES,
    UnsafeRedirect,
    derivative_redirect,
    encode_name,
    original_redirect,
)

KEY = "0f" + "1e" + "a" * 60


@pytest.mark.parametrize(
    ("raw", "encoded"),
    [
        (b"IMG_0001.JPG", "IMG_0001.JPG"),
        (b"a-b_c.d~e", "a-b_c.d~e"),
        (b"space name.jpg", "space%20name.jpg"),
        (b"100%.jpg", "100%25.jpg"),
        (b"what?.jpg", "what%3F.jpg"),
        (b"hash#tag.jpg", "hash%23tag.jpg"),
        (b"plus+sign.jpg", "plus%2Bsign.jpg"),
        (b"inner%3F.jpg", "inner%253F.jpg"),
        (b"semi;colon,comma=equals&amp.jpg", "semi%3Bcolon%2Ccomma%3Dequals%26amp.jpg"),
        (b"quote\"back\\slash'.jpg", "quote%22back%5Cslash%27.jpg"),
        (b"latin\xe9\xff.jpg", "latin%E9%FF.jpg"),
        ("caf\u00e9 \u6771\u4eac.jpg".encode(), "caf%C3%A9%20%E6%9D%B1%E4%BA%AC.jpg"),
        (b"tab\tnew\nline\r.jpg", "tab%09new%0Aline%0D.jpg"),
        (b"\x7f\x80.jpg", "%7F%80.jpg"),
        (b"...", "..."),
        (b".hidden", ".hidden"),
    ],
)
def test_names_are_percent_encoded_outside_unreserved_bytes(raw: bytes, encoded: str) -> None:
    assert encode_name(raw) == encoded
    # nginx decodes the redirect exactly once; that restores the raw bytes.
    assert unquote_to_bytes(encoded) == raw


def test_every_byte_round_trips_and_only_unreserved_bytes_stay_literal() -> None:
    for byte in range(1, 256):
        if byte == ord("/"):
            continue
        raw = bytes([byte]) + b"x"
        encoded = encode_name(raw)
        assert unquote_to_bytes(encoded) == raw
        literal = chr(byte).isascii() and (chr(byte).isalnum() or chr(byte) in "._~-")
        assert (encoded[0] != "%") is literal
        assert encoded.isascii() and encoded.isprintable()


@pytest.mark.parametrize(
    "raw",
    [b"", b".", b"..", b"a/b", b"nul\x00byte", b"x" * 256, "text", None],
)
def test_unsafe_names_are_refused(raw: object) -> None:
    with pytest.raises(UnsafeRedirect):
        encode_name(raw)  # type: ignore[arg-type]


def test_original_redirect_joins_encoded_names_below_the_slot() -> None:
    names = (b"Photos 2024", b"Trip?", "\u00e9t\u00e9 #1.jpg".encode())

    assert original_redirect("family-photos", names) == (
        "/__aegis_roots/family-photos/Photos%202024/Trip%3F/%C3%A9t%C3%A9%20%231.jpg"
    )


@pytest.mark.parametrize(
    ("slot", "names"),
    [
        ("Family", (b"a.jpg",)),
        ("1slot", (b"a.jpg",)),
        ("slot/../x", (b"a.jpg",)),
        ("slot", ()),
        ("slot", (b"..", b"a.jpg")),
        ("slot", (b"a", b".", b"b.jpg")),
        ("slot", (b"a", b"", b"b.jpg")),
        ("slot", (b"a/b.jpg",)),
        ("slot", b"a.jpg"),
        ("slot", (b"d",) * (MAX_PATH_DEPTH + 1)),
        ("slot", (b"\xe9" * 255,) * 11),
    ],
)
def test_unsafe_or_oversized_original_redirects_are_refused(
    slot: str, names: object,
) -> None:
    with pytest.raises(UnsafeRedirect):
        original_redirect(slot, names)  # type: ignore[arg-type]


def test_original_redirect_stays_within_the_header_bound() -> None:
    names = (b"\xe9" * 240,) * 11 + (b"long.bin",)
    uri = original_redirect("gateway-test", names)

    assert 7000 < len(uri) <= MAX_REDIRECT_URI_BYTES
    assert uri.startswith("/__aegis_roots/gateway-test/%E9")


def test_derivative_redirect_is_the_key_fan_out() -> None:
    assert derivative_redirect(KEY, "webp") == f"/__aegis_derivatives/0f/1e/{KEY}.webp"
    assert derivative_redirect(KEY, "mp4") == f"/__aegis_derivatives/0f/1e/{KEY}.mp4"


@pytest.mark.parametrize(
    ("key", "extension"),
    [
        (KEY.upper(), "webp"),
        (KEY[:-1], "webp"),
        ("../" + KEY[3:], "webp"),
        (KEY, "html"),
        (KEY, "WEBP"),
        (KEY, ""),
    ],
)
def test_malformed_derivative_redirects_are_refused(key: str, extension: str) -> None:
    with pytest.raises(UnsafeRedirect):
        derivative_redirect(key, extension)
