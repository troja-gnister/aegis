"""Effective mount provenance checks for exact disposable PostgreSQL fixtures."""

from __future__ import annotations

import errno
import hashlib
import os
import re
from pathlib import Path
from typing import Any

POSTGRES_PRIVATE_TMPFS = {
    "/run/aegis-source-secrets": (65_536, 0o700),
    "/run/secrets": (65_536, 0o700),
    "/run/postgresql": (16_777_216, 0o775),
    "/tmp": (16_777_216, 0o1777),
}
SOURCE_CHILDREN = {
    f"/run/aegis-source-secrets/{name}"
    for name in (
        "postgres_superuser_password",
        "db_migrator_password",
        "db_web_password",
        "db_operations_password",
        "db_indexer_password",
        "db_media_password",
    )
}


def canonical_tmpfs_target(target: str) -> str:
    return "/run/postgresql" if target == "/var/run/postgresql" else target


def assert_private_tmpfs_provenance(
    inspection: dict[str, Any],
    expected: dict[str, tuple[int, int]],
) -> None:
    """Require explicit engine tmpfs declarations and reject overlapping inputs.

    Docker can omit tmpfs from Mounts; its HostConfig.Tmpfs declaration is still
    required. Podman can include them there. Neither representation may contain
    a bind/volume substitution, unexpected child mount, or duplicate alias.
    Container stat/mountinfo independently proves final ownership/protections.
    """
    declarations = inspection.get("HostConfig", {}).get("Tmpfs")
    assert isinstance(declarations, dict), "private tmpfs declarations unavailable"
    normalized: dict[str, str] = {}
    for target, options in declarations.items():
        assert isinstance(target, str) and isinstance(options, str)
        canonical = canonical_tmpfs_target(target)
        assert canonical not in normalized, "ambiguous tmpfs alias"
        normalized[canonical] = options
    for target, (size, mode) in expected.items():
        assert target in normalized, "private tmpfs declaration missing"
        flags: set[str] = set()
        values: dict[str, str] = {}
        for option in normalized[target].split(","):
            name, separator, value = option.partition("=")
            assert name not in flags and name not in values, "duplicate tmpfs option"
            if separator:
                values[name] = value
            else:
                flags.add(name)
        assert {"noexec", "nosuid", "nodev"} <= flags, "unprotected private tmpfs"
        assert not flags & {"ro", "exec", "suid", "dev", "shared", "rshared"}
        match = re.fullmatch(r"([0-9]+)([kKmM]?)", values.get("size", ""))
        assert match is not None, "unbounded private tmpfs"
        multiplier = {"": 1, "k": 1024, "m": 1024 * 1024}[match[2].lower()]
        assert int(match[1]) * multiplier == size, "private tmpfs size changed"
        assert re.fullmatch(r"0?[0-7]{3,4}", values.get("mode", ""))
        assert int(values["mode"], 8) == mode, "private tmpfs mode changed"

    mounts = inspection.get("Mounts")
    assert isinstance(mounts, list), "effective mounts unavailable"
    destinations: set[str] = set()
    for mount in mounts:
        assert isinstance(mount, dict) and isinstance(mount.get("Destination"), str)
        destination = canonical_tmpfs_target(mount["Destination"])
        assert destination not in destinations, "ambiguous effective mount"
        destinations.add(destination)
        for target in expected:
            if destination == target:
                assert mount.get("Type") == "tmpfs", "private tmpfs was substituted"
            elif target.startswith(destination.rstrip("/") + "/"):
                raise AssertionError("private tmpfs ancestor was substituted")
            elif destination.startswith(target + "/"):
                assert target == "/run/aegis-source-secrets" and destination in SOURCE_CHILDREN
                assert mount.get("Type") == "bind" and mount.get("RW") is False
    if "/run/aegis-source-secrets" in expected:
        assert destinations >= SOURCE_CHILDREN, "read-only source mounts missing"


def source_metadata_snapshot(paths: list[Path]) -> dict[str, tuple[object, ...]]:
    """Hash only owned synthetic inputs, after their one-time fixture preparation."""
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in paths:
        metadata = path.lstat()
        try:
            label = os.getxattr(path, "security.selinux", follow_symlinks=False)
        except OSError as error:
            if error.errno not in (errno.ENODATA, errno.ENOTSUP):
                raise
            label = None
        snapshot[str(path)] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_size,
            metadata.st_mtime_ns,
            label,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
    return snapshot
