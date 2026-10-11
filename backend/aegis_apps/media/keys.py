"""Source fingerprints, derivative keys and derivative paths (spec M1).

All digests are SHA-256 and are written as 64 lowercase hexadecimal characters.
Every input is encoded at a fixed width or with an explicit length, and every digest
starts with its own NUL-terminated domain tag, so no two different inputs can share an
encoding.

Fingerprint (99 input bytes)::

    offset  size  field
    0       27    b"aegis.media.fingerprint.v1\\x00"
    27      16    root_id, UUID.bytes (RFC 4122 network byte order)
    43      16    entry_id, UUID.bytes
    59       8    device, unsigned 64-bit big-endian
    67       8    inode, unsigned 64-bit big-endian
    75       8    size, unsigned 64-bit big-endian
    83      16    mtime_ns, signed 128-bit big-endian two's complement

``mtime_ns`` is 128-bit because file systems store timestamps that overflow a signed
64-bit nanosecond count (after 2262 or before 1677); the catalog keeps up to 30 digits.

Parameter digest (variable length)::

    b"aegis.media.params.v1\\x00"         (22 bytes)
    canonical JSON of the parameters: an object of at most 32 keys matching
    ^[a-z][A-Za-z0-9]{0,31}$, whose values are booleans, integers in the signed
    64-bit range, or printable ASCII strings of at most 64 characters, encoded with
    sorted keys, no insignificant whitespace and ASCII escapes; at most 1,024 bytes

Derivative key (variable length, at most 165 input bytes)::

    b"aegis.media.derivative.v1\\x00"         (26 bytes)
    fingerprint, the 32 raw digest bytes
    len(processor_version), 1 byte, then processor_version as ASCII (1-64 bytes,
        matching ^[A-Za-z0-9][A-Za-z0-9._+/-]{0,63}$)
    len(kind), 1 byte, then kind as ASCII (the DerivativeKind value)
    params_digest, the 32 raw digest bytes

Derivative path, relative to the derivatives directory::

    <key[0:2]>/<key[2:4]>/<key>.<extension>
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from decimal import Decimal
from typing import Final

from .enums import DerivativeExtension, DerivativeKind

FINGERPRINT_DOMAIN: Final = b"aegis.media.fingerprint.v1\x00"
PARAMS_DOMAIN: Final = b"aegis.media.params.v1\x00"
DERIVATIVE_KEY_DOMAIN: Final = b"aegis.media.derivative.v1\x00"

UINT64_MAX: Final = (1 << 64) - 1
INT128_MIN: Final = -(1 << 127)
INT128_MAX: Final = (1 << 127) - 1
INT64_MIN: Final = -(1 << 63)
INT64_MAX: Final = (1 << 63) - 1

MAX_PARAMS: Final = 32
MAX_PARAMS_BYTES: Final = 1024
MAX_PARAM_STRING: Final = 64

HEX_DIGEST_PATTERN: Final = r"^[0-9a-f]{64}$"
# Such as "aegis-media-processor/1"; bumped whenever an operation's outputs change.
PROCESSOR_VERSION_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,63}$"

_HEX_DIGEST_RE: Final = re.compile(r"[0-9a-f]{64}")
_PROCESSOR_VERSION_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]{0,63}")
_PARAM_NAME_RE: Final = re.compile(r"[a-z][A-Za-z0-9]{0,31}")
_PARAM_STRING_RE: Final = re.compile(r"[\x20-\x7e]{0,64}")


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid {name}")
    if isinstance(value, int):
        return value
    if (
        isinstance(value, Decimal)
        and value.is_finite()
        and value == value.to_integral_value()
    ):
        return int(value)
    raise ValueError(f"invalid {name}")


def _uint64(value: object, name: str) -> bytes:
    number = _integer(value, name)
    if not 0 <= number <= UINT64_MAX:
        raise ValueError(f"invalid {name}")
    return number.to_bytes(8, "big")


def _int128(value: object, name: str) -> bytes:
    number = _integer(value, name)
    if not INT128_MIN <= number <= INT128_MAX:
        raise ValueError(f"invalid {name}")
    return number.to_bytes(16, "big", signed=True)


def _uuid(value: object, name: str) -> bytes:
    if not isinstance(value, uuid.UUID):
        raise ValueError(f"invalid {name}")
    return value.bytes


def _digest_bytes(value: object, name: str) -> bytes:
    if not isinstance(value, str) or _HEX_DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")
    return bytes.fromhex(value)


def is_hex_digest(value: object) -> bool:
    return isinstance(value, str) and _HEX_DIGEST_RE.fullmatch(value) is not None


def fingerprint(
    root_id: uuid.UUID,
    entry_id: uuid.UUID,
    device: int | Decimal,
    inode: int | Decimal,
    size: int | Decimal,
    mtime_ns: int | Decimal,
) -> str:
    """Return the source fingerprint of one catalog file observation."""
    encoded = b"".join(
        (
            FINGERPRINT_DOMAIN,
            _uuid(root_id, "root ID"),
            _uuid(entry_id, "entry ID"),
            _uint64(device, "device"),
            _uint64(inode, "inode"),
            _uint64(size, "size"),
            _int128(mtime_ns, "modification time"),
        )
    )
    return hashlib.sha256(encoded).hexdigest()


def canonical_params(params: Mapping[str, object]) -> bytes:
    """Return the canonical JSON encoding of derivative parameters."""
    if not isinstance(params, Mapping) or len(params) > MAX_PARAMS:
        raise ValueError("invalid derivative parameters")
    normalized: dict[str, bool | int | str] = {}
    for name, value in params.items():
        if not isinstance(name, str) or _PARAM_NAME_RE.fullmatch(name) is None:
            raise ValueError("invalid derivative parameters")
        if isinstance(value, bool):
            normalized[name] = value
        elif isinstance(value, int):
            if not INT64_MIN <= value <= INT64_MAX:
                raise ValueError("invalid derivative parameters")
            normalized[name] = value
        elif isinstance(value, str) and _PARAM_STRING_RE.fullmatch(value) is not None:
            normalized[name] = value
        else:
            raise ValueError("invalid derivative parameters")
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if len(encoded) > MAX_PARAMS_BYTES:
        raise ValueError("invalid derivative parameters")
    return encoded


def params_digest(params: Mapping[str, object]) -> str:
    """Return the digest that stands for a derivative's parameters in its key."""
    return hashlib.sha256(PARAMS_DOMAIN + canonical_params(params)).hexdigest()


