"""Internal redirect targets for the gateway's protected aliases (spec M6).

nginx handles an ``X-Accel-Redirect`` value with ``ngx_http_parse_unsafe_uri``: a literal
``?`` starts the arguments, the rest is percent-decoded exactly once, and a decoded NUL
or ``..`` segment is refused. The decoded URI is then mapped through the internal
location's ``alias`` without any further decoding, so the bytes reaching ``open(2)`` are
exactly the decoded bytes.

Each raw name byte outside ``[A-Za-z0-9._~-]`` is therefore percent-encoded (uppercase
hexadecimal), and ``/`` separates names. A raw ``?``, ``#``, ``%``, space or non-UTF-8 byte
always travels encoded, so nginx's single decode restores the exact raw bytes. Every
emitted URI is checked against a strict grammar, and ``..``, ``.`` and empty segments are
refused even though the catalog never stores them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final
from urllib.parse import unquote_to_bytes

from .keys import relative_path

ORIGINALS_ALIAS: Final = "/__aegis_roots/"
DERIVATIVES_ALIAS: Final = "/__aegis_derivatives/"
# Bounded so the upstream response headers always fit the gateway's proxy buffer.
MAX_REDIRECT_URI_BYTES: Final = 8192
MAX_PATH_DEPTH: Final = 256
MAX_NAME_BYTES: Final = 255

_UNRESERVED: Final = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._~-"
)
_SLOT_ID_RE: Final = re.compile(r"[a-z][a-z0-9-]{0,62}")
_SEGMENT: Final = r"(?:[A-Za-z0-9._~-]|%[0-9A-F]{2})+"
_ORIGINAL_URI_RE: Final = re.compile(
    rf"/__aegis_roots/[a-z][a-z0-9-]{{0,62}}(?:/{_SEGMENT})+", re.ASCII
)
_DERIVATIVE_URI_RE: Final = re.compile(
    r"/__aegis_derivatives/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.(?:webp|mp4)",
    re.ASCII,
)


class UnsafeRedirect(ValueError):
    def __init__(self) -> None:
        super().__init__("unsafe internal redirect")


def encode_name(raw: bytes) -> str:
    """Percent-encode one raw path name for a single nginx decode."""
    if (
        type(raw) is not bytes
        or not 1 <= len(raw) <= MAX_NAME_BYTES
        or raw in (b".", b"..")
        or b"/" in raw
        or b"\0" in raw
    ):
        raise UnsafeRedirect()
    return "".join(chr(byte) if byte in _UNRESERVED else f"%{byte:02X}" for byte in raw)


def _require_safe_segments(segments: Sequence[str]) -> None:
    for segment in segments:
        decoded = unquote_to_bytes(segment)
        if decoded in (b"", b".", b"..") or b"/" in decoded or b"\0" in decoded:
            raise UnsafeRedirect()


def original_redirect(slot_id: str, names: Sequence[bytes]) -> str:
    """The ``/__aegis_roots/<slot>/<encoded path>`` target of one original file."""
    if not isinstance(slot_id, str) or _SLOT_ID_RE.fullmatch(slot_id) is None:
        raise UnsafeRedirect()
    if isinstance(names, (bytes, str)) or not 1 <= len(names) <= MAX_PATH_DEPTH:
        raise UnsafeRedirect()
    encoded = [encode_name(name) for name in names]
    uri = ORIGINALS_ALIAS + slot_id + "/" + "/".join(encoded)
    if len(uri) > MAX_REDIRECT_URI_BYTES or _ORIGINAL_URI_RE.fullmatch(uri) is None:
        raise UnsafeRedirect()
    _require_safe_segments(uri[len(ORIGINALS_ALIAS):].split("/"))
    return uri


def derivative_redirect(key: str, extension: str) -> str:
    """The ``/__aegis_derivatives/<aa>/<bb>/<key>.<ext>`` target of one derivative."""
    try:
        uri = DERIVATIVES_ALIAS + relative_path(key, extension)
    except ValueError:
        raise UnsafeRedirect() from None
    match = _DERIVATIVE_URI_RE.fullmatch(uri)
    if match is None or match[1] != match[3][0:2] or match[2] != match[3][2:4]:
        raise UnsafeRedirect()
    return uri
