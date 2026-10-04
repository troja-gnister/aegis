"""Pinned user-local development tools: install once, verify on every run.

Standard library only, so ``python3 -m scripts.podman_dev tools`` can bootstrap a
machine that does not have uv yet.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LOCK = Path(__file__).with_name("tools.lock.json")
RECEIPT = ".aegis-tool-receipt.json"
KINDS = ("binary", "tar-member", "tar-tree")
MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024

Fetch = Callable[[str, Path], None]


class ToolError(RuntimeError):
    """A pinned tool is missing, mismatched or could not be installed safely."""


@dataclass(frozen=True)
class Tool:
    name: str
    version: str
    kind: str
    url: str
    sha256: str
    destination: str
    member: str = ""
    member_sha256: str = ""

    def path(self, home: Path) -> Path:
        return home / self.destination


def load_lock(path: Path = LOCK) -> dict[str, Tool]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    tools = {name: Tool(name=name, **fields) for name, fields in raw.items()}
    for tool in tools.values():
        destination = Path(tool.destination)
        if tool.kind not in KINDS:
            raise ToolError(f"{tool.name}: unknown kind {tool.kind!r}")
        if not tool.url.startswith("https://"):
            raise ToolError(f"{tool.name}: download URL must use https")
        if destination.is_absolute() or ".." in destination.parts:
            raise ToolError(f"{tool.name}: destination must stay inside the home directory")
        if tool.kind != "binary" and not tool.member:
            raise ToolError(f"{tool.name}: archive tools must name their member")
        if tool.kind == "tar-member" and len(tool.member_sha256) != 64:
            raise ToolError(f"{tool.name}: archive members need a pinned SHA-256")
    return tools


def tool_path(name: str, home: Path, lock: Path = LOCK) -> Path:
    return load_lock(lock)[name].path(home)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def https_fetch(url: str, target: Path) -> None:
    if not url.startswith("https://"):
        raise ToolError(f"refusing a non-https download: {url}")
    with urllib.request.urlopen(url, timeout=60) as response, target.open("xb") as output:
        copied = 0
        for block in iter(lambda: response.read(1024 * 1024), b""):
            copied += len(block)
            if copied > MAX_DOWNLOAD_BYTES:
                raise ToolError(f"download exceeds {MAX_DOWNLOAD_BYTES} bytes: {url}")
            output.write(block)


def installed_state(tool: Tool, home: Path) -> str:
    """Return ``missing``, ``verified`` or ``mismatch`` for the installed copy."""
    target = tool.path(home)
    if not os.path.lexists(target):
        return "missing"
    if target.is_symlink():
        return "mismatch"
    if tool.kind == "tar-tree":
        try:
            receipt = json.loads((target / RECEIPT).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "mismatch"
        expected = {"version": tool.version, "sha256": tool.sha256}
        return "verified" if receipt == expected else "mismatch"
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or not metadata.st_mode & stat.S_IXUSR
    ):
        return "mismatch"
    expected_digest = tool.member_sha256 if tool.kind == "tar-member" else tool.sha256
    return "verified" if sha256_file(target) == expected_digest else "mismatch"


def install(tool: Tool, home: Path, fetch: Fetch = https_fetch) -> None:
    target = tool.path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aegis-tool-") as scratch:
        download = Path(scratch) / "download"
        fetch(tool.url, download)
        actual = sha256_file(download)
        if actual != tool.sha256:
            raise ToolError(f"{tool.name}: download checksum {actual} != pinned {tool.sha256}")
        # Stage beside the target so the final rename is atomic on one filesystem.
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
        try:
            os.replace(_stage(tool, download, staging, target.name), target)
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _stage(tool: Tool, download: Path, staging: Path, name: str) -> Path:
    if tool.kind == "binary":
        staged = staging / name
        shutil.copyfile(download, staged)
        staged.chmod(0o755)
        return staged
    with tarfile.open(download) as archive:
        if tool.kind == "tar-member":
            try:
                member = archive.getmember(tool.member)
            except KeyError as error:
                raise ToolError(f"{tool.name}: archive lacks {tool.member}") from error
            source = archive.extractfile(member) if member.isfile() else None
            if source is None:
                raise ToolError(f"{tool.name}: {tool.member} is not a regular file")
            staged = staging / name
            with source, staged.open("xb") as output:
                shutil.copyfileobj(source, output)
            if sha256_file(staged) != tool.member_sha256:
                raise ToolError(f"{tool.name}: extracted {tool.member} checksum mismatch")
            staged.chmod(0o755)
            return staged
        archive.extractall(staging, filter="data")
    staged = staging / tool.member
    if staged.is_symlink() or not staged.is_dir():
        raise ToolError(f"{tool.name}: archive lacks the directory {tool.member}")
    receipt = {"version": tool.version, "sha256": tool.sha256}
    (staged / RECEIPT).write_text(json.dumps(receipt), encoding="utf-8")
    return staged


def ensure_tools(home: Path, *, fetch: Fetch = https_fetch, lock: Path = LOCK) -> list[str]:
    lines: list[str] = []
    problems: list[str] = []
    for tool in load_lock(lock).values():
        target = tool.path(home)
        state = installed_state(tool, home)
        if state == "missing":
            install(tool, home, fetch)
            if installed_state(tool, home) != "verified":
                problems.append(f"{tool.name} {tool.version}: installed copy does not verify")
                continue
            lines.append(f"{tool.name} {tool.version}: installed {target}")
        elif state == "verified":
            lines.append(f"{tool.name} {tool.version}: verified {target}")
        else:
            problems.append(f"{tool.name} {tool.version}: MISMATCH at {target}; left unchanged")
    if problems:
        raise ToolError("\n".join([*lines, *problems]))
    return lines
