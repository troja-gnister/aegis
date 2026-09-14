"""Bounded, data-only messages between a reader and its future supervisor."""

import base64
import binascii
import json
import struct
from dataclasses import dataclass, field
from typing import BinaryIO, Literal, cast

from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind, Observation, SourceState
from aegis_apps.catalog.names import source_name

MAX_PAYLOAD_BYTES = 1024 * 1024
MAX_BATCH_RECORDS = 2000
MAX_INTEGER = (1 << 63) - 1
_CODES = frozenset({
    "source_unavailable", "identity_changed", "permission_denied", "unsupported_entry",
    "reader_timeout", "reader_protocol_error",
})

FailureCode = Literal[
    "source_unavailable", "identity_changed", "permission_denied", "unsupported_entry",
    "reader_timeout", "reader_protocol_error",
]


class ProtocolError(ValueError):
    def __init__(self) -> None:
        super().__init__("reader_protocol_error")


@dataclass(frozen=True, slots=True)
class ReaderBatch:
    sequence: int
    observations: tuple[Observation, ...]


@dataclass(frozen=True, slots=True)
class ReaderComplete:
    identity: DirectoryIdentity


@dataclass(frozen=True, slots=True)
class ReaderFailure:
    code: FailureCode


type ReaderMessage = ReaderBatch | ReaderComplete | ReaderFailure


def encode_message(message: ReaderMessage) -> bytes:
    if type(message) is ReaderBatch:
        _integer(message.sequence, 1)
        if type(message.observations) is not tuple or not 1 <= len(
            message.observations
        ) <= MAX_BATCH_RECORDS:
            raise ProtocolError
        payload = {"tag": "batch", "sequence": message.sequence,
                   "observations": [_observation_payload(item) for item in message.observations]}
    elif type(message) is ReaderComplete:
        identity = message.identity
        if type(identity) is not DirectoryIdentity:
            raise ProtocolError
        payload = {"tag": "complete", "identity": [
            _integer(identity.device, 0, (1 << 64) - 1),
            _integer(identity.inode, 1, (1 << 64) - 1),
            _integer(identity.mtime_ns), _integer(identity.ctime_ns),
        ]}
    elif type(message) is ReaderFailure:
        if type(message.code) is not str or message.code not in _CODES:
            raise ProtocolError
        payload = {"tag": "failure", "code": message.code}
    else:
        raise ProtocolError
    raw = _json_bytes(payload)
    if len(raw) > MAX_PAYLOAD_BYTES:
        raise ProtocolError
    return struct.pack("!I", len(raw)) + raw


def _integer(value: object, minimum: int = -(1 << 63), maximum: int = MAX_INTEGER) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProtocolError
    return value


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()


def _observation_payload(item: Observation) -> dict[str, object]:
    if type(item) is not Observation:
        raise ProtocolError
    try:
        if source_name(item.name.raw) != item.name:
            raise ProtocolError
    except (ValueError, AttributeError):
        raise ProtocolError from None
    if type(item.kind) is not EntryKind or type(item.state) is not SourceState:
        raise ProtocolError
    payload: dict[str, object] = {
        "raw": base64.b64encode(item.name.raw).decode("ascii"),
        "kind": item.kind.value, "state": item.state.value,
    }
    for key, value, minimum, maximum in (
        ("size", item.size, 0, MAX_INTEGER),
        ("mtime_ns", item.mtime_ns, -(1 << 63), MAX_INTEGER),
        ("ctime_ns", item.ctime_ns, -(1 << 63), MAX_INTEGER),
        ("device", item.device, 0, (1 << 64) - 1),
        ("inode", item.inode, 1, (1 << 64) - 1),
    ):
        payload[key] = None if value is None else _integer(value, minimum, maximum)
    return payload


def observation_encoded_size(item: Observation) -> int:
    """Exact JSON object size; caller adds envelope and comma bytes."""
    return len(_json_bytes(_observation_payload(item)))


def batch_frame_overhead(sequence: int) -> int:
    _integer(sequence, 1)
    return 4 + len(_json_bytes({"tag": "batch", "sequence": sequence, "observations": []}))


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError
        result[key] = value
    return result


def _parsed_int(raw: str) -> int:
    if len(raw) > 21:
        raise ProtocolError
    return int(raw)


def _constant(raw: str) -> object:
    raise ProtocolError


