from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.podman_dev import tools

REPOSITORY = Path(__file__).resolve().parents[2]
BINARY = b"#!/bin/sh\necho compose\n"
COMPOSE_URL = "https://example.invalid/docker-compose"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_lock(path: Path, entries: dict[str, dict[str, str]]) -> Path:
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def tar_bytes(files: dict[str, bytes], mode: str = "w:gz") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Downloads:
    """Serve fixed bytes per URL and record every fetch."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.fetched: list[str] = []

    def __call__(self, url: str, target: Path) -> None:
        self.fetched.append(url)
        target.write_bytes(self.payloads[url])


def compose_lock(tmp_path: Path, payload: bytes = BINARY) -> Path:
    return write_lock(tmp_path / "lock.json", {"docker-compose": {
        "version": "2.39.4", "kind": "binary", "url": COMPOSE_URL,
        "sha256": digest(payload), "destination": ".local/libexec/aegis/docker-compose",
    }})


def test_real_lock_pins_ci_versions_and_keeps_compose_off_path() -> None:
    locked = tools.load_lock()
    assert {name: tool.version for name, tool in locked.items()} == {
        "uv": "0.12.8", "node": "24.20.0", "docker-compose": "2.39.4",
    }
    assert locked["docker-compose"].destination == ".local/libexec/aegis/docker-compose"
    assert locked["node"].destination == ".local/opt/node-v24.20.0-linux-x64"
    workflow = (REPOSITORY / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert 'version: "0.12.8"' in workflow
    assert 'node-version: "24.20.0"' in workflow
    guard = REPOSITORY / "backend/aegisctl/podman_mask_compatibility.py"
    assert '== "2.39.4"' in guard.read_text(encoding="utf-8")


@pytest.mark.parametrize(("field", "value", "message"), [
    ("url", "http://example.invalid/tool", "https"),
    ("destination", "../outside", "home directory"),
    ("destination", "/usr/local/bin/tool", "home directory"),
    ("kind", "zip", "unknown kind"),
])
def test_lock_refuses_unsafe_entries(
    tmp_path: Path, field: str, value: str, message: str,
) -> None:
    entry = {
        "version": "1", "kind": "binary", "url": COMPOSE_URL,
        "sha256": digest(BINARY), "destination": ".local/bin/tool",
    } | {field: value}
    with pytest.raises(tools.ToolError, match=message):
        tools.load_lock(write_lock(tmp_path / "lock.json", {"tool": entry}))


def test_binary_installs_once_then_verifies_without_downloading(tmp_path: Path) -> None:
    lock = compose_lock(tmp_path)
    home = tmp_path / "home"
    installed = home / ".local/libexec/aegis/docker-compose"
    downloads = Downloads({COMPOSE_URL: BINARY})
    assert tools.ensure_tools(home, fetch=downloads, lock=lock) == [
        f"docker-compose 2.39.4: installed {installed}",
    ]
    assert installed.read_bytes() == BINARY
    assert installed.stat().st_mode & 0o777 == 0o755
    assert tools.ensure_tools(home, fetch=downloads, lock=lock) == [
        f"docker-compose 2.39.4: verified {installed}",
    ]
    assert downloads.fetched == [COMPOSE_URL]


def test_download_with_wrong_checksum_installs_nothing(tmp_path: Path) -> None:
    lock = compose_lock(tmp_path)
    home = tmp_path / "home"
    with pytest.raises(tools.ToolError, match="checksum"):
        tools.ensure_tools(home, fetch=Downloads({COMPOSE_URL: b"tampered"}), lock=lock)
    assert list((home / ".local/libexec/aegis").iterdir()) == []


def test_mismatched_existing_file_is_reported_and_left_unchanged(tmp_path: Path) -> None:
    lock = compose_lock(tmp_path)
    home = tmp_path / "home"
    installed = home / ".local/libexec/aegis/docker-compose"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"someone else's compose")
    installed.chmod(0o755)
    downloads = Downloads({COMPOSE_URL: BINARY})
    with pytest.raises(tools.ToolError, match="MISMATCH"):
        tools.ensure_tools(home, fetch=downloads, lock=lock)
    assert installed.read_bytes() == b"someone else's compose"
    assert downloads.fetched == []


def test_group_writable_binary_counts_as_mismatch(tmp_path: Path) -> None:
    lock = compose_lock(tmp_path)
    home = tmp_path / "home"
    tools.ensure_tools(home, fetch=Downloads({COMPOSE_URL: BINARY}), lock=lock)
    (home / ".local/libexec/aegis/docker-compose").chmod(0o775)
    assert tools.installed_state(tools.load_lock(lock)["docker-compose"], home) == "mismatch"


def test_tar_member_installs_only_the_pinned_member(tmp_path: Path) -> None:
    uv = b"uv binary"
    archive = tar_bytes({"uv-x86_64/uv": uv, "uv-x86_64/uvx": b"not wanted"})
    url = "https://example.invalid/uv.tar.gz"
    lock = write_lock(tmp_path / "lock.json", {"uv": {
        "version": "0.12.8", "kind": "tar-member", "url": url, "sha256": digest(archive),
        "member": "uv-x86_64/uv", "member_sha256": digest(uv), "destination": ".local/bin/uv",
    }})
    home = tmp_path / "home"
    tools.ensure_tools(home, fetch=Downloads({url: archive}), lock=lock)
    assert (home / ".local/bin/uv").read_bytes() == uv
    assert sorted(path.name for path in (home / ".local/bin").iterdir()) == ["uv"]


def test_tar_tree_installs_with_receipt_and_verifies(tmp_path: Path) -> None:
    archive = tar_bytes(
        {"node-v24/bin/node": b"node", "node-v24/README.md": b"docs"}, "w:xz",
    )
    url = "https://example.invalid/node.tar.xz"
    lock = write_lock(tmp_path / "lock.json", {"node": {
        "version": "24.20.0", "kind": "tar-tree", "url": url, "sha256": digest(archive),
        "member": "node-v24", "destination": ".local/opt/node-v24",
    }})
    home = tmp_path / "home"
    root = home / ".local/opt/node-v24"
    downloads = Downloads({url: archive})
    tools.ensure_tools(home, fetch=downloads, lock=lock)
    assert (root / "bin/node").read_bytes() == b"node"
    receipt = json.loads((root / tools.RECEIPT).read_text(encoding="utf-8"))
    assert receipt == {"version": "24.20.0", "sha256": digest(archive)}
    assert tools.ensure_tools(home, fetch=downloads, lock=lock) == [
        f"node 24.20.0: verified {root}",
    ]
    assert downloads.fetched == [url]
    assert [path.name for path in (home / ".local/opt").iterdir()] == ["node-v24"]
