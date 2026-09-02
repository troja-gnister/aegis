from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType

import pytest
from aegis_apps.roots.manifest import ManifestError, MountManifest


def _write_manifest(path: Path, *, slots: list[dict[str, object]] | None = None) -> str:
    payload = {
        "version": 1,
        "generatedAt": "2026-09-02T12:34:56Z",
        "slots": slots
        if slots is not None
        else [
            {
                "slotId": "photos",
                "containerPath": "/srv/aegis/roots/photos",
                "mode": "read_only",
                "filesystemId": 123,
                "rootInode": 456,
                "expectedIdentity": "remote:nas01:/exports/photos",
                "mountFingerprint": "a" * 64,
            }
        ],
    }
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    os.chown(path, os.geteuid(), os.getegid())
    return hashlib.sha256(raw).hexdigest()


def test_load_returns_immutable_sanitized_slots(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    digest = _write_manifest(path)

    manifest = MountManifest.load(path, digest)

    assert isinstance(manifest.slots, MappingProxyType)
    assert manifest.get("photos") is not None
    assert manifest.get("photos").container_path.as_posix() == "/srv/aegis/roots/photos"  # type: ignore[union-attr]
    assert not hasattr(manifest.get("photos"), "source")
    with pytest.raises(TypeError):
        manifest.slots["other"] = manifest.slots["photos"]  # type: ignore[index]


@pytest.mark.parametrize("mode", [0o644, 0o400, 0o660])
def test_load_requires_exact_0600(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "manifest.json"
    digest = _write_manifest(path)
    path.chmod(mode)

    with pytest.raises(ManifestError, match="permissions"):
        MountManifest.load(path, digest)


def test_load_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    digest = _write_manifest(target)
    link = tmp_path / "manifest.json"
    link.symlink_to(target)

    with pytest.raises(ManifestError, match="regular"):
        MountManifest.load(link, digest)


def test_load_rejects_unknown_fields_and_duplicate_identity_without_disclosure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private-name.json"
    slot = {
        "slotId": "photos",
        "containerPath": "/srv/aegis/roots/photos",
        "mode": "read_only",
        "filesystemId": 123,
        "rootInode": 456,
        "expectedIdentity": "local:123:456",
        "mountFingerprint": "a" * 64,
    }
    digest = _write_manifest(path, slots=[slot, {**slot, "slotId": "other", "extra": 1}])

    with pytest.raises(ManifestError) as caught:
        MountManifest.load(path, digest)

    assert str(path) not in str(caught.value)
    assert "local:123:456" not in str(caught.value)


def test_load_rejects_wrong_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "manifest.json"
    digest = _write_manifest(path)
    real_fstat = os.fstat

    class _FakeStat:
        def __init__(self, original: os.stat_result) -> None:
            self.st_mode = original.st_mode
            self.st_uid = original.st_uid + 1
            self.st_gid = original.st_gid

    monkeypatch.setattr(
        os,
        "fstat",
        lambda descriptor: _FakeStat(real_fstat(descriptor)),
    )

    with pytest.raises(ManifestError, match="owner"):
        MountManifest.load(path, digest)


def test_load_rechecks_the_descriptor_when_path_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    digest = _write_manifest(path)
    replacement = tmp_path / "replacement.json"
    replacement.write_text('{"replaced":true}\n', encoding="ascii")
    replacement.chmod(0o600)
    real_read_bytes = Path.read_bytes

    def replace_then_read(self: Path) -> bytes:
        if self == path:
            replacement.replace(path)
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", replace_then_read)

    manifest = MountManifest.load(path, digest)

    assert manifest.get("photos") is not None
