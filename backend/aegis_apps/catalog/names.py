"""Lossless child-name identity and versioned, locale-independent ordering."""

import unicodedata

from .domain import SourceName

SORT_KEY_VERSION = 1


def source_name(raw: bytes) -> SourceName:
    if type(raw) is not bytes or not 1 <= len(raw) <= 255:
        raise ValueError("invalid source name")
    if raw in (b".", b"..") or b"/" in raw or b"\0" in raw:
        raise ValueError("invalid source name")
    decoded = raw.decode("utf-8", "surrogateescape")
    display = "".join(
        f"\\x{ord(c) - 0xDC00:02x}" if 0xDC80 <= ord(c) <= 0xDCFF else
        "\\\\" if c == "\\" else
        (f"\\U{ord(c):08x}" if ord(c) > 0xFFFF else f"\\u{ord(c):04x}")
        if unicodedata.category(c).startswith("C") else c
        for c in decoded
    )
    key = unicodedata.normalize("NFC", display.casefold()).encode("utf-8")
    # Supported components expand at most 6:1 (1,530 bytes). Keep a defensive
    # stored-key ceiling below the index-tuple budget; never truncate identity/order.
    if len(key) > 2048:
        raise ValueError("invalid source name")
    suffix = raw.rpartition(b".")[2].lower() if b"." in raw else b""
    hint = (
        suffix.decode("ascii")
        if 1 <= len(suffix) <= 16 and suffix.isascii() and suffix.isalnum() else None
    )
    return SourceName(raw, display, key, hint)
