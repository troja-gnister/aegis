from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Literal

import pytest
from aegis_apps.roots.manifest import ManifestSlot, MountManifest
from aegisctl.mounts import MountAttestationError, attest_mounts, parse_mountinfo


@pytest.mark.parametrize("role", ["operations", "indexer", "media"])
@pytest.mark.parametrize("mode", [0o000, 0o100, 0o400])
def test_role_attestation_rejects_unreadable_or_untraversable_root(
    tmp_path: Path,
    role: Literal["operations", "indexer", "media"],
    mode: int,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sentinel = source / "keep"
    sentinel.write_bytes(b"synthetic original")
    source = source.resolve()
    record = f"1 0 8:1 /private {source} ro - ext4 /dev/sda rw\n".encode()
    fingerprint = parse_mountinfo(record)[str(source)].mount_fingerprint
    manifest = MountManifest(
        "a" * 64,
        {
            "photos": ManifestSlot(
                "photos",
                PurePosixPath(source),
                "read_only",
                "local:1:2",
                1,
                2,
                fingerprint,
            )
        },
    )
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_bytes(record)
    source.chmod(mode)
    try:
        with pytest.raises(MountAttestationError, match="photos"):
            attest_mounts(manifest, role, mountinfo_path=mountinfo)
    finally:
        source.chmod(0o700)
    assert sentinel.read_bytes() == b"synthetic original"