def _fields(value: object, fields: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProtocolError
    return cast(dict[str, object], value)


def _decode_observation(value: object) -> Observation:
    obj = _fields(value, {
        "raw", "kind", "state", "size", "mtime_ns", "ctime_ns", "device", "inode",
    })
    raw = obj["raw"]
    if not isinstance(raw, str) or not 1 <= len(raw) <= 340 or not raw.isascii():
        raise ProtocolError
    try:
        decoded = base64.b64decode(raw, validate=True)
        if base64.b64encode(decoded).decode("ascii") != raw:
            raise ProtocolError
        name = source_name(decoded)
        kind_value, state_value = obj["kind"], obj["state"]
        if not isinstance(kind_value, str) or not isinstance(state_value, str):
            raise ProtocolError
        kind = EntryKind(kind_value)
        state = SourceState(state_value)
    except (ValueError, TypeError, binascii.Error):
        raise ProtocolError from None

    def number(key: str, minimum: int = -(1 << 63), maximum: int = MAX_INTEGER) -> int | None:
        value = obj[key]
        return None if value is None else _integer(value, minimum, maximum)

    return Observation(name, kind, state, number("size", 0), number("mtime_ns"), number("ctime_ns"),
                       number("device", 0, (1 << 64) - 1), number("inode", 1, (1 << 64) - 1))


def decode_message(raw: bytes) -> ReaderMessage:
    if type(raw) is not bytes or len(raw) < 5:
        raise ProtocolError
    length = struct.unpack("!I", raw[:4])[0]
    if not 1 <= length <= MAX_PAYLOAD_BYTES or len(raw) != length + 4:
        raise ProtocolError
    try:
        obj = json.loads(raw[4:].decode("utf-8", "strict"), object_pairs_hook=_object,
                         parse_int=_parsed_int, parse_constant=_constant)
        if not isinstance(obj, dict):
            raise ProtocolError
        if obj.get("tag") == "batch":
            _fields(obj, {"tag", "sequence", "observations"})
            sequence = _integer(obj["sequence"], 1)
            items = obj["observations"]
            if not isinstance(items, list) or not 1 <= len(items) <= MAX_BATCH_RECORDS:
                raise ProtocolError
            return ReaderBatch(sequence, tuple(_decode_observation(item) for item in items))
        if obj.get("tag") == "complete":
            _fields(obj, {"tag", "identity"})
            identity = obj["identity"]
            if not isinstance(identity, list) or len(identity) != 4:
                raise ProtocolError
            return ReaderComplete(DirectoryIdentity(
                _integer(identity[0], 0, (1 << 64) - 1),
                _integer(identity[1], 1, (1 << 64) - 1),
                _integer(identity[2]), _integer(identity[3]),
            ))
        _fields(obj, {"tag", "code"})
        if obj["tag"] != "failure" or type(obj["code"]) is not str or obj["code"] not in _CODES:
            raise ProtocolError
        return ReaderFailure(cast(FailureCode, obj["code"]))
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise ProtocolError from None


def read_message(stream: BinaryIO) -> ReaderMessage:
    """Read exactly one frame; validate the header before requesting body bytes."""
    def exact(size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            chunk = stream.read(min(size - len(result), 65536))
            if not chunk or len(chunk) > size - len(result):
                raise ProtocolError
            result.extend(chunk)
        return bytes(result)

    try:
        header = exact(4)
        length = struct.unpack("!I", header)[0]
        if not 1 <= length <= MAX_PAYLOAD_BYTES:
            raise ProtocolError
        return decode_message(header + exact(length))
    except OSError:
        raise ProtocolError from None


@dataclass(slots=True)
class ReaderChannelState:
    """Task 7 must reserve before send and acknowledge only committed/discarded batches.

    Maintain one state per reader per transport endpoint. This helper owns no IO;
    it cannot enforce acknowledgment timing or cap an independently created pipe.
    A protocol violation must abort the pass at the supervisor boundary.
    """

    _next_sequence: int = field(default=1, init=False)
    _outstanding: set[int] = field(default_factory=set, init=False)
    _terminal: bool = field(default=False, init=False)

    def accept(self, message: ReaderMessage) -> None:
        if self._terminal:
            raise ProtocolError
        encode_message(message)
        if isinstance(message, ReaderBatch):
            if message.sequence != self._next_sequence or len(self._outstanding) >= 2:
                raise ProtocolError
            self._outstanding.add(message.sequence)
            self._next_sequence += 1
        else:
            self._terminal = True

    def acknowledge(self, sequence: int) -> None:
        _integer(sequence, 1)
        if sequence not in self._outstanding:
            raise ProtocolError
        self._outstanding.remove(sequence)
