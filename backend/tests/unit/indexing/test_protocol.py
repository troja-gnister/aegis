"""Framing must reject corruption without trusting sender-controlled lengths."""

import io
import json
import struct

import pytest
from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind, Observation, SourceState
from aegis_apps.catalog.names import source_name
from aegis_apps.indexing.protocol import (
    ProtocolError,
    ReaderBatch,
    ReaderChannelState,
    ReaderComplete,
    ReaderFailure,
    ReaderMessage,
    decode_message,
    encode_message,
    read_message,
)


def observation(raw: bytes = b"odd\xff\\name") -> Observation:
    return Observation(source_name(raw), EntryKind.FILE, SourceState.PRESENT,
                       9007199254740993, -10, 30, 40, 50)


def frame(payload: object) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return struct.pack("!I", len(raw)) + raw


@pytest.mark.parametrize("message", [
    ReaderBatch(1, (observation(),)),
    ReaderComplete(DirectoryIdentity(40, 50, -10, 30)),
    ReaderFailure("permission_denied"),
])
def test_roundtrip_preserves_raw_bytes_and_lossless_integer_metadata(
    message: ReaderMessage,
) -> None:
    encoded = encode_message(message)
    assert struct.unpack("!I", encoded[:4])[0] == len(encoded) - 4
    assert decode_message(encoded) == message
    if isinstance(message, ReaderBatch):
        assert b"9007199254740993" in encoded
        assert b'"raw":"b2Rk/1xuYW1l"' in encoded


@pytest.mark.parametrize("raw", [
    b"", b"\0\0\0", b"\0\0\0\0", b"\0\x10\0\x01",
    b"\0\0\0\x01\xff", b"\0\0\0\x02{}extra",
    frame({"tag": "failure", "code": "private exception /source"}),
    frame({"tag": "failure", "code": "source_unavailable", "path": "/secret"}),
    frame({"tag": "batch", "sequence": True, "observations": []}),
    frame({"tag": "complete", "identity": [1, 2, 3, 4.0]}),
    frame({"tag": "complete", "identity": [1, 2, 3, 2**80]}),
    b'\0\0\0\x1f{"tag":"failure","tag":"batch"}',
    struct.pack("!I", 20000) + b"[" * 10000 + b"]" * 10000,
], ids=[f"invalid-{n}" for n in range(13)])
def test_rejects_malformed_or_unbounded_frames(raw: bytes) -> None:
    with pytest.raises(ProtocolError, match="reader_protocol_error"):
        decode_message(raw)


@pytest.mark.parametrize("field,value", [
    ("raw", "eB=="), ("raw", "Lw=="), ("raw", "eA==\n"),
    ("kind", "executable"), ("state", "unknown"), ("size", True),
    ("inode", -1), ("mtime_ns", 2**64), ("size", -1),
])
def test_rejects_invalid_observation_fields(field: str, value: object) -> None:
    payload = json.loads(encode_message(ReaderBatch(1, (observation(b"x"),)))[4:])
    payload["observations"][0][field] = value
    with pytest.raises(ProtocolError):
        decode_message(frame(payload))


def test_stream_checks_header_before_requesting_body() -> None:
    class HeaderOnly(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None
            assert 0 <= size <= 4, "unvalidated length used for body allocation"
            return super().read(size)

    with pytest.raises(ProtocolError):
        read_message(HeaderOnly(struct.pack("!I", 1048577)))


def test_stream_handles_fragmentation_and_truncation() -> None:
    class Fragmented(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            assert size is not None
            return super().read(min(size, 2))

    encoded = encode_message(ReaderFailure("source_unavailable"))
    assert read_message(Fragmented(encoded)) == ReaderFailure("source_unavailable")
    with pytest.raises(ProtocolError):
        read_message(Fragmented(encoded[:-1]))


def test_encoder_rejects_oversized_batch_before_encoding_records() -> None:
    with pytest.raises(ProtocolError):
        encode_message(ReaderBatch(1, (observation(),) * 2001))


def test_two_credits_sequence_and_explicit_matching_acknowledgment() -> None:
    state = ReaderChannelState()
    state.accept(ReaderBatch(1, (observation(),)))
    state.accept(ReaderBatch(2, (observation(),)))
    with pytest.raises(ProtocolError):
        state.accept(ReaderBatch(3, (observation(),)))
    with pytest.raises(ProtocolError):
        state.acknowledge(3)
    state.acknowledge(1)
    with pytest.raises(ProtocolError):
        state.acknowledge(1)
    state.accept(ReaderBatch(3, (observation(),)))
    with pytest.raises(ProtocolError):
        state.accept(ReaderBatch(3, (observation(),)))
    state.accept(ReaderComplete(DirectoryIdentity(1, 2, 3, 4)))
    state.acknowledge(2)
    state.acknowledge(3)
    with pytest.raises(ProtocolError):
        state.accept(ReaderFailure("reader_timeout"))


def test_reordered_frames_and_frames_after_failure_are_rejected() -> None:
    state = ReaderChannelState()
    with pytest.raises(ProtocolError):
        state.accept(ReaderBatch(2, (observation(),)))
    state.accept(ReaderFailure("reader_timeout"))
    with pytest.raises(ProtocolError):
        state.accept(ReaderBatch(1, (observation(),)))


def test_encoder_rejects_valid_records_that_exceed_byte_bound() -> None:
    item = Observation(source_name(b"x" * 255), EntryKind.FILE, SourceState.PRESENT,
                       9223372036854775807, -9223372036854775808, 9223372036854775807,
                       18446744073709551615, 18446744073709551615)
    with pytest.raises(ProtocolError):
        encode_message(ReaderBatch(1, (item,) * 2000))


def test_duplicate_json_fields_are_rejected_with_correct_frame_length() -> None:
    raw = b'{"tag":"failure","code":"reader_timeout","code":"source_unavailable"}'
    with pytest.raises(ProtocolError):
        decode_message(struct.pack("!I", len(raw)) + raw)