def derivative_key(
    source_fingerprint: str,
    processor_version: str,
    kind: DerivativeKind | str,
    parameters_digest: str,
) -> str:
    """Return the key of the derivative of *kind* built from one source fingerprint."""
    if (
        not isinstance(processor_version, str)
        or _PROCESSOR_VERSION_RE.fullmatch(processor_version) is None
    ):
        raise ValueError("invalid processor version")
    if not isinstance(kind, str) or kind not in DerivativeKind.values:
        raise ValueError("invalid derivative kind")
    version = processor_version.encode("ascii")
    kind_bytes = str(kind).encode("ascii")
    encoded = b"".join(
        (
            DERIVATIVE_KEY_DOMAIN,
            _digest_bytes(source_fingerprint, "fingerprint"),
            len(version).to_bytes(1, "big"),
            version,
            len(kind_bytes).to_bytes(1, "big"),
            kind_bytes,
            _digest_bytes(parameters_digest, "parameter digest"),
        )
    )
    return hashlib.sha256(encoded).hexdigest()


def relative_path(key: str, extension: DerivativeExtension | str) -> str:
    """Return the derivative's path relative to the derivatives directory."""
    if not is_hex_digest(key):
        raise ValueError("invalid derivative key")
    if not isinstance(extension, str) or extension not in DerivativeExtension.values:
        raise ValueError("invalid derivative extension")
    return f"{key[0:2]}/{key[2:4]}/{key}.{extension}"
