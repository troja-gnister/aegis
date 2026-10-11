"""RFC 6266 ``Content-Disposition`` for original delivery.

The header always carries both forms: a quoted ASCII ``filename`` fallback, and an RFC 8187
``filename*`` with the UTF-8 name percent-encoded. Raw names may be any bytes except NUL
and ``/``; invalid UTF-8 becomes U+FFFD and every control, format, surrogate or private-use
character becomes ``_``, so neither form can carry a separator, a quote or a line break.
"""

from __future__ import annotations

import unicodedata
from typing import Final, Literal

FALLBACK_NAME: Final = "download"
# Controls, format characters (bidi overrides), surrogates, private use, unassigned, and
# line or paragraph separators.
_UNSAFE_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_ATTR_CHAR: Final = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$&+-.^_`|~"
)


def display_filename(raw: bytes) -> str:
    """A safe Unicode file name for a raw name: no controls, separators or quotes."""
    if type(raw) is not bytes or not raw:
        return FALLBACK_NAME
    decoded = raw.decode("utf-8", "replace")
    name = "".join(
        "_" if unicodedata.category(c) in _UNSAFE_CATEGORIES or c in '/\\"' else c
        for c in decoded
    ).strip()
    return name if name and name not in (".", "..") else FALLBACK_NAME


def _ascii_fallback(name: str) -> str:
    # Printable ASCII only; "%" is replaced too, because some clients percent-decode
    # the plain parameter.
    fallback = "".join(c if " " <= c <= "~" and c not in '"\\%' else "_" for c in name)
    return fallback.strip() or FALLBACK_NAME


def _rfc8187(name: str) -> str:
    return "".join(
        chr(byte) if byte in _ATTR_CHAR else f"%{byte:02X}" for byte in name.encode("utf-8")
    )


def content_disposition(disposition: Literal["inline", "attachment"], raw: bytes) -> str:
    if disposition not in ("inline", "attachment"):
        raise ValueError("invalid disposition")
    name = display_filename(raw)
    return (
        f'{disposition}; filename="{_ascii_fallback(name)}"; '
        f"filename*=UTF-8''{_rfc8187(name)}"
    )
