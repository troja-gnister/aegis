"""Bounded, canonical metadata filters for indexed directory browsing."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from .domain import EntryKind, SourceState

FILTER_VERSION = 1
MAX_FILTER_FIELDS = 8
MAX_SELECTED_VALUES = 32
MAX_FILTER_BYTES = 8192
MAX_PREFIX_BYTES = 2048
MAX_SIZE = 2**64 - 1
MIN_TIMESTAMP_NS = -(2**63)
MAX_TIMESTAMP_NS = 2**63 - 1
UNKNOWN_TYPE = "__unknown__"

_TYPE_RE = re.compile(r"[a-z0-9]{1,16}\Z", re.ASCII)
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)
_TIMESTAMP_RE = re.compile(
    r"(?P<year>[0-9]{4})-(?P<month>[0-9]{2})-(?P<day>[0-9]{2})"
    r"T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2}):(?P<second>[0-9]{2})"
    r"(?:\.(?P<fraction>[0-9]{1,9}))?"
    r"(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})\Z",
    re.ASCII,
)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FILTER_FIELDS = frozenset(
    {"v", "kind", "type", "size", "modified", "availability", "prefix"}
)


@dataclass(frozen=True, slots=True)
class SizeRange:
    """Inclusive unsigned-64 file-size bounds, or an explicit null selection."""

    minimum: int | None = None
    maximum: int | None = None
    unknown: bool = False


@dataclass(frozen=True, slots=True)
class ModifiedRange:
    """A UTC nanosecond ``[from_ns, before_ns)`` range, or explicit null selection."""

    from_ns: int | None = None
    before_ns: int | None = None
    unknown: bool = False


@dataclass(frozen=True, slots=True)
class FileFilter:
    """Normalized values whose canonical bytes can safely bind a cursor context."""

    kind: tuple[EntryKind, ...] = ()
    type: tuple[str, ...] = ()
    size: SizeRange | None = None
    modified: ModifiedRange | None = None
    availability: tuple[SourceState, ...] = ()
    prefix: str | None = None


def _invalid() -> ValueError:
    return ValueError("invalid filters")


def _bounded_sum(total: int, addition: int, limit: int) -> int:
    if addition > limit - total:
        raise _invalid()
    return total + addition


def _ascii_json_string_size(value: str, limit: int) -> int:
    size = 2
    if size > limit:
        raise _invalid()
    for character in value:
        point = ord(character)
        if character in ('"', "\\", "\b", "\f", "\n", "\r", "\t"):
            addition = 2
        elif point < 0x20 or 0x7E < point <= 0xFFFF:
            addition = 6
        elif point > 0xFFFF:
            addition = 12
        else:
            addition = 1
        size = _bounded_sum(size, addition, limit)
    return size


def _ascii_json_size(value: object, limit: int, *, depth: int = 0) -> int:
    if depth > 4:
        raise _invalid()
    if type(value) is str:
        return _ascii_json_string_size(value, limit)
    if value is None:
        size = 4
    elif type(value) is bool:
        size = 4 if value else 5
    elif type(value) is int:
        if value.bit_length() > 4 * limit:
            raise _invalid()
        try:
            size = len(str(value))
        except ValueError:
            raise _invalid() from None
    elif type(value) is list:
        size = 2
        for index, item in enumerate(value):
            size = _bounded_sum(size, int(index > 0), limit)
            size = _bounded_sum(
                size,
                _ascii_json_size(item, limit - size, depth=depth + 1),
                limit,
            )
    elif type(value) is dict:
        size = 2
        for index, (key, item) in enumerate(value.items()):
            if type(key) is not str:
                raise _invalid()
            size = _bounded_sum(size, int(index > 0), limit)
            size = _bounded_sum(size, _ascii_json_string_size(key, limit - size), limit)
            size = _bounded_sum(size, 1, limit)
            size = _bounded_sum(
                size,
                _ascii_json_size(item, limit - size, depth=depth + 1),
                limit,
            )
    else:
        raise _invalid()
    if size > limit:
        raise _invalid()
    return size


def _validate_raw_filter_bytes(value: dict[object, object]) -> None:
    _ascii_json_size(value, MAX_FILTER_BYTES)


def _enum_selection[EnumValue: StrEnum](
    value: object,
    enum_type: type[EnumValue],
) -> tuple[EnumValue, ...]:
    if type(value) is not list or len(value) > MAX_SELECTED_VALUES:
        raise _invalid()
    selected: set[EnumValue] = set()
    for item in value:
        if type(item) is not str:
            raise _invalid()
        try:
            selected.add(enum_type(item))
        except ValueError:
            raise _invalid() from None
    return tuple(sorted(selected, key=str))


def _type_selection(value: object) -> tuple[str, ...]:
    if type(value) is not list or len(value) > MAX_SELECTED_VALUES:
        raise _invalid()
    selected: set[str] = set()
    for item in value:
        if type(item) is not str or (
            item != UNKNOWN_TYPE and _TYPE_RE.fullmatch(item) is None
        ):
            raise _invalid()
        selected.add(item)
    return tuple(sorted(selected))


def _decimal_size(value: object) -> int:
    if type(value) is not str or _DECIMAL_RE.fullmatch(value) is None:
        raise _invalid()
    number = int(value)
    if number > MAX_SIZE:
        raise _invalid()
    return number


def _size_range(value: object) -> SizeRange:
    if type(value) is not dict or not value or not set(value) <= {"min", "max", "unknown"}:
        raise _invalid()
    unknown = value.get("unknown", False)
    if type(unknown) is not bool or ("unknown" in value and not unknown):
        raise _invalid()
    if unknown:
        if set(value) != {"unknown"}:
            raise _invalid()
        return SizeRange(unknown=True)
    minimum = _decimal_size(value["min"]) if "min" in value else None
    maximum = _decimal_size(value["max"]) if "max" in value else None
    if minimum is None and maximum is None:
        raise _invalid()
    if minimum is not None and maximum is not None and minimum > maximum:
        raise _invalid()
    return SizeRange(minimum, maximum)


def _timestamp_ns(value: object) -> int:
    if type(value) is not str:
        raise _invalid()
    matched = _TIMESTAMP_RE.fullmatch(value)
    if matched is None:
        raise _invalid()
    fields = matched.groupdict()
    try:
        local = datetime(
            int(fields["year"]),
            int(fields["month"]),
            int(fields["day"]),
            int(fields["hour"]),
            int(fields["minute"]),
            int(fields["second"]),
            tzinfo=UTC,
        )
    except ValueError:
        raise _invalid() from None
    zone = fields["zone"]
    offset_seconds = 0
    if zone != "Z":
        offset_hour, offset_minute = (int(part) for part in zone[1:].split(":"))
        if offset_hour > 23 or offset_minute > 59:
            raise _invalid()
        offset_seconds = (offset_hour * 60 + offset_minute) * 60
        if zone[0] == "-":
            offset_seconds = -offset_seconds
    delta = local - _EPOCH
    whole_seconds = delta.days * 86_400 + delta.seconds - offset_seconds
    fraction = (fields["fraction"] or "").ljust(9, "0")
    result = whole_seconds * 1_000_000_000 + int(fraction or "0")
    if not MIN_TIMESTAMP_NS <= result <= MAX_TIMESTAMP_NS:
        raise _invalid()
    return result


def _modified_range(value: object) -> ModifiedRange:
    allowed = {"from", "before", "unknown"}
    if type(value) is not dict or not value or not set(value) <= allowed:
        raise _invalid()
    unknown = value.get("unknown", False)
    if type(unknown) is not bool or ("unknown" in value and not unknown):
        raise _invalid()
    if unknown:
        if set(value) != {"unknown"}:
            raise _invalid()
        return ModifiedRange(unknown=True)
    from_ns = _timestamp_ns(value["from"]) if "from" in value else None
    before_ns = _timestamp_ns(value["before"]) if "before" in value else None
    if from_ns is None and before_ns is None:
        raise _invalid()
    if from_ns is not None and before_ns is not None and from_ns >= before_ns:
        raise _invalid()
    return ModifiedRange(from_ns, before_ns)


def _prefix(value: object) -> str | None:
    if type(value) is not str:
        raise _invalid()
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid() from None
    if len(encoded) > MAX_PREFIX_BYTES:
        raise _invalid()
    normalized = unicodedata.normalize("NFC", value.casefold())
    return normalized or None


def parse_filters(value: object) -> FileFilter:
    """Validate and normalize a versioned filter request without external access."""

    if type(value) is not dict or len(value) > MAX_FILTER_FIELDS:
        raise _invalid()
    if set(value) - _FILTER_FIELDS or type(value.get("v")) is not int:
        raise _invalid()
    if value["v"] != FILTER_VERSION:
        raise _invalid()
    _validate_raw_filter_bytes(value)
    filters = FileFilter(
        kind=_enum_selection(value["kind"], EntryKind) if "kind" in value else (),
        type=_type_selection(value["type"]) if "type" in value else (),
        size=_size_range(value["size"]) if "size" in value else None,
        modified=_modified_range(value["modified"]) if "modified" in value else None,
        availability=(
            _enum_selection(value["availability"], SourceState)
            if "availability" in value
            else ()
        ),
        prefix=_prefix(value["prefix"]) if "prefix" in value else None,
    )
    canonical_filter_bytes(filters)
    return filters


def canonical_filter_bytes(filters: FileFilter) -> bytes:
    """Return the sole stable JSON representation used to derive filter digests."""

    if type(filters) is not FileFilter:
        raise _invalid()
    document: dict[str, object] = {"v": FILTER_VERSION}
    if filters.kind:
        document["kind"] = [value.value for value in filters.kind]
    if filters.type:
        document["type"] = list(filters.type)
    if filters.size is not None:
        size: dict[str, object] = {}
        if filters.size.minimum is not None:
            size["min"] = str(filters.size.minimum)
        if filters.size.maximum is not None:
            size["max"] = str(filters.size.maximum)
        if filters.size.unknown:
            size["unknown"] = True
        document["size"] = size
    if filters.modified is not None:
        modified: dict[str, object] = {}
        if filters.modified.from_ns is not None:
            modified["from"] = str(filters.modified.from_ns)
        if filters.modified.before_ns is not None:
            modified["before"] = str(filters.modified.before_ns)
        if filters.modified.unknown:
            modified["unknown"] = True
        document["modified"] = modified
    if filters.availability:
        document["availability"] = [value.value for value in filters.availability]
    if filters.prefix is not None:
        document["prefix"] = filters.prefix
    try:
        raw = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError):
        raise _invalid() from None
    if len(raw) > MAX_FILTER_BYTES:
        raise _invalid()
    return raw
