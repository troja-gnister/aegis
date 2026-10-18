"""The /proc/self/mountinfo parser alone: standard library only and cheap to import.

Every scan reader child parses its mount table before opening a source, so it imports this
module rather than all of ``aegisctl.mounts``, which re-exports these names.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal

Mode = Literal["read_only", "read_write"]

MAX_PATH_LENGTH = 4096
MAX_MOUNTINFO_BYTES = 1024 * 1024


class MountAttestationError(ValueError):
    """A safe runtime mount-attestation error."""


@dataclass(frozen=True, slots=True)
class MountInfoRecord:
    mountpoint: str
    effective_mode: Mode
    mount_fingerprint: str
    filesystem_identity: tuple[str, str]
    filesystem_root: PurePosixPath


_MOUNTINFO_ESCAPES = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}


def _decode_mountinfo_field(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            result.append(value[index])
            index += 1
            continue
        escape = value[index + 1 : index + 4]
        if len(escape) != 3 or escape not in _MOUNTINFO_ESCAPES:
            raise MountAttestationError("mountinfo contains an invalid escape")
        result.append(_MOUNTINFO_ESCAPES[escape])
        index += 4
    return "".join(result)


def _option_mode(options: str) -> Mode:
    values = options.split(",")
    has_ro = "ro" in values
    has_rw = "rw" in values
    if has_ro == has_rw:
        raise MountAttestationError("mountinfo contains ambiguous mount flags")
    return "read_only" if has_ro else "read_write"


def _parse_mountinfo_records(raw: bytes) -> tuple[MountInfoRecord, ...]:
    if len(raw) > MAX_MOUNTINFO_BYTES:
        raise MountAttestationError("mountinfo exceeds size limit")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise MountAttestationError("mountinfo is malformed") from exc
    records: list[MountInfoRecord] = []
    lines = text.splitlines()
    if len(lines) > 8192:
        raise MountAttestationError("mountinfo exceeds record limit")
    for line in lines:
        if not line or len(line) > 16_384:
            raise MountAttestationError("mountinfo is malformed")
        fields = line.split(" ")
        separators = [index for index, field in enumerate(fields) if field == "-"]
        if len(separators) != 1 or separators[0] < 6 or len(fields) <= separators[0] + 3:
            raise MountAttestationError("mountinfo is malformed")
        mountpoint = _decode_mountinfo_field(fields[4])
        major_minor = fields[2]
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9}):(?:0|[1-9][0-9]{0,9})", major_minor):
            raise MountAttestationError("mountinfo is malformed")
        encoded_root = fields[3]
        filesystem_type = fields[separators[0] + 1]
        mount_source = fields[separators[0] + 2]
        if any(
            not value or len(value) > MAX_PATH_LENGTH
            for value in (encoded_root, filesystem_type, mount_source)
        ):
            raise MountAttestationError("mountinfo is malformed")
        decoded_root = _decode_mountinfo_field(encoded_root)
        filesystem_root = PurePosixPath(decoded_root)
        _decode_mountinfo_field(filesystem_type)
        _decode_mountinfo_field(mount_source)
        per_mount = _option_mode(fields[5])
        superblock = _option_mode(fields[separators[0] + 3])
        effective: Mode
        if per_mount == "read_only" or superblock == "read_only":
            effective = "read_only"
        elif per_mount == "read_write" and superblock == "read_write":
            effective = "read_write"
        else:
            raise MountAttestationError("mountinfo contains ambiguous mount flags")
        fingerprint_payload = b"aegis.mount-fingerprint.v1\0" + b"".join(
            value.encode("ascii") + b"\0"
            for value in (major_minor, encoded_root, filesystem_type, mount_source)
        )
        records.append(MountInfoRecord(
            mountpoint,
            effective,
            hashlib.sha256(fingerprint_payload).hexdigest(),
            (major_minor, filesystem_type),
            filesystem_root,
        ))
    return tuple(records)


def parse_mountinfo(raw: bytes) -> Mapping[str, MountInfoRecord]:
    records: dict[str, MountInfoRecord] = {}
    for record in _parse_mountinfo_records(raw):
        if record.mountpoint in records:
            raise MountAttestationError("mountinfo contains an ambiguous mountpoint")
        records[record.mountpoint] = record
    return MappingProxyType(records)
