"""Opaque, expiring browse cursor contracts without authorization side effects."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from django.core import signing

from .names import SORT_KEY_VERSION

CURSOR_VERSION = 1
CURSOR_SALT = "aegis.catalog.cursor.v1"
CURSOR_MAX_AGE = 900
MAX_CURSOR_BYTES = 8192
MAX_NAME_KEY_BYTES = 2048
MAX_EPOCH = 2**63 - 1
MAX_SIZE = 2**64 - 1
MIN_TIMESTAMP_NS = -(2**63)
MAX_TIMESTAMP_NS = 2**63 - 1

type SortName = Literal["name", "modified", "size"]
type SortOrder = Literal["asc", "desc"]
type CursorTravel = Literal["next", "previous"]
type CursorSortValue = bytes | int
type CursorKey = tuple[int, int, CursorSortValue, bytes, UUID]

_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_INTEGER_RE = re.compile(r"(?:0|-?[1-9][0-9]*)\Z", re.ASCII)
_PAYLOAD_FIELDS = {"v", "sv", "context", "key", "travel"}


class CursorRestartRequired(ValueError):
    def __init__(self) -> None:
        super().__init__("cursor_restart_required")


@dataclass(frozen=True, slots=True)
class CursorContext:
    user_id: UUID
    cache_namespace: str
    user_epoch: int
    root_id: UUID
    root_epoch: int
    parent_id: UUID
    parent_revision: int
    filter_digest: str
    sort: SortName
    order: SortOrder
    limit: int


@dataclass(frozen=True, slots=True)
class CursorPosition:
    key: CursorKey
    travel: CursorTravel


def _invalid() -> ValueError:
    return ValueError("invalid cursor")


def _bounded_integer(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def _context_fields(context: CursorContext) -> dict[str, object]:
    if type(context) is not CursorContext:
        raise _invalid()
    uuid_fields = (context.user_id, context.root_id, context.parent_id)
    if any(type(value) is not UUID for value in uuid_fields):
        raise _invalid()
    try:
        namespace_bytes = context.cache_namespace.encode("utf-8")
    except (AttributeError, UnicodeEncodeError):
        raise _invalid() from None
    if not 1 <= len(namespace_bytes) <= 256:
        raise _invalid()
    if not all(
        _bounded_integer(value, 0, MAX_EPOCH)
        for value in (context.user_epoch, context.root_epoch, context.parent_revision)
    ):
        raise _invalid()
    if (
        type(context.filter_digest) is not str
        or _DIGEST_RE.fullmatch(context.filter_digest) is None
    ):
        raise _invalid()
    if context.sort not in ("name", "modified", "size") or type(context.sort) is not str:
        raise _invalid()
    if context.order not in ("asc", "desc") or type(context.order) is not str:
        raise _invalid()
    if not _bounded_integer(context.limit, 1, 250):
        raise _invalid()
    return {
        "user_id": str(context.user_id),
        "cache_namespace": context.cache_namespace,
        "user_epoch": context.user_epoch,
        "root_id": str(context.root_id),
        "root_epoch": context.root_epoch,
        "parent_id": str(context.parent_id),
        "parent_revision": context.parent_revision,
        "filter_digest": context.filter_digest,
        "sort": context.sort,
        "order": context.order,
        "limit": context.limit,
    }


def context_digest(fields: dict[str, object]) -> str:
    raw = json.dumps(
        fields,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def _expected_context_digest(context: CursorContext) -> str:
    try:
        return context_digest(_context_fields(context))
    except (TypeError, ValueError, UnicodeError):
        raise _invalid() from None


def _base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unbase64(value: object) -> bytes:
    if type(value) is not str:
        raise _invalid()
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise _invalid() from None
    if _base64(decoded) != value:
        raise _invalid()
    return decoded


def _validated_key(context: CursorContext, key: object) -> CursorKey:
    if type(key) not in (tuple, list):
        raise _invalid()
    values = cast(Sequence[object], key)
    if len(values) != 5:
        raise _invalid()
    kind_rank, null_rank, sort_value, name_key, entry_id = values
    if type(kind_rank) is not int or kind_rank not in (0, 1):
        raise _invalid()
    if type(null_rank) is not int or null_rank not in (0, 1):
        raise _invalid()
    if type(name_key) is not bytes or not 1 <= len(name_key) <= MAX_NAME_KEY_BYTES:
        raise _invalid()
    if type(entry_id) is not UUID:
        raise _invalid()
    if context.sort == "name":
        if null_rank != 0 or type(sort_value) is not bytes or sort_value != name_key:
            raise _invalid()
    else:
        minimum, maximum = (
            (0, MAX_SIZE)
            if context.sort == "size"
            else (MIN_TIMESTAMP_NS, MAX_TIMESTAMP_NS)
        )
        if not _bounded_integer(sort_value, minimum, maximum):
            raise _invalid()
        if null_rank == 1 and sort_value != 0:
            raise _invalid()
    return kind_rank, null_rank, cast(CursorSortValue, sort_value), name_key, entry_id


def _encoded_key(context: CursorContext, key: object) -> list[object]:
    kind_rank, null_rank, sort_value, name_key, entry_id = _validated_key(context, key)
    encoded_value: list[str]
    if type(sort_value) is bytes:
        encoded_value = ["bytes", _base64(sort_value)]
    else:
        encoded_value = ["integer", str(sort_value)]
    return [kind_rank, null_rank, encoded_value, _base64(name_key), str(entry_id)]


def _decoded_key(context: CursorContext, value: object) -> CursorKey:
    if type(value) is not list or len(value) != 5:
        raise _invalid()
    kind_rank, null_rank, encoded_value, encoded_name, encoded_id = value
    if type(encoded_value) is not list or len(encoded_value) != 2:
        raise _invalid()
    tag, scalar = encoded_value
    if tag == "bytes" and type(tag) is str:
        sort_value: CursorSortValue = _unbase64(scalar)
    elif tag == "integer" and type(tag) is str:
        if type(scalar) is not str or _INTEGER_RE.fullmatch(scalar) is None:
            raise _invalid()
        sort_value = int(scalar)
    else:
        raise _invalid()
    name_key = _unbase64(encoded_name)
    if type(encoded_id) is not str:
        raise _invalid()
    try:
        entry_id = UUID(encoded_id)
    except (ValueError, AttributeError):
        raise _invalid() from None
    if str(entry_id) != encoded_id:
        raise _invalid()
    return _validated_key(
        context,
        (kind_rank, null_rank, sort_value, name_key, entry_id),
    )


def signed_cursor_payload(payload: dict[str, object]) -> str:
    return signing.dumps(payload, salt=CURSOR_SALT, compress=False)


def unsigned_cursor_payload(value: str) -> object:
    if not isinstance(value, str) or len(value) > MAX_CURSOR_BYTES or value.startswith("."):
        raise CursorRestartRequired()
    try:
        return signing.loads(value, salt=CURSOR_SALT, max_age=CURSOR_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError, UnicodeError, RecursionError):
        raise CursorRestartRequired() from None


def encode_cursor(context: CursorContext, key: CursorKey, travel: CursorTravel) -> str:
    """Sign a strict keyset boundary; authorization remains the caller's duty."""

    try:
        expected_context = _expected_context_digest(context)
        if type(travel) is not str or travel not in ("next", "previous"):
            raise _invalid()
        payload: dict[str, object] = {
            "v": CURSOR_VERSION,
            "sv": SORT_KEY_VERSION,
            "context": expected_context,
            "key": _encoded_key(context, key),
            "travel": travel,
        }
        token = signed_cursor_payload(payload)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None
    if len(token) > MAX_CURSOR_BYTES or token.startswith("."):
        raise _invalid()
    return token


def decode_cursor(value: str, context: CursorContext) -> CursorPosition:
    """Authenticate and validate a cursor against the supplied authorized context."""

    payload = unsigned_cursor_payload(value)
    try:
        expected_context = _expected_context_digest(context)
        if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
            raise _invalid()
        if type(payload["v"]) is not int or payload["v"] != CURSOR_VERSION:
            raise _invalid()
        if type(payload["sv"]) is not int or payload["sv"] != SORT_KEY_VERSION:
            raise _invalid()
        supplied_context = payload["context"]
        if (
            type(supplied_context) is not str
            or _DIGEST_RE.fullmatch(supplied_context) is None
            or not secrets.compare_digest(supplied_context, expected_context)
        ):
            raise _invalid()
        travel = payload["travel"]
        if type(travel) is not str or travel not in ("next", "previous"):
            raise _invalid()
        key = _decoded_key(context, payload["key"])
        return CursorPosition(key, cast(CursorTravel, travel))
    except (KeyError, TypeError, ValueError, UnicodeError, RecursionError):
        raise CursorRestartRequired() from None
