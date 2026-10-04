# Rootless Podman Development Environment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the shared Fedora 44 Server development host a repeatable Aegis development loop under native rootless Podman: pinned toolchain, a committed session helper, and `make verify`, `make verify-compose` and `make test-e2e` running to completion, with Docker CI kept green.

**Architecture:** A standard-library package `scripts/podman_dev/` (invoked as `python -m scripts.podman_dev <command>`) installs pinned user-local tools, starts a private foreground `podman system service` socket plus a locally built Playwright WebKit server container, prints the environment the existing guarded launcher expects, and stops everything by recorded identity while comparing the pre-existing Podman inventory. The canonical runners stay unchanged except for one allowlisted argument pass-through in `scripts/test-e2e.sh` and a line-number-only failure diagnostic in the e2e sanitizer (Task 10). Every failure found by the gates is triaged by a fixed policy; fixes go into Podman-specific layers and never weaken protections.

**Tech Stack:** Python 3.13.15 (stdlib only for the helper), pytest, uv 0.12.8, Node.js 24.20.0, Playwright 1.63.0, rootless Podman 5.8.7 with crun 1.28 and pasta, Docker Compose 2.39.4 as the Podman Compose provider, bash.

**Spec:** [`docs/superpowers/specs/2026-10-04-podman-dev-environment-design.md`](../specs/2026-10-04-podman-dev-environment-design.md) (approved October 4, 2026). Executors read the spec and this plan together.

## Global Constraints

- Exact versions: uv 0.12.8; Python 3.13.15; Node.js 24.20.0; Playwright 1.63.0 (from `frontend/package-lock.json`); Docker Compose 2.39.4; Podman 5.8.7; crun 1.28.
- The Compose provider lives at `~/.local/libexec/aegis/docker-compose`, outside `PATH`, selected only through `PODMAN_COMPOSE_PROVIDER`.
- Node.js 24 lives at `~/.local/opt/node-v24.20.0-linux-x64/` and is prepended to `PATH` only by the helper's `env` output; the host Node 22 stays the default.
- The helper package `scripts/podman_dev/` uses only the Python standard library, so `python3 -m scripts.podman_dev tools` works before uv exists.
- The private API socket lives under `$XDG_RUNTIME_DIR/aegis-podman-dev/`, never on TCP, never through the systemd `podman.socket` unit, never rootful.
- Never: skips or expected-failure markers, relaxed security assertions, `:z`/`:Z`/`:U` on originals, `label=disable`, rootful or privileged execution, `prune`, or deletion by name, label or prefix.
- Before every harness run: ports 18080 and 55432 have no listeners (never stop an unrelated service), and `/` has at least 40 GiB of free space; otherwise stop and ask the user.
- Shared files (`compose.yaml`, runners, product code) keep identical Docker semantics.
- The host's existing rootless containers and their volumes, networks and images are never test or cleanup targets.
- Workflow: work on `main`; implementers do not commit or spawn agents; the root session commits after each task's reviews pass; **ask the user before every push** and watch CI with `gh`.
- Static gates: `uv run --locked ruff check backend tests scripts` and `uv run --locked mypy backend` stay clean. The project does not use `ruff format`.
- Never commit secrets, generated mounts, raw logs, private screenshots or browser storage. Working notes and logs go in the ignored `.superpowers/sdd/2026-10-04-podman-dev-environment/` directory.
- Do not claim full Podman compatibility while O11 (rootless client-address loss) is open.

## Review Focus

1. A session whose API process died (crash, OOM, logout): `env` must exit non-zero with nothing on stdout, and `stop` must clean up recorded entries without signalling anything. Pinned in Task 4 by `test_environment_refuses_a_dead_session`, `test_cli_env_without_session_prints_nothing` and `test_stop_api_recovers_when_process_is_already_gone`.
2. PID reuse after a crash: `stop` must never signal a process whose start time or command differs from the record. Pinned in Task 4 by `test_stop_api_refuses_a_reused_pid`.
3. An existing service on the host recreates or removes its container during a session: `stop` reports it with before and after identities, exits 3, removes only helper state and never touches the container. Pinned in Task 4 by `test_stop_reports_changed_inventory_and_still_cleans_up`.
4. `start` run a second time while a session exists: the second run refuses and changes nothing. Pinned in Task 4 by `test_start_refuses_when_state_exists_and_changes_nothing`.
5. Home or runtime paths containing spaces, quotes or `$`: `env` output must round-trip through a real shell. Pinned in Task 4 by `test_export_lines_round_trip_through_bash`.

---

## File Structure

| Path | Responsibility | Task |
| --- | --- | --- |
| `scripts/podman_dev/__init__.py` | Package docstring | 1 |
| `scripts/podman_dev/tools.lock.json` | Pinned tool versions, URLs, SHA-256s, destinations | 1 |
| `scripts/podman_dev/tools.py` | Install or verify the pinned user-local tools | 1 |
| `scripts/podman_dev/__main__.py` | Command line (`tools` in Task 1; all commands in Task 4) | 1, 4 |
| `scripts/podman_dev/podman.py` | The single local `podman --remote=false` command form | 2 |
| `scripts/podman_dev/records.py` | Session error type, identity dataclasses, private JSON record | 2 |
| `scripts/podman_dev/inventory.py` | Snapshot and compare pre-existing Podman objects | 2 |
| `docker/dev-webkit.Dockerfile` | Development-only Playwright WebKit server image | 3 |
| `scripts/podman_dev/webkit.py` | Build, create, start, check and remove the WebKit container | 3 |
| `scripts/podman_dev/session.py` | State paths, preflight, shell export lines | 4 |
| `scripts/podman_dev/api.py` | Private API service launch, readiness, identity and stop | 4 |
| `scripts/podman_dev/lifecycle.py` | `start`/`status`/`env`/`stop` orchestration | 4 |
| `tests/support/podman_dev_doubles.py` | Scripted Podman runner, fake `/proc` entries, listing replies | 2 |
| `tests/deployment/test_podman_dev_tools.py` | Tools tests | 1 |
| `tests/deployment/test_podman_dev_records.py` | Records and inventory tests | 2 |
| `tests/deployment/test_podman_dev_webkit.py` | WebKit tests | 3 |
| `tests/deployment/test_podman_dev_session.py` | Session, API, lifecycle and CLI tests | 4 |
| `tests/deployment/test_compose.py:340` | Stale core-services NNP assertion | 7 |
| `scripts/test-e2e.sh`, `frontend/e2e/safe-location.ts`, `frontend/e2e/safe-test.ts`, `frontend/e2e/safe-reporter.ts`, `frontend/vite.config.ts` | Allowlisted Playwright arguments and failure-line diagnostics | 10 |
| `docs/verification/linux-podman-prerequisite.md`, `README.md`, `docs/development.md`, `HANDOFF.md` | Evidence and documentation | 11 |

The spec names a single module `scripts/podman_dev.py`; this plan uses the package `scripts/podman_dev/` with the same invocation (`python -m scripts.podman_dev`), following the `scripts/benchmarks/` package pattern, so each file keeps one responsibility.

Working-note directory for every task (ignored by git):

```bash
SDD=.superpowers/sdd/2026-10-04-podman-dev-environment
EVIDENCE="$SDD/evidence"
mkdir -p "$EVIDENCE"
```

---

### Task 0: Bootstrap uv, Python and the project environment (operational, no commit)

**Files:** none tracked. Creates `~/.local/bin/uv`, the uv-managed Python 3.13.15 and `.venv/`.

**Interfaces:**
- Consumes: nothing.
- Produces: a working `uv run --locked` environment for Tasks 1–11.

- [ ] **Step 1: Confirm the starting point**

Run:
```bash
git status --short --branch
git log --oneline -3
test ! -e ~/.local/bin/uv && echo "uv absent: ok"
rpm -q podman crun
```
Expected: clean tree on `main` (ahead of `origin/main` only by the spec/plan commits), `uv absent: ok`, `podman-5.8.7-1.fc44`, `crun-1.28-1.fc44`. If `~/.local/bin/uv` exists, stop and report it; do not overwrite.

- [ ] **Step 2: Install uv 0.12.8 with checksum verification**

Run:
```bash
scratch=$(mktemp -d)
curl -sSfL -o "$scratch/uv.tar.gz" \
  https://github.com/astral-sh/uv/releases/download/0.12.8/uv-x86_64-unknown-linux-gnu.tar.gz
echo "2e2b37e9811e17675a9e70bed5e1a58fc8c0388be63d751d72cc735188c149ff  $scratch/uv.tar.gz" | sha256sum -c -
tar -xzf "$scratch/uv.tar.gz" -C "$scratch" uv-x86_64-unknown-linux-gnu/uv
echo "fe60af3d6b1771ecea15fdfd3821959912009e1612b3767a4988f135bc3cdd9d  $scratch/uv-x86_64-unknown-linux-gnu/uv" | sha256sum -c -
install -D -m 0755 "$scratch/uv-x86_64-unknown-linux-gnu/uv" ~/.local/bin/uv
rm -r "$scratch"
uv --version
```
Expected: both `sha256sum` lines print `OK`; `uv 0.12.8 (x86_64-unknown-linux-gnu)`.

- [ ] **Step 3: Install Python 3.13.15 and the locked environment**

Run:
```bash
uv python install 3.13.15
uv sync --locked --group dev
.venv/bin/python --version
uv lock --check
```
Expected: `Python 3.13.15`; `uv lock --check` succeeds.

- [ ] **Step 4: Confirm resource-free tests run without a database**

Run:
```bash
uv run --locked pytest backend/tests/unit/aegisctl/test_container_engine.py -q -p no:cacheprovider
uv run --locked pytest tests/deployment --collect-only -q -p no:cacheprovider | tail -3
```
Expected: the unit file passes, and collection of `tests/deployment` finishes with a test count and no errors (this proves the root `conftest.py` and test settings import without a database). If either fails on database or settings errors, stop and report the exact error; later tasks rely on running helper tests this way.

- [ ] **Step 5: Confirm static gates are clean at baseline**

Run:
```bash
uv run --locked ruff check backend tests scripts
uv run --locked mypy backend
```
Expected: both clean. Record the outputs in `$EVIDENCE/task0.md` with the versions from Steps 2–3.

---

### Task 1: Pinned tools installer (`tools` command)

**Files:**
- Create: `scripts/podman_dev/__init__.py`
- Create: `scripts/podman_dev/tools.lock.json`
- Create: `scripts/podman_dev/tools.py`
- Create: `scripts/podman_dev/__main__.py`
- Test: `tests/deployment/test_podman_dev_tools.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tools.ToolError(RuntimeError)`
  - `tools.Tool` (frozen dataclass: `name`, `version`, `kind`, `url`, `sha256`, `destination`, `member=""`, `member_sha256=""`; method `path(home: Path) -> Path`)
  - `tools.load_lock(path: Path = LOCK) -> dict[str, Tool]`
  - `tools.tool_path(name: str, home: Path, lock: Path = LOCK) -> Path`
  - `tools.installed_state(tool: Tool, home: Path) -> str` returning `"missing"`, `"verified"` or `"mismatch"`
  - `tools.ensure_tools(home: Path, *, fetch: Fetch = https_fetch, lock: Path = LOCK) -> list[str]`
  - `tools.RECEIPT == ".aegis-tool-receipt.json"`

- [ ] **Step 1: Write the failing tests**

Create `tests/deployment/test_podman_dev_tools.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/deployment/test_podman_dev_tools.py -q -p no:cacheprovider`
Expected: collection error `ModuleNotFoundError: No module named 'scripts.podman_dev'`.

- [ ] **Step 3: Write the package, lock and installer**

Create `scripts/podman_dev/__init__.py`:

```python
"""Private rootless Podman development session for Aegis (development hosts only)."""
```

Create `scripts/podman_dev/tools.lock.json`:

```json
{
  "uv": {
    "version": "0.12.8",
    "kind": "tar-member",
    "url": "https://github.com/astral-sh/uv/releases/download/0.12.8/uv-x86_64-unknown-linux-gnu.tar.gz",
    "sha256": "2e2b37e9811e17675a9e70bed5e1a58fc8c0388be63d751d72cc735188c149ff",
    "member": "uv-x86_64-unknown-linux-gnu/uv",
    "member_sha256": "fe60af3d6b1771ecea15fdfd3821959912009e1612b3767a4988f135bc3cdd9d",
    "destination": ".local/bin/uv"
  },
  "node": {
    "version": "24.20.0",
    "kind": "tar-tree",
    "url": "https://nodejs.org/dist/v24.20.0/node-v24.20.0-linux-x64.tar.xz",
    "sha256": "2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2",
    "member": "node-v24.20.0-linux-x64",
    "destination": ".local/opt/node-v24.20.0-linux-x64"
  },
  "docker-compose": {
    "version": "2.39.4",
    "kind": "binary",
    "url": "https://github.com/docker/compose/releases/download/v2.39.4/docker-compose-linux-x86_64",
    "sha256": "7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4",
    "destination": ".local/libexec/aegis/docker-compose"
  }
}
```

These values were checked on October 4, 2026 against the publishers' checksum files: uv's `.sha256` release asset (the member checksum comes from extracting that verified archive), Node.js `SHASUMS256.txt`, and Docker Compose's `.sha256` asset. The Compose value also equals the provider SHA-256 recorded in `docs/verification/linux-podman-prerequisite.md`.

Create `scripts/podman_dev/tools.py`:

```python
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
```

Create `scripts/podman_dev/__main__.py` (Task 4 replaces it with the full command set):

```python
"""Command line for the Podman development helper: python -m scripts.podman_dev."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.podman_dev import tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.podman_dev")
    parser.add_argument("command", choices=("tools",))
    parser.parse_args(argv)
    try:
        print("\n".join(tools.ensure_tools(Path.home())))
    except tools.ToolError as error:
        print(f"podman_dev: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/deployment/test_podman_dev_tools.py -q -p no:cacheprovider`
Expected: all tests pass (11 including parameters).

- [ ] **Step 5: Static checks**

Run: `uv run --locked ruff check backend tests scripts`
Expected: clean.

- [ ] **Step 6: Install the real tools with the system Python**

Run from the repository root:
```bash
python3 -m scripts.podman_dev tools
~/.local/libexec/aegis/docker-compose version --short
~/.local/opt/node-v24.20.0-linux-x64/bin/node --version
command -v docker-compose || echo "docker-compose not on PATH: ok"
```
Expected: `uv 0.12.8: verified …/.local/bin/uv` (Task 0 installed the identical binary), `node 24.20.0: installed …`, `docker-compose 2.39.4: installed …`; then `2.39.4`, `v24.20.0`, and `docker-compose not on PATH: ok`.

- [ ] **Step 7: Install frontend dependencies and host Chromium**

Run:
```bash
export PATH="$HOME/.local/opt/node-v24.20.0-linux-x64/bin:$PATH"
npm --prefix frontend ci
(cd frontend && npm exec -- playwright install chromium)
(cd frontend && node -e "
const {chromium} = require('@playwright/test');
chromium.launch({chromiumSandbox: true})
  .then(async (b) => { console.log('chromium', b.version()); await b.close(); })
  .catch((e) => { console.error(e.message); process.exit(1); });")
```
Expected: `npm ci` succeeds without lockfile changes (`git status --short` shows nothing new); the probe prints `chromium <version>`. If the launch fails on missing shared libraries, run `ldd ~/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux/headless_shell | grep 'not found'`, stop, and report the exact libraries to the user. Do not install packages.

- [ ] **Step 8: Commit (root session only, after reviews pass)**

```bash
git add scripts/podman_dev/__init__.py scripts/podman_dev/__main__.py \
  scripts/podman_dev/tools.py scripts/podman_dev/tools.lock.json \
  tests/deployment/test_podman_dev_tools.py
git commit -m "build: add pinned user-local tools for Podman development"
```

---

### Task 2: Session records, local Podman runner and inventory comparison

**Files:**
- Create: `scripts/podman_dev/podman.py`
- Create: `scripts/podman_dev/records.py`
- Create: `scripts/podman_dev/inventory.py`
- Create: `tests/support/podman_dev_doubles.py`
- Test: `tests/deployment/test_podman_dev_records.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `podman.Runner = Callable[..., subprocess.CompletedProcess[str]]`; `podman.run(arguments: Sequence[str], *, timeout: float = 120.0) -> CompletedProcess[str]` (always `podman --remote=false …`, `check=False`)
  - `records.SessionError(RuntimeError)`
  - `records.PathIdentity(device: int, inode: int)` with classmethod `of(path: Path)`
  - `records.ApiRecord(pid: int, start_time: int, command: list[str], socket: str, directory_identity: PathIdentity, socket_identity: PathIdentity | None = None)`
  - `records.WebkitRecord(container_id: str, image_id: str, port: int, token: str)` with property `endpoint -> str`
  - `records.SessionRecord(state_identity: PathIdentity, api: ApiRecord | None = None, webkit: WebkitRecord | None = None)`
  - `records.RECORD_NAME == "session.json"`, `records.TEMPORARY_NAME == ".session.json.tmp"`
  - `records.save_record(state: Path, record: SessionRecord) -> None`; `records.load_record(state: Path) -> SessionRecord`
  - `inventory.Inventory = dict[str, dict[str, list[Any]]]`; `inventory.snapshot(run: Runner = podman.run) -> Inventory`; `inventory.compare(before: Inventory, after: Inventory) -> list[str]`
  - Test support: `ScriptedPodman`, `write_proc_entry`, `listing_replies`

- [ ] **Step 1: Write the test doubles**

Create `tests/support/podman_dev_doubles.py`:

```python
"""Resource-free doubles for the Podman development helper tests."""
from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

Reply = tuple[Sequence[str], int, str]


class ScriptedPodman:
    """Answer each call with the next scripted reply after checking its command prefix."""

    def __init__(self, replies: Sequence[Reply]) -> None:
        self.replies = [(tuple(prefix), code, stdout) for prefix, code, stdout in replies]
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, arguments: Sequence[str], *, timeout: float = 120.0,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        command = tuple(arguments)
        self.calls.append(command)
        assert self.replies, f"unexpected podman call {command}"
        prefix, code, stdout = self.replies.pop(0)
        assert command[: len(prefix)] == prefix, f"expected {prefix}, got {command}"
        return subprocess.CompletedProcess(list(command), code, stdout, "")

    def assert_done(self) -> None:
        assert not self.replies, f"unused scripted replies {self.replies}"


def write_proc_entry(
    proc: Path, pid: int, *, start_time: int, command: Sequence[str], state: str = "S",
) -> None:
    """A minimal /proc/<pid> with a command name that contains spaces and parentheses."""
    entry = proc / str(pid)
    entry.mkdir(parents=True, exist_ok=True)
    fields = " ".join([state, *(["0"] * 18), str(start_time), "0", "0"])
    (entry / "stat").write_text(f"{pid} (podman (svc) x) {fields}\n", encoding="ascii")
    (entry / "cmdline").write_bytes(b"\0".join(part.encode() for part in command) + b"\0")


def listing_replies(
    containers: str = "[]", volumes: str = "null", networks: str = "[]", images: str = "[]",
) -> list[Reply]:
    return [
        (("ps", "--all", "--format", "json"), 0, containers),
        (("volume", "ls", "--format", "json"), 0, volumes),
        (("network", "ls", "--format", "json"), 0, networks),
        (("images", "--all", "--format", "json"), 0, images),
    ]
```

- [ ] **Step 2: Write the failing tests**

Create `tests/deployment/test_podman_dev_records.py`:

```python
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from scripts.podman_dev import inventory
from scripts.podman_dev.records import (
    RECORD_NAME,
    ApiRecord,
    PathIdentity,
    SessionError,
    SessionRecord,
    WebkitRecord,
    load_record,
    save_record,
)
from tests.support.podman_dev_doubles import ScriptedPodman, listing_replies

CONTAINER = "c" * 64
IMAGE = "e" * 64
NETWORK = "f" * 64


def full_record() -> SessionRecord:
    return SessionRecord(
        state_identity=PathIdentity(1, 2),
        api=ApiRecord(
            pid=42, start_time=99, command=["podman", "system", "service"],
            socket="/run/user/1000/aegis-podman-dev/api-x/podman.sock",
            directory_identity=PathIdentity(3, 4), socket_identity=PathIdentity(5, 6),
        ),
        webkit=WebkitRecord(container_id=CONTAINER, image_id=IMAGE, port=41234, token="a" * 32),
    )


def test_record_round_trips_privately_and_atomically(tmp_path: Path) -> None:
    save_record(tmp_path, full_record())
    assert load_record(tmp_path) == full_record()
    assert stat.S_IMODE((tmp_path / RECORD_NAME).stat().st_mode) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == [RECORD_NAME]


def test_partial_record_round_trips(tmp_path: Path) -> None:
    partial = SessionRecord(state_identity=PathIdentity(7, 8))
    save_record(tmp_path, partial)
    assert load_record(tmp_path) == partial


def test_webkit_endpoint_has_the_validated_shape() -> None:
    assert full_record().webkit is not None
    assert full_record().webkit.endpoint == f"ws://127.0.0.1:41234/{'a' * 32}"


def test_snapshot_records_identity_fields_and_tolerates_null() -> None:
    run = ScriptedPodman(listing_replies(
        containers=json.dumps([{
            "Id": CONTAINER, "Names": ["media-app"], "ImageID": IMAGE, "Created": 1,
            "State": "running",
        }]),
        volumes="null",
        networks=json.dumps([{"id": NETWORK, "name": "demo_default", "created": "t0"}]),
        images=json.dumps([{"Id": IMAGE, "Created": 5}]),
    ))
    assert inventory.snapshot(run) == {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1]},
        "volumes": {},
        "networks": {NETWORK: ["demo_default", "t0"]},
        "images": {IMAGE: [5]},
    }
    run.assert_done()


def test_snapshot_survives_a_json_round_trip() -> None:
    run = ScriptedPodman(listing_replies(volumes=json.dumps([
        {"Name": "demo_caddy_data", "CreatedAt": "t1"},
    ])))
    snapshot = inventory.snapshot(run)
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_snapshot_failure_names_the_command() -> None:
    run = ScriptedPodman([(("ps",), 125, "")])
    with pytest.raises(SessionError, match="podman ps --all --format json failed"):
        inventory.snapshot(run)


def test_compare_ignores_new_objects_and_runtime_state() -> None:
    before: inventory.Inventory = {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1]},
        "volumes": {}, "networks": {}, "images": {},
    }
    after: inventory.Inventory = {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1], "d" * 64: [["new"], IMAGE, 2]},
        "volumes": {"aegis-new": ["t"]}, "networks": {}, "images": {"9" * 64: [3]},
    }
    assert inventory.compare(before, after) == []


def test_compare_reports_disappeared_and_recreated_objects() -> None:
    before: inventory.Inventory = {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1]},
        "volumes": {"demo_caddy_data": ["t1"]}, "networks": {}, "images": {},
    }
    after: inventory.Inventory = {
        "containers": {},
        "volumes": {"demo_caddy_data": ["t2"]}, "networks": {}, "images": {},
    }
    assert inventory.compare(before, after) == [
        f"pre-existing containers {CONTAINER} disappeared (was [['media-app'], '{IMAGE}', 1])",
        "pre-existing volumes demo_caddy_data changed from ['t1'] to ['t2']",
    ]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/deployment/test_podman_dev_records.py -q -p no:cacheprovider`
Expected: collection error `ModuleNotFoundError: No module named 'scripts.podman_dev.records'`.

- [ ] **Step 4: Write the modules**

Create `scripts/podman_dev/podman.py`:

```python
"""The one local Podman command form used by the helper (never a remote connection)."""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence

Runner = Callable[..., subprocess.CompletedProcess[str]]


def run(arguments: Sequence[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["podman", "--remote=false", *arguments],
        capture_output=True, text=True, timeout=timeout, check=False,
    )
```

Create `scripts/podman_dev/records.py`:

```python
"""Exact identities recorded for one helper session, persisted as private JSON."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

RECORD_NAME = "session.json"
TEMPORARY_NAME = f".{RECORD_NAME}.tmp"


class SessionError(RuntimeError):
    """A session cannot start, verify or stop safely; diagnostics are preserved."""


@dataclass(frozen=True)
class PathIdentity:
    device: int
    inode: int

    @classmethod
    def of(cls, path: Path) -> PathIdentity:
        metadata = path.lstat()
        return cls(metadata.st_dev, metadata.st_ino)


@dataclass
class ApiRecord:
    pid: int
    start_time: int
    command: list[str]
    socket: str
    directory_identity: PathIdentity
    socket_identity: PathIdentity | None = None


@dataclass(frozen=True)
class WebkitRecord:
    container_id: str
    image_id: str
    port: int
    token: str

    @property
    def endpoint(self) -> str:
        return f"ws://127.0.0.1:{self.port}/{self.token}"


@dataclass
class SessionRecord:
    state_identity: PathIdentity
    api: ApiRecord | None = None
    webkit: WebkitRecord | None = None


def save_record(state: Path, record: SessionRecord) -> None:
    temporary = state / TEMPORARY_NAME
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    with os.fdopen(os.open(temporary, flags, 0o600), "w", encoding="utf-8") as handle:
        json.dump(asdict(record), handle, indent=2, sort_keys=True)
    os.replace(temporary, state / RECORD_NAME)


def load_record(state: Path) -> SessionRecord:
    raw = json.loads((state / RECORD_NAME).read_text(encoding="utf-8"))
    api = raw["api"]
    webkit = raw["webkit"]
    return SessionRecord(
        state_identity=PathIdentity(**raw["state_identity"]),
        api=None if api is None else ApiRecord(
            pid=api["pid"],
            start_time=api["start_time"],
            command=list(api["command"]),
            socket=api["socket"],
            directory_identity=PathIdentity(**api["directory_identity"]),
            socket_identity=(
                None if api["socket_identity"] is None
                else PathIdentity(**api["socket_identity"])
            ),
        ),
        webkit=None if webkit is None else WebkitRecord(**webkit),
    )
```

Create `scripts/podman_dev/inventory.py`:

```python
"""Identity inventory of pre-existing Podman objects; it reports and never deletes.

Runtime state (running, exited, restart counts) is deliberately excluded: other
services on the host may restart during a session without that being a change.
"""
from __future__ import annotations

import json
from typing import Any

from scripts.podman_dev import podman
from scripts.podman_dev.records import SessionError

KINDS = ("containers", "volumes", "networks", "images")
Inventory = dict[str, dict[str, list[Any]]]


def _listing(run: podman.Runner, arguments: tuple[str, ...]) -> list[dict[str, Any]]:
    result = run(list(arguments))
    if result.returncode != 0:
        raise SessionError(f"podman {' '.join(arguments)} failed: {result.stderr[-2000:]}")
    parsed = json.loads(result.stdout or "null")
    return parsed if isinstance(parsed, list) else []


def snapshot(run: podman.Runner = podman.run) -> Inventory:
    containers = _listing(run, ("ps", "--all", "--format", "json"))
    volumes = _listing(run, ("volume", "ls", "--format", "json"))
    networks = _listing(run, ("network", "ls", "--format", "json"))
    images = _listing(run, ("images", "--all", "--format", "json"))
    return {
        "containers": {
            item["Id"]: [sorted(item["Names"]), item["ImageID"], item["Created"]]
            for item in containers
        },
        "volumes": {item["Name"]: [item["CreatedAt"]] for item in volumes},
        "networks": {item["id"]: [item["name"], item["created"]] for item in networks},
        "images": {item["Id"]: [item["Created"]] for item in images},
    }


def compare(before: Inventory, after: Inventory) -> list[str]:
    problems: list[str] = []
    for kind in KINDS:
        current = after.get(kind, {})
        for key, identity in sorted(before.get(kind, {}).items()):
            if key not in current:
                problems.append(f"pre-existing {kind} {key} disappeared (was {identity})")
            elif current[key] != identity:
                problems.append(
                    f"pre-existing {kind} {key} changed from {identity} to {current[key]}"
                )
    return problems
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/deployment/test_podman_dev_records.py -q -p no:cacheprovider`
Expected: 8 passed.

- [ ] **Step 6: Check the real field names once against this host**

Run (read-only):
```bash
uv run --locked python -c "
from scripts.podman_dev import inventory
s = inventory.snapshot()
print({kind: len(objects) for kind, objects in s.items()})"
```
Expected: non-zero counts for containers, volumes, networks and images, and no `KeyError`.

- [ ] **Step 7: Static checks**

Run: `uv run --locked ruff check backend tests scripts`
Expected: clean.

- [ ] **Step 8: Commit (root session only, after reviews pass)**

```bash
git add scripts/podman_dev/podman.py scripts/podman_dev/records.py \
  scripts/podman_dev/inventory.py tests/support/podman_dev_doubles.py \
  tests/deployment/test_podman_dev_records.py
git commit -m "feat: record Podman helper sessions and compare pre-existing inventory"
```

---

### Task 3: WebKit server image and container lifecycle

**Files:**
- Create: `docker/dev-webkit.Dockerfile`
- Create: `scripts/podman_dev/webkit.py`
- Test: `tests/deployment/test_podman_dev_webkit.py`

**Interfaces:**
- Consumes: `podman.Runner`, `podman.run`, `records.SessionError`, `records.WebkitRecord` (Task 2); test doubles `ScriptedPodman`.
- Produces:
  - `webkit.image_tag(repository: Path = REPOSITORY) -> str`
  - `webkit.ensure_image(run: Runner = podman.run, repository: Path = REPOSITORY) -> str` (full image ID)
  - `webkit.create_arguments(image_id: str, port: int, token: str) -> list[str]`
  - `webkit.start_webkit(on_created: Callable[[WebkitRecord], None], run: Runner = podman.run, *, repository: Path = REPOSITORY, port: int | None = None, token: str | None = None, sleep=time.sleep, now=time.monotonic) -> WebkitRecord`
  - `webkit.webkit_running(record: WebkitRecord, run: Runner = podman.run) -> bool`
  - `webkit.remove_webkit(record: WebkitRecord, run: Runner = podman.run) -> None`
  - `webkit.E2E_GATEWAY_PORT == 18080`

- [ ] **Step 1: Write the failing tests**

Create `tests/deployment/test_podman_dev_webkit.py`:

```python
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from scripts.podman_dev import webkit
from scripts.podman_dev.records import SessionError, WebkitRecord
from tests.support.podman_dev_doubles import ScriptedPodman

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER = "c" * 64
IMAGE = "e" * 64
TOKEN = "0123456789abcdef" * 2
ENDPOINT = re.compile(r"ws://127\.0\.0\.1:([1-9][0-9]{3,4})/([0-9a-f]{32})")


def copy_image_inputs(target: Path) -> Path:
    for relative in webkit.IMAGE_INPUTS:
        (target / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPOSITORY / relative, target / relative)
    return target


def test_dockerfile_pins_bases_by_digest_and_reuses_the_locked_node() -> None:
    dockerfile = (REPOSITORY / "docker/dev-webkit.Dockerfile").read_text(encoding="utf-8")
    bases = re.findall(r"^FROM (\S+)", dockerfile, flags=re.MULTILINE)
    assert bases and all("@sha256:" in base for base in bases)
    lock = (REPOSITORY / "deploy/images.lock").read_text(encoding="utf-8").split("\n")
    node = next(line.split() for line in lock if line.startswith("node "))
    assert f"docker.io/library/node:{node[1]}@{node[2]}" in bases
    assert "mcr.microsoft.com/playwright:v1.63.0-noble@sha256:" in bases[-1]
    assert re.search(r"^USER pwuser$", dockerfile, flags=re.MULTILINE)


def test_create_arguments_lock_the_container_down() -> None:
    arguments = webkit.create_arguments(IMAGE, 41234, TOKEN)
    assert arguments[0] == "create"
    assert TOKEN[:12] not in arguments[arguments.index("--name") + 1]
    pairs = list(zip(arguments, arguments[1:], strict=False))
    assert ("--cap-drop", "ALL") in pairs
    assert ("--security-opt", "no-new-privileges:true") in pairs
    assert ("--network", "pasta:-T,18080") in pairs
    assert ("--publish", "127.0.0.1:41234:41234") in pairs
    assert ("--memory", "2g") in pairs
    assert ("--pids-limit", "256") in pairs
    assert "--read-only" in arguments
    for forbidden in ("--privileged", "--volume", "-v", "--mount", "--device", "host"):
        assert forbidden not in arguments
    assert arguments[arguments.index(IMAGE) + 1 :] == [
        "node", webkit.SERVER_CLI, "run-server", "--host", "0.0.0.0",
        "--port", "41234", "--path", f"/{TOKEN}",
    ]


def test_image_tag_follows_the_image_inputs(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    first = webkit.image_tag(repository)
    assert re.fullmatch(r"localhost/aegis-dev-webkit:[0-9a-f]{16}", first)
    lock = repository / "frontend/package-lock.json"
    lock.write_text(lock.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert webkit.image_tag(repository) != first


def test_existing_image_is_reused_without_building(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman([(("image", "inspect"), 0, f"{IMAGE}\n")])
    assert webkit.ensure_image(run, repository) == IMAGE
    run.assert_done()


def test_missing_image_is_built_once(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman([
        (("image", "inspect"), 125, ""),
        (("build", "--file", str(repository / webkit.DOCKERFILE)), 0, "built"),
        (("image", "inspect"), 0, f"{IMAGE}\n"),
    ])
    assert webkit.ensure_image(run, repository) == IMAGE
    run.assert_done()


def started_replies(running: str = "true") -> list[tuple[tuple[str, ...], int, str]]:
    return [
        (("image", "inspect"), 0, f"{IMAGE}\n"),
        (("create",), 0, f"{CONTAINER}\n"),
        (("start", CONTAINER), 0, CONTAINER),
        (("container", "inspect", "--format", "{{.State.Running}}", CONTAINER), 0, running),
        (("logs", CONTAINER), 0, f"Listening on ws://0.0.0.0:41234/{TOKEN}\n"),
    ]


def test_start_records_the_container_before_starting_it(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman(started_replies())
    seen: list[tuple[WebkitRecord, int]] = []
    record = webkit.start_webkit(
        lambda created: seen.append((created, len(run.calls))), run,
        repository=repository, port=41234, token=TOKEN,
    )
    assert record == WebkitRecord(CONTAINER, IMAGE, 41234, TOKEN)
    assert seen == [(record, 2)]
    assert ENDPOINT.fullmatch(record.endpoint)
    run.assert_done()


def test_failed_start_is_still_reported_for_cleanup(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman([
        (("image", "inspect"), 0, f"{IMAGE}\n"),
        (("create",), 0, f"{CONTAINER}\n"),
        (("start", CONTAINER), 125, ""),
    ])
    seen: list[WebkitRecord] = []
    with pytest.raises(SessionError, match="starting the WebKit server failed"):
        webkit.start_webkit(seen.append, run, repository=repository, port=41234, token=TOKEN)
    assert seen == [WebkitRecord(CONTAINER, IMAGE, 41234, TOKEN)]


def test_container_that_stops_during_startup_fails(tmp_path: Path) -> None:
    repository = copy_image_inputs(tmp_path)
    run = ScriptedPodman(started_replies(running="false")[:4])
    with pytest.raises(SessionError, match="stopped during startup"):
        webkit.start_webkit(
            lambda created: None, run, repository=repository, port=41234, token=TOKEN,
        )


RECORD = WebkitRecord(CONTAINER, IMAGE, 41234, TOKEN)


def test_remove_accepts_an_already_absent_container() -> None:
    run = ScriptedPodman([(("container", "exists", CONTAINER), 1, "")])
    webkit.remove_webkit(RECORD, run)
    run.assert_done()


def test_remove_refuses_a_container_with_another_image() -> None:
    run = ScriptedPodman([
        (("container", "exists", CONTAINER), 0, ""),
        (("container", "inspect"), 0, f"{CONTAINER} {'9' * 64}\n"),
    ])
    with pytest.raises(SessionError, match="not the recorded WebKit server"):
        webkit.remove_webkit(RECORD, run)
    assert not any(call[0] == "rm" for call in run.calls)


def test_remove_deletes_by_full_id_and_confirms_absence() -> None:
    run = ScriptedPodman([
        (("container", "exists", CONTAINER), 0, ""),
        (("container", "inspect"), 0, f"{CONTAINER} {IMAGE}\n"),
        (("rm", "--force", "--time", "5", CONTAINER), 0, CONTAINER),
        (("container", "exists", CONTAINER), 1, ""),
    ])
    webkit.remove_webkit(RECORD, run)
    run.assert_done()


def test_running_check_requires_the_recorded_identity() -> None:
    healthy = ScriptedPodman([(("container", "inspect"), 0, f"{CONTAINER} {IMAGE} true\n")])
    assert webkit.webkit_running(RECORD, healthy)
    replaced = ScriptedPodman([(("container", "inspect"), 0, f"{CONTAINER} {'9' * 64} true")])
    assert not webkit.webkit_running(RECORD, replaced)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/deployment/test_podman_dev_webkit.py -q -p no:cacheprovider`
Expected: collection error `ImportError: cannot import name 'webkit'`.

- [ ] **Step 3: Write the Dockerfile and module**

Create `docker/dev-webkit.Dockerfile`:

```dockerfile
# Development-only Playwright WebKit server for hosts whose libraries cannot run
# Playwright's Ubuntu WebKit build (for example Fedora). Never part of a deployment.
FROM docker.io/library/node:24.20.0-bookworm-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS node

FROM mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
WORKDIR /opt/aegis-webkit
COPY frontend/package.json frontend/package-lock.json ./
RUN /usr/local/bin/node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci \
        --ignore-scripts --no-audit --no-fund \
    && /usr/local/bin/node -e 'const v = JSON.parse(require("fs").readFileSync("node_modules/@playwright/test/package.json", "utf8")).version; if (v !== "1.63.0") { console.error(v); process.exit(1); }'
USER pwuser
```

Create `scripts/podman_dev/webkit.py`:

```python
"""Pinned Playwright WebKit server container for Podman e2e runs.

Fedora cannot run Playwright's Ubuntu WebKit build, so the e2e suite connects to
this locally built server instead. Every removal is by recorded full container ID.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import socket
import time
from collections.abc import Callable
from pathlib import Path

from scripts.podman_dev import podman
from scripts.podman_dev.records import SessionError, WebkitRecord

REPOSITORY = Path(__file__).resolve().parents[2]
DOCKERFILE = Path("docker/dev-webkit.Dockerfile")
IMAGE_INPUTS = (DOCKERFILE, Path("frontend/package.json"), Path("frontend/package-lock.json"))
IMAGE_REPOSITORY = "localhost/aegis-dev-webkit"
E2E_GATEWAY_PORT = 18080
SERVER_CLI = "/opt/aegis-webkit/node_modules/@playwright/test/cli.js"
BUILD_TIMEOUT = 1800.0
FULL_ID = re.compile(r"[0-9a-f]{64}")


def image_tag(repository: Path = REPOSITORY) -> str:
    """Content-addressed tag: any change to an image input forces a rebuild."""
    digest = hashlib.sha256()
    for relative in IMAGE_INPUTS:
        digest.update(relative.as_posix().encode() + b"\0")
        digest.update((repository / relative).read_bytes() + b"\0")
    return f"{IMAGE_REPOSITORY}:{digest.hexdigest()[:16]}"


def ensure_image(run: podman.Runner = podman.run, repository: Path = REPOSITORY) -> str:
    tag = image_tag(repository)
    found = run(["image", "inspect", "--format", "{{.Id}}", tag])
    if found.returncode != 0:
        built = run(
            ["build", "--file", str(repository / DOCKERFILE), "--tag", tag, str(repository)],
            timeout=BUILD_TIMEOUT,
        )
        if built.returncode != 0:
            raise SessionError("building the WebKit image failed:\n" + built.stderr[-4000:])
        found = run(["image", "inspect", "--format", "{{.Id}}", tag])
    image_id = found.stdout.strip()
    if found.returncode != 0 or not FULL_ID.fullmatch(image_id):
        raise SessionError(f"the WebKit image {tag} has no valid image ID")
    return image_id


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def create_arguments(image_id: str, port: int, token: str) -> list[str]:
    # The name is cosmetic (removal always uses the full ID) and never carries the token.
    return [
        "create", "--name", f"aegis-dev-webkit-{port}",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--ipc", "private", "--shm-size", "256m",
        "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m,mode=1777", "--env", "HOME=/tmp",
        "--network", f"pasta:-T,{E2E_GATEWAY_PORT}",
        "--publish", f"127.0.0.1:{port}:{port}",
        image_id,
        "node", SERVER_CLI, "run-server", "--host", "0.0.0.0",
        "--port", str(port), "--path", f"/{token}",
    ]


def start_webkit(
    on_created: Callable[[WebkitRecord], None],
    run: podman.Runner = podman.run,
    *,
    repository: Path = REPOSITORY,
    port: int | None = None,
    token: str | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> WebkitRecord:
    image_id = ensure_image(run, repository)
    port = free_loopback_port() if port is None else port
    token = secrets.token_hex(16) if token is None else token
    created = run(create_arguments(image_id, port, token))
    container_id = created.stdout.strip()
    if created.returncode != 0 or not FULL_ID.fullmatch(container_id):
        raise SessionError("creating the WebKit server failed:\n" + created.stderr[-4000:])
    record = WebkitRecord(container_id=container_id, image_id=image_id, port=port, token=token)
    on_created(record)
    started = run(["start", container_id])
    if started.returncode != 0:
        raise SessionError("starting the WebKit server failed:\n" + started.stderr[-4000:])
    _wait_for_listener(record, run, sleep=sleep, now=now)
    return record


def _wait_for_listener(
    record: WebkitRecord, run: podman.Runner, *, sleep: Callable[[float], None],
    now: Callable[[], float], deadline_seconds: float = 60.0,
) -> None:
    end = now() + deadline_seconds
    while True:
        state = run(
            ["container", "inspect", "--format", "{{.State.Running}}", record.container_id]
        )
        if state.returncode != 0 or state.stdout.strip() != "true":
            raise SessionError("the WebKit server container stopped during startup; run stop")
        logs = run(["logs", record.container_id])
        if "Listening on" in logs.stdout + logs.stderr:
            return
        if now() >= end:
            raise SessionError("the WebKit server did not listen within 60 s; run stop")
        sleep(0.5)


def webkit_running(record: WebkitRecord, run: podman.Runner = podman.run) -> bool:
    state = run([
        "container", "inspect", "--format", "{{.Id}} {{.Image}} {{.State.Running}}",
        record.container_id,
    ])
    return state.returncode == 0 and state.stdout.split() == [
        record.container_id, record.image_id, "true",
    ]


def remove_webkit(record: WebkitRecord, run: podman.Runner = podman.run) -> None:
    exists = run(["container", "exists", record.container_id])
    if exists.returncode == 1:
        return
    if exists.returncode != 0:
        raise SessionError("could not tell whether the WebKit container exists; left alone")
    inspected = run(
        ["container", "inspect", "--format", "{{.Id}} {{.Image}}", record.container_id]
    )
    if inspected.returncode != 0 or inspected.stdout.split() != [
        record.container_id, record.image_id,
    ]:
        raise SessionError(
            f"container {record.container_id[:12]} is not the recorded WebKit server; "
            "left in place"
        )
    removed = run(["rm", "--force", "--time", "5", record.container_id])
    if removed.returncode != 0:
        raise SessionError("removing the WebKit container failed:\n" + removed.stderr[-2000:])
    if run(["container", "exists", record.container_id]).returncode != 1:
        raise SessionError("the WebKit container still exists after removal")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run --locked pytest tests/deployment/test_podman_dev_webkit.py -q -p no:cacheprovider`
Expected: 12 passed.

- [ ] **Step 5: Build the image once and record its ID**

Run:
```bash
uv run --locked python -c "from scripts.podman_dev import webkit; print(webkit.image_tag()); print(webkit.ensure_image())"
```
Expected: the tag and a 64-hex image ID; the build's `npm ci` succeeds and the version check passes. Record both in `$EVIDENCE/task3.md`.

- [ ] **Step 6: Static checks**

Run: `uv run --locked ruff check backend tests scripts`
Expected: clean.

- [ ] **Step 7: Commit (root session only, after reviews pass)**

```bash
git add docker/dev-webkit.Dockerfile scripts/podman_dev/webkit.py \
  tests/deployment/test_podman_dev_webkit.py
git commit -m "build: add a pinned WebKit server container for Podman e2e runs"
```

---

### Task 4: Private API service, session lifecycle and full command line

**Files:**
- Create: `scripts/podman_dev/session.py`
- Create: `scripts/podman_dev/api.py`
- Create: `scripts/podman_dev/lifecycle.py`
- Modify: `scripts/podman_dev/__main__.py` (replace entirely)
- Test: `tests/deployment/test_podman_dev_session.py`

**Interfaces:**
- Consumes: `tools.tool_path`, `tools.load_lock`, `tools.installed_state`, `tools.ToolError` (Task 1); `podman`, `records`, `inventory` (Task 2); `webkit.start_webkit`, `webkit.webkit_running`, `webkit.remove_webkit` (Task 3); doubles `ScriptedPodman`, `write_proc_entry`, `listing_replies`.
- Produces:
  - `session.STATE_NAME == "aegis-podman-dev"`, `session.BASELINE_NAME == "inventory-before.json"`, `session.LOG_NAME == "podman-api.log"`
  - `session.state_directory(environ: Mapping[str, str]) -> Path`
  - `session.preflight_problems(environ, *, euid: int, rootless: bool, selinux_enforce: str) -> list[str]`
  - `session.export_lines(record: SessionRecord, *, node_bin: Path, compose_provider: Path) -> str`
  - `api.ProcessIdentity(start_time: int, command: tuple[str, ...])`; `api.read_process_identity(pid, proc=PROC)`; `api.process_matches(record, proc=PROC) -> bool`; `api.ping(path, timeout=2.0) -> bool`; `api.launch_api(state, *, popen=subprocess.Popen, proc=PROC) -> ApiRecord`; `api.wait_until_ready(record, *, deadline_seconds=30.0, proc=PROC, probe=ping, sleep, now) -> PathIdentity`; `api.stop_api(record, *, deadline_seconds=20.0, proc=PROC, kill=os.kill, sleep, now) -> None`
  - `lifecycle.start(environ, *, home: Path, run=podman.run) -> SessionRecord`; `lifecycle.status(environ, run=podman.run) -> tuple[list[str], bool]`; `lifecycle.environment(environ, *, home: Path, run=podman.run) -> str`; `lifecycle.stop(environ, run=podman.run) -> tuple[list[str], bool]`
  - CLI exit codes: 0 success; 1 refusal or error (message on stderr, nothing on stdout); 3 `stop` completed but the pre-existing inventory changed.

- [ ] **Step 1: Write the failing tests**

Create `tests/deployment/test_podman_dev_session.py`:

```python
from __future__ import annotations

import json
import os
import signal
import socket
import stat
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest
from aegisctl.container_engine import podman_socket

from scripts.podman_dev import __main__ as cli
from scripts.podman_dev import api, lifecycle, session
from scripts.podman_dev.records import (
    RECORD_NAME,
    ApiRecord,
    PathIdentity,
    SessionError,
    SessionRecord,
    WebkitRecord,
    load_record,
    save_record,
)
from tests.support.podman_dev_doubles import ScriptedPodman, listing_replies, write_proc_entry

COMMAND = ["podman", "--remote=false", "system", "service", "--time=0", "unix:///x/podman.sock"]
CONTAINER = "c" * 64
IMAGE = "e" * 64
ROOTLESS = (("info", "--format", "{{.Host.Security.Rootless}}"), 0, "true\n")


@pytest.fixture
def runtime(tmp_path: Path) -> dict[str, str]:
    directory = tmp_path / "run"
    directory.mkdir()
    directory.chmod(0o700)
    return {"XDG_RUNTIME_DIR": str(directory)}


def state_of(runtime: dict[str, str]) -> Path:
    return Path(runtime["XDG_RUNTIME_DIR"]) / session.STATE_NAME


def api_record(tmp_path: Path, *, pid: int = 4242, start_time: int = 111) -> ApiRecord:
    directory = tmp_path / "api-dir"
    directory.mkdir(mode=0o700)
    socket_path = directory / "podman.sock"
    socket_path.write_bytes(b"")
    return ApiRecord(
        pid=pid, start_time=start_time, command=list(COMMAND), socket=str(socket_path),
        directory_identity=PathIdentity.of(directory),
        socket_identity=PathIdentity.of(socket_path),
    )


# --- process identity -----------------------------------------------------------------


def test_read_process_identity_handles_parentheses_in_the_command_name(tmp_path: Path) -> None:
    write_proc_entry(tmp_path, 7, start_time=123456, command=COMMAND)
    assert api.read_process_identity(7, tmp_path) == api.ProcessIdentity(123456, tuple(COMMAND))


def test_zombie_and_missing_processes_count_as_exited(tmp_path: Path) -> None:
    write_proc_entry(tmp_path, 7, start_time=1, command=COMMAND, state="Z")
    assert api.read_process_identity(7, tmp_path) is None
    assert api.read_process_identity(8, tmp_path) is None


# --- preflight and exports ------------------------------------------------------------


@pytest.mark.parametrize(("change", "message"), [
    ({"euid": 0}, "root"),
    ({"rootless": False}, "rootless"),
    ({"selinux_enforce": "0\n"}, "SELinux"),
    ({"remote": "CONTAINER_HOST"}, "CONTAINER_HOST"),
    ({"mode": 0o755}, "XDG_RUNTIME_DIR"),
    ({"state": True}, "run stop first"),
])
def test_preflight_refuses_unsafe_hosts(
    runtime: dict[str, str], change: dict[str, object], message: str,
) -> None:
    environ = dict(runtime)
    if "remote" in change:
        environ[str(change["remote"])] = "unix:///elsewhere"
    if "mode" in change:
        Path(runtime["XDG_RUNTIME_DIR"]).chmod(int(str(change["mode"])))
    if change.get("state"):
        state_of(runtime).mkdir()
    problems = session.preflight_problems(
        environ,
        euid=int(str(change.get("euid", os.geteuid()))),
        rootless=bool(change.get("rootless", True)),
        selinux_enforce=str(change.get("selinux_enforce", "1\n")),
    )
    assert any(message in problem for problem in problems), problems


def test_preflight_accepts_a_safe_host(runtime: dict[str, str]) -> None:
    assert session.preflight_problems(
        runtime, euid=os.geteuid(), rootless=True, selinux_enforce="1\n",
    ) == []


def full_record(socket_path: str) -> SessionRecord:
    return SessionRecord(
        state_identity=PathIdentity(1, 2),
        api=ApiRecord(
            pid=1, start_time=2, command=["podman"], socket=socket_path,
            directory_identity=PathIdentity(3, 4), socket_identity=PathIdentity(5, 6),
        ),
        webkit=WebkitRecord(CONTAINER, IMAGE, 41234, "a" * 32),
    )


def test_export_lines_quote_shell_metacharacters() -> None:
    text = session.export_lines(
        full_record("/run/user/1000/a b/$x/podman.sock"),
        node_bin=Path("/home/o'brien/node/bin"),
        compose_provider=Path("/home/x/.local/libexec/aegis/docker-compose"),
    )
    assert text == (
        "export AEGIS_CONTAINER_ENGINE=podman\n"
        "export PODMAN_COMPOSE_PROVIDER=/home/x/.local/libexec/aegis/docker-compose\n"
        "export AEGIS_PODMAN_SOCKET='/run/user/1000/a b/$x/podman.sock'\n"
        f"export E2E_WEBKIT_WS_ENDPOINT=ws://127.0.0.1:41234/{'a' * 32}\n"
        "export PATH='/home/o'\"'\"'brien/node/bin':\"$PATH\"\n"
    )


def test_export_lines_round_trip_through_bash() -> None:
    text = session.export_lines(
        full_record("/run/user/1000/a b/$x/podman.sock"),
        node_bin=Path("/home/o'brien/node/bin"),
        compose_provider=Path("/home/x/.local/libexec/aegis/docker-compose"),
    )
    shown = subprocess.run(
        ["bash", "-c", text + 'printf "%s\\n%s\\n" "$AEGIS_PODMAN_SOCKET" "${PATH%%:*}"'],
        capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"},
    )
    assert shown.stdout == "/run/user/1000/a b/$x/podman.sock\n/home/o'brien/node/bin\n"


def test_export_lines_refuse_an_incomplete_record() -> None:
    with pytest.raises(SessionError, match="incomplete"):
        session.export_lines(
            SessionRecord(state_identity=PathIdentity(1, 2)),
            node_bin=Path("/n"), compose_provider=Path("/c"),
        )


# --- API service ----------------------------------------------------------------------


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid


def test_launch_api_records_identity_and_a_private_socket_directory(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    def popen(command: Sequence[str], **options: object) -> FakeProcess:
        assert options["start_new_session"] is True
        write_proc_entry(proc, 4242, start_time=555, command=command)
        return FakeProcess(4242)

    record = api.launch_api(state, popen=popen, proc=proc)
    socket_path = Path(record.socket)
    assert record.command[:5] == ["podman", "--remote=false", "system", "service", "--time=0"]
    assert record.command[5] == f"unix://{socket_path}"
    assert (record.pid, record.start_time, record.socket_identity) == (4242, 555, None)
    assert socket_path.parent.parent == state
    assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / session.LOG_NAME).stat().st_mode) == 0o600


def test_launch_api_removes_its_directory_when_podman_is_missing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)

    def popen(command: Sequence[str], **options: object) -> FakeProcess:
        raise FileNotFoundError("podman")

    with pytest.raises(SessionError, match="could not start"):
        api.launch_api(state, popen=popen, proc=tmp_path / "proc")
    assert [path.name for path in state.iterdir()] == [session.LOG_NAME]


def test_session_socket_layout_satisfies_the_guarded_launcher() -> None:
    with tempfile.TemporaryDirectory(prefix="pd-", dir="/tmp") as base:
        runtime = Path(base) / "run"
        runtime.mkdir(mode=0o700)
        state = runtime / session.STATE_NAME
        state.mkdir(mode=0o700)
        proc = Path(base) / "proc"

        def popen(command: Sequence[str], **options: object) -> FakeProcess:
            write_proc_entry(proc, 9, start_time=1, command=command)
            return FakeProcess(9)

        record = api.launch_api(state, popen=popen, proc=proc)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(record.socket)
            accepted = podman_socket({"AEGIS_PODMAN_SOCKET": record.socket})
        assert accepted == Path(record.socket)


def test_wait_until_ready_fails_when_the_process_exits(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    with pytest.raises(SessionError, match="exited during startup"):
        api.wait_until_ready(record, proc=tmp_path / "proc", probe=lambda path: True)


def test_wait_until_ready_returns_the_socket_identity(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    write_proc_entry(tmp_path / "proc", 4242, start_time=111, command=COMMAND)
    identity = api.wait_until_ready(record, proc=tmp_path / "proc", probe=lambda path: True)
    assert identity == PathIdentity.of(Path(record.socket))


def test_stop_api_refuses_a_reused_pid(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    write_proc_entry(tmp_path / "proc", 4242, start_time=999, command=COMMAND)
    signals: list[tuple[int, int]] = []
    with pytest.raises(SessionError, match="no longer matches"):
        api.stop_api(record, proc=tmp_path / "proc",
                     kill=lambda pid, number: signals.append((pid, number)))
    assert signals == []
    assert Path(record.socket).exists()


def test_stop_api_terminates_then_removes_recorded_entries(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    proc = tmp_path / "proc"
    write_proc_entry(proc, 4242, start_time=111, command=COMMAND)
    signals: list[tuple[int, int]] = []

    def kill(pid: int, number: int) -> None:
        signals.append((pid, number))
        for entry in (proc / str(pid)).iterdir():
            entry.unlink()
        (proc / str(pid)).rmdir()

    api.stop_api(record, proc=proc, kill=kill, sleep=lambda seconds: None)
    assert signals == [(4242, signal.SIGTERM)]
    assert not Path(record.socket).parent.exists()


def test_stop_api_recovers_when_process_is_already_gone(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    signals: list[tuple[int, int]] = []
    api.stop_api(record, proc=tmp_path / "proc",
                 kill=lambda pid, number: signals.append((pid, number)))
    assert signals == []
    assert not Path(record.socket).parent.exists()


def test_stop_api_refuses_a_replaced_socket(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    socket_path = Path(record.socket)
    socket_path.unlink()
    (socket_path.parent / "keep-inode-busy").write_bytes(b"")
    socket_path.write_bytes(b"replacement")
    with pytest.raises(SessionError, match="not the recorded socket"):
        api.stop_api(record, proc=tmp_path / "proc")
    assert socket_path.read_bytes() == b"replacement"


def test_stop_api_leaves_a_process_that_ignores_sigterm(tmp_path: Path) -> None:
    record = api_record(tmp_path)
    write_proc_entry(tmp_path / "proc", 4242, start_time=111, command=COMMAND)
    clock = iter([0.0, 0.0, 25.0])
    with pytest.raises(SessionError, match="ignored SIGTERM"):
        api.stop_api(record, proc=tmp_path / "proc", kill=lambda pid, number: None,
                     sleep=lambda seconds: None, now=lambda: next(clock))
    assert Path(record.socket).exists()


# --- lifecycle ------------------------------------------------------------------------


def allow_host(monkeypatch: pytest.MonkeyPatch, *, selinux: str = "1\n") -> None:
    monkeypatch.setattr(lifecycle, "selinux_enforce", lambda: selinux)
    monkeypatch.setattr(lifecycle.tools, "installed_state", lambda tool, home: "verified")


def write_session(
    runtime: dict[str, str], record_api: ApiRecord | None = None,
    record_webkit: WebkitRecord | None = None, baseline: object = None,
) -> Path:
    state = state_of(runtime)
    state.mkdir(mode=0o700)
    save_record(state, SessionRecord(
        state_identity=PathIdentity.of(state), api=record_api, webkit=record_webkit,
    ))
    empty = {"containers": {}, "volumes": {}, "networks": {}, "images": {}}
    (state / session.BASELINE_NAME).write_text(json.dumps(baseline or empty), encoding="utf-8")
    return state


def test_start_refuses_on_a_failed_preflight_and_creates_nothing(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    allow_host(monkeypatch, selinux="0\n")
    with pytest.raises(SessionError, match="SELinux"):
        lifecycle.start(runtime, home=tmp_path, run=ScriptedPodman([ROOTLESS]))
    assert not state_of(runtime).exists()


def test_start_refuses_when_state_exists_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    allow_host(monkeypatch)
    state_of(runtime).mkdir(mode=0o700)
    (state_of(runtime) / RECORD_NAME).write_text("sentinel", encoding="utf-8")
    with pytest.raises(SessionError, match="run stop first"):
        lifecycle.start(runtime, home=tmp_path, run=ScriptedPodman([ROOTLESS]))
    assert (state_of(runtime) / RECORD_NAME).read_text(encoding="utf-8") == "sentinel"


def test_start_keeps_a_record_when_the_api_launch_fails(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    allow_host(monkeypatch)

    def failing_launch(state: Path) -> ApiRecord:
        assert (state / RECORD_NAME).is_file()
        raise SessionError("launch failed")

    monkeypatch.setattr(lifecycle.api, "launch_api", failing_launch)
    run = ScriptedPodman([ROOTLESS, *listing_replies()])
    with pytest.raises(SessionError, match="launch failed"):
        lifecycle.start(runtime, home=tmp_path, run=run)
    record = load_record(state_of(runtime))
    assert (record.api, record.webkit) == (None, None)
    assert (state_of(runtime) / session.BASELINE_NAME).is_file()
    assert stat.S_IMODE(state_of(runtime).stat().st_mode) == 0o700


def test_stop_removes_webkit_before_api_and_cleans_state(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    order: list[str] = []
    monkeypatch.setattr(lifecycle.webkit, "remove_webkit",
                        lambda record, run: order.append("webkit"))
    monkeypatch.setattr(lifecycle.api, "stop_api", lambda record: order.append("api"))
    write_session(runtime, api_record(tmp_path), WebkitRecord(CONTAINER, IMAGE, 41234, "a" * 32))
    report, unchanged = lifecycle.stop(runtime, run=ScriptedPodman(listing_replies()))
    assert order == ["webkit", "api"]
    assert unchanged is True
    assert "inventory: no pre-existing Podman object removed or changed" in report
    assert not state_of(runtime).exists()


def test_stop_reports_changed_inventory_and_still_cleans_up(runtime: dict[str, str]) -> None:
    baseline = {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1]},
        "volumes": {}, "networks": {}, "images": {},
    }
    write_session(runtime, baseline=baseline)
    report, unchanged = lifecycle.stop(runtime, run=ScriptedPodman(listing_replies()))
    assert unchanged is False
    assert any(CONTAINER in line and "disappeared" in line for line in report)
    assert not state_of(runtime).exists()


def test_stop_refuses_a_replaced_state_directory(runtime: dict[str, str]) -> None:
    state = write_session(runtime)
    save_record(state, SessionRecord(state_identity=PathIdentity(0, 0)))
    with pytest.raises(SessionError, match="replaced"):
        lifecycle.stop(runtime, run=ScriptedPodman([]))
    assert (state / RECORD_NAME).is_file()


def test_stop_refuses_unexpected_state_entries(runtime: dict[str, str]) -> None:
    state = write_session(runtime)
    (state / "notes.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(SessionError, match="unexpected entries"):
        lifecycle.stop(runtime, run=ScriptedPodman(listing_replies()))
    assert (state / "notes.txt").read_text(encoding="utf-8") == "keep"


def test_environment_refuses_a_dead_session(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str], tmp_path: Path,
) -> None:
    write_session(runtime, api_record(tmp_path), WebkitRecord(CONTAINER, IMAGE, 41234, "a" * 32))
    monkeypatch.setattr(lifecycle.api, "process_matches", lambda record: False)
    run = ScriptedPodman([(("container", "inspect"), 0, f"{CONTAINER} {IMAGE} true")])
    with pytest.raises(SessionError, match="no healthy session"):
        lifecycle.environment(runtime, home=tmp_path, run=run)


def test_cli_env_without_session_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, runtime: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", runtime["XDG_RUNTIME_DIR"])
    assert cli.main(["env"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no healthy session" in captured.err
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --locked pytest tests/deployment/test_podman_dev_session.py -q -p no:cacheprovider`
Expected: collection error `ImportError: cannot import name 'api'`.

- [ ] **Step 3: Write `session.py`**

```python
"""State paths, preflight and shell exports for one Podman development session."""
from __future__ import annotations

import os
import shlex
import stat
from collections.abc import Mapping
from pathlib import Path

from scripts.podman_dev.records import SessionError, SessionRecord

STATE_NAME = "aegis-podman-dev"
BASELINE_NAME = "inventory-before.json"
LOG_NAME = "podman-api.log"
REMOTE_VARIABLES = ("DOCKER_HOST", "DOCKER_CONTEXT", "CONTAINER_HOST", "CONTAINER_CONNECTION")


def state_directory(environ: Mapping[str, str]) -> Path:
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    if not runtime or not Path(runtime).is_absolute():
        raise SessionError("XDG_RUNTIME_DIR must be set to an absolute path")
    return Path(runtime) / STATE_NAME


def preflight_problems(
    environ: Mapping[str, str], *, euid: int, rootless: bool, selinux_enforce: str,
) -> list[str]:
    problems: list[str] = []
    if euid == 0:
        problems.append("refusing to run as root")
    if not rootless:
        problems.append("Podman must run rootless")
    if selinux_enforce.strip() != "1":
        problems.append("SELinux must be enforcing")
    remote = [name for name in REMOTE_VARIABLES if name in environ]
    if remote:
        problems.append("remote engine variables are set: " + ", ".join(remote))
    runtime = environ.get("XDG_RUNTIME_DIR", "")
    try:
        metadata = os.lstat(runtime) if runtime and Path(runtime).is_absolute() else None
    except OSError:
        metadata = None
    if (
        metadata is None
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != euid
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        problems.append("XDG_RUNTIME_DIR must be an absolute private (0700) directory you own")
    elif os.path.lexists(Path(runtime) / STATE_NAME):
        problems.append(f"a previous session exists in {Path(runtime) / STATE_NAME}; "
                        "run stop first")
    return problems


def export_lines(record: SessionRecord, *, node_bin: Path, compose_provider: Path) -> str:
    if record.api is None or record.webkit is None:
        raise SessionError("the session is incomplete; run stop, then start")
    values = [
        ("AEGIS_CONTAINER_ENGINE", "podman"),
        ("PODMAN_COMPOSE_PROVIDER", str(compose_provider)),
        ("AEGIS_PODMAN_SOCKET", record.api.socket),
        ("E2E_WEBKIT_WS_ENDPOINT", record.webkit.endpoint),
    ]
    lines = [f"export {name}={shlex.quote(value)}" for name, value in values]
    lines.append(f'export PATH={shlex.quote(str(node_bin))}:"$PATH"')
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Write `api.py`**

```python
"""The private foreground Podman API service and its exact process identity."""
from __future__ import annotations

import contextlib
import http.client
import os
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scripts.podman_dev.records import ApiRecord, PathIdentity, SessionError
from scripts.podman_dev.session import LOG_NAME

PROC = Path("/proc")
SOCKET_NAME = "podman.sock"
LOG_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW


@dataclass(frozen=True)
class ProcessIdentity:
    start_time: int
    command: tuple[str, ...]


def read_process_identity(pid: int, proc: Path = PROC) -> ProcessIdentity | None:
    """Start time and command of a live process, or None once it has exited."""
    try:
        status = (proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        raw_command = (proc / str(pid) / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # The command name may contain spaces or parentheses; fields resume after the
    # last ")". Field 3 (state) is index 0 and field 22 (start time) is index 19.
    fields = status[status.rindex(")") + 2 :].split()
    if fields[0] in ("Z", "X"):
        return None
    command = tuple(
        part.decode("utf-8", "surrogateescape") for part in raw_command.split(b"\0") if part
    )
    return ProcessIdentity(int(fields[19]), command)


def recorded_identity(record: ApiRecord) -> ProcessIdentity:
    return ProcessIdentity(record.start_time, tuple(record.command))


def process_matches(record: ApiRecord, proc: Path = PROC) -> bool:
    return read_process_identity(record.pid, proc) == recorded_identity(record)


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float) -> None:
        super().__init__("localhost", timeout=timeout)
        self._socket_path = str(path)

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(self._socket_path)
        self.sock = connection


def ping(path: Path, timeout: float = 2.0) -> bool:
    connection = _UnixHTTPConnection(path, timeout)
    try:
        connection.request("GET", "/_ping")
        response = connection.getresponse()
        return response.status == 200 and response.read().strip() == b"OK"
    except OSError:
        return False
    finally:
        connection.close()


def launch_api(
    state: Path, *, popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    proc: Path = PROC,
) -> ApiRecord:
    """Start the service and return its identity at once, before it is ready."""
    directory = Path(tempfile.mkdtemp(prefix="api-", dir=state))
    socket_path = directory / SOCKET_NAME
    command = [
        "podman", "--remote=false", "system", "service", "--time=0", f"unix://{socket_path}",
    ]
    log = os.open(state / LOG_NAME, LOG_FLAGS, 0o600)
    try:
        process = popen(
            command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )
    except OSError as error:
        directory.rmdir()
        raise SessionError(f"could not start the Podman API service: {error}") from error
    finally:
        os.close(log)
    identity = read_process_identity(process.pid, proc)
    if identity is None:
        directory.rmdir()
        raise SessionError(f"the Podman API service exited at once; see {state / LOG_NAME}")
    return ApiRecord(
        pid=process.pid, start_time=identity.start_time, command=list(identity.command),
        socket=str(socket_path), directory_identity=PathIdentity.of(directory),
    )


def wait_until_ready(
    record: ApiRecord, *, deadline_seconds: float = 30.0, proc: Path = PROC,
    probe: Callable[[Path], bool] = ping, sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> PathIdentity:
    socket_path = Path(record.socket)
    end = now() + deadline_seconds
    while True:
        if not process_matches(record, proc):
            raise SessionError(
                "the Podman API service exited during startup; see its log, then run stop"
            )
        if socket_path.exists() and probe(socket_path):
            return PathIdentity.of(socket_path)
        if now() >= end:
            raise SessionError(
                f"the Podman API service was not ready within {deadline_seconds:g} s; run stop"
            )
        sleep(0.2)


def stop_api(
    record: ApiRecord, *, deadline_seconds: float = 20.0, proc: Path = PROC,
    kill: Callable[[int, int], None] = os.kill, sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> None:
    expected = recorded_identity(record)
    current = read_process_identity(record.pid, proc)
    if current is not None:
        if current != expected:
            raise SessionError(
                f"process {record.pid} no longer matches the recorded Podman API service; "
                "nothing was signalled"
            )
        with contextlib.suppress(ProcessLookupError):
            kill(record.pid, signal.SIGTERM)
        end = now() + deadline_seconds
        while read_process_identity(record.pid, proc) == expected:
            if now() >= end:
                raise SessionError(
                    f"the Podman API service ignored SIGTERM for {deadline_seconds:g} s; "
                    "it was left running"
                )
            sleep(0.2)
    _remove_socket_entries(record)


def _remove_socket_entries(record: ApiRecord) -> None:
    socket_path = Path(record.socket)
    directory = socket_path.parent
    if os.path.lexists(socket_path):
        recorded = record.socket_identity
        if recorded is None or PathIdentity.of(socket_path) != recorded:
            raise SessionError(f"{socket_path} is not the recorded socket; left in place")
        socket_path.unlink()
    if os.path.lexists(directory):
        if PathIdentity.of(directory) != record.directory_identity:
            raise SessionError(
                f"{directory} is not the recorded socket directory; left in place"
            )
        if any(directory.iterdir()):
            raise SessionError(f"{directory} has unexpected contents; left in place")
        directory.rmdir()
```

- [ ] **Step 5: Write `lifecycle.py`**

```python
"""Start, check and stop one helper session; every removal is by recorded identity."""
from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from scripts.podman_dev import api, inventory, podman, session, tools, webkit
from scripts.podman_dev.records import (
    RECORD_NAME,
    TEMPORARY_NAME,
    PathIdentity,
    SessionError,
    SessionRecord,
    WebkitRecord,
    load_record,
    save_record,
)

SELINUX_ENFORCE = Path("/sys/fs/selinux/enforce")
STATE_ENTRIES = frozenset({
    RECORD_NAME, TEMPORARY_NAME, session.BASELINE_NAME, session.LOG_NAME,
})


def selinux_enforce(path: Path = SELINUX_ENFORCE) -> str:
    try:
        return path.read_text(encoding="ascii")
    except OSError:
        return ""


def podman_rootless(run: podman.Runner) -> bool:
    result = run(["info", "--format", "{{.Host.Security.Rootless}}"])
    return result.returncode == 0 and result.stdout.strip() == "true"


def _write_private_json(path: Path, value: object) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True)


def start(
    environ: Mapping[str, str], *, home: Path, run: podman.Runner = podman.run,
) -> SessionRecord:
    problems = session.preflight_problems(
        environ, euid=os.geteuid(), rootless=podman_rootless(run),
        selinux_enforce=selinux_enforce(),
    )
    for name in ("node", "docker-compose"):
        tool = tools.load_lock()[name]
        if tools.installed_state(tool, home) != "verified":
            problems.append(f"{name} {tool.version} is not installed and verified; "
                            "run the tools command first")
    if problems:
        raise SessionError("\n".join(problems))
    state = session.state_directory(environ)
    try:
        state.mkdir(mode=0o700)
    except FileExistsError as error:
        raise SessionError(f"a previous session exists in {state}; run stop first") from error
    state.chmod(0o700)
    record = SessionRecord(state_identity=PathIdentity.of(state))
    save_record(state, record)
    _write_private_json(state / session.BASELINE_NAME, inventory.snapshot(run))
    record.api = api.launch_api(state)
    save_record(state, record)
    record.api.socket_identity = api.wait_until_ready(record.api)
    save_record(state, record)

    def remember(created: WebkitRecord) -> None:
        record.webkit = created
        save_record(state, record)

    webkit.start_webkit(remember, run)
    return record


def _open_session(environ: Mapping[str, str]) -> tuple[Path, SessionRecord] | None:
    state = session.state_directory(environ)
    if not os.path.lexists(state):
        return None
    metadata = state.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise SessionError(f"{state} is not a private directory you own; nothing was changed")
    if not (state / RECORD_NAME).is_file():
        raise SessionError(f"{state} has no session record; inspect it by hand; "
                           "nothing was changed")
    record = load_record(state)
    if PathIdentity.of(state) != record.state_identity:
        raise SessionError(f"{state} was replaced since the session started; nothing was changed")
    return state, record


def _check(
    environ: Mapping[str, str], run: podman.Runner,
) -> tuple[list[str], bool, SessionRecord | None]:
    opened = _open_session(environ)
    if opened is None:
        return ["no session"], False, None
    record = opened[1]
    lines: list[str] = []
    healthy = True
    socket_path = Path(record.api.socket) if record.api else None
    if record.api is None or record.api.socket_identity is None or socket_path is None:
        lines.append("api: not started")
        healthy = False
    elif (
        api.process_matches(record.api)
        and os.path.lexists(socket_path)
        and PathIdentity.of(socket_path) == record.api.socket_identity
        and api.ping(socket_path)
    ):
        lines.append(f"api: ready pid={record.api.pid} socket={record.api.socket}")
    else:
        lines.append("api: NOT RUNNING (run stop, then start)")
        healthy = False
    if record.webkit is None:
        lines.append("webkit: not started")
        healthy = False
    elif webkit.webkit_running(record.webkit, run):
        lines.append(f"webkit: ready ws://127.0.0.1:{record.webkit.port}/<token>")
    else:
        lines.append("webkit: NOT RUNNING (run stop, then start)")
        healthy = False
    return lines, healthy, record


def status(environ: Mapping[str, str], run: podman.Runner = podman.run) -> tuple[list[str], bool]:
    lines, healthy, _record = _check(environ, run)
    return lines, healthy


def environment(
    environ: Mapping[str, str], *, home: Path, run: podman.Runner = podman.run,
) -> str:
    lines, healthy, record = _check(environ, run)
    if not healthy or record is None:
        raise SessionError("no healthy session:\n" + "\n".join(lines))
    return session.export_lines(
        record, node_bin=tools.tool_path("node", home) / "bin",
        compose_provider=tools.tool_path("docker-compose", home),
    )


def stop(environ: Mapping[str, str], run: podman.Runner = podman.run) -> tuple[list[str], bool]:
    """Stop everything recorded; return the report and whether pre-existing objects held."""
    opened = _open_session(environ)
    if opened is None:
        return ["no session to stop"], True
    state, record = opened
    report: list[str] = []
    if record.webkit is not None:
        webkit.remove_webkit(record.webkit, run)
        record.webkit = None
        save_record(state, record)
        report.append("webkit: removed")
    if record.api is not None:
        api.stop_api(record.api)
        record.api = None
        save_record(state, record)
        report.append("api: stopped")
    unchanged = True
    baseline = state / session.BASELINE_NAME
    if baseline.is_file():
        before = json.loads(baseline.read_text(encoding="utf-8"))
        problems = inventory.compare(before, inventory.snapshot(run))
        unchanged = not problems
        report += problems or ["inventory: no pre-existing Podman object removed or changed"]
    else:
        report.append("inventory: no baseline was recorded")
    unexpected = sorted({entry.name for entry in state.iterdir()} - STATE_ENTRIES)
    if unexpected:
        raise SessionError(f"{state} holds unexpected entries {unexpected}; left in place")
    for name in sorted(STATE_ENTRIES - {RECORD_NAME}):
        (state / name).unlink(missing_ok=True)
    (state / RECORD_NAME).unlink()
    state.rmdir()
    return report, unchanged
```

- [ ] **Step 6: Replace `__main__.py`**

```python
"""Command line for the Podman development helper: python -m scripts.podman_dev."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from scripts.podman_dev import lifecycle, tools
from scripts.podman_dev.records import SessionError

LOAD_HINT = 'eval "$(uv run --locked python -m scripts.podman_dev env)"'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.podman_dev",
        description="Private rootless Podman development session for Aegis.",
    )
    parser.add_argument("command", choices=("tools", "start", "env", "status", "stop"))
    command = parser.parse_args(argv).command
    home = Path.home()
    try:
        if command == "tools":
            print("\n".join(tools.ensure_tools(home)))
        elif command == "start":
            lifecycle.start(os.environ, home=home)
            print(f"Session started. Load it with:\n  {LOAD_HINT}")
        elif command == "env":
            sys.stdout.write(lifecycle.environment(os.environ, home=home))
        elif command == "status":
            lines, healthy = lifecycle.status(os.environ)
            print("\n".join(lines))
            return 0 if healthy else 1
        else:
            report, unchanged = lifecycle.stop(os.environ)
            print("\n".join(report))
            return 0 if unchanged else 3
    except (SessionError, tools.ToolError) as error:
        print(f"podman_dev: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run all helper tests**

Run:
```bash
uv run --locked pytest tests/deployment/test_podman_dev_tools.py \
  tests/deployment/test_podman_dev_records.py tests/deployment/test_podman_dev_webkit.py \
  tests/deployment/test_podman_dev_session.py -q -p no:cacheprovider
```
Expected: all pass. If `test_session_socket_layout_satisfies_the_guarded_launcher` fails because `/tmp` paths exceed the 108-byte Unix socket limit, shorten only that test's prefix; do not change `launch_api`.

- [ ] **Step 8: Static checks**

Run: `uv run --locked ruff check backend tests scripts && uv run --locked mypy backend`
Expected: clean.

- [ ] **Step 9: Commit (root session only, after reviews pass)**

```bash
git add scripts/podman_dev/session.py scripts/podman_dev/api.py \
  scripts/podman_dev/lifecycle.py scripts/podman_dev/__main__.py \
  tests/deployment/test_podman_dev_session.py
git commit -m "feat: add a private Podman API and WebKit development session helper"
```

---

### Task 5: Real smoke run and pasta reachability measurement (operational)

**Files:** none tracked unless a defect is found (then fix it with a failing test first, inside the owning module, and commit separately).

**Interfaces:**
- Consumes: the full CLI (Task 4).
- Produces: measured evidence that the session works on this host, recorded in `$EVIDENCE/task5.md`.

- [ ] **Step 1: Preconditions**

Run:
```bash
ss -ltnH '( sport = :18080 or sport = :55432 )'
df --output=avail -BG / | tail -1
podman ps -a --format '{{.Names}} {{.Status}}' | sort > "$EVIDENCE/containers-before.txt"
wc -l < "$EVIDENCE/containers-before.txt"
```
Expected: no `ss` output; at least `40G` free; the host's existing containers recorded.

- [ ] **Step 2: Start, check and load the session**

Run:
```bash
uv run --locked python -m scripts.podman_dev start
uv run --locked python -m scripts.podman_dev status
eval "$(uv run --locked python -m scripts.podman_dev env)"
node --version
"$PODMAN_COMPOSE_PROVIDER" version --short
podman --remote --url "unix://$AEGIS_PODMAN_SOCKET" version --format '{{.Server.Version}}'
```
Expected: `status` exits 0 with `api: ready …` and `webkit: ready …`; `v24.20.0`; `2.39.4`; `5.8.7`.

- [ ] **Step 3: Confirm the recorded process owns the socket and the container is locked down**

Run:
```bash
pid=$(python3 -c "import json,os; print(json.load(open(os.environ['XDG_RUNTIME_DIR']+'/aegis-podman-dev/session.json'))['api']['pid'])")
ss -xlpH | grep -F "$AEGIS_PODMAN_SOCKET"
cid=$(python3 -c "import json,os; print(json.load(open(os.environ['XDG_RUNTIME_DIR']+'/aegis-podman-dev/session.json'))['webkit']['container_id'])")
podman container inspect "$cid" --format \
  'ro={{.HostConfig.ReadonlyRootfs}} capdrop={{.HostConfig.CapDrop}} sec={{.HostConfig.SecurityOpt}} net={{.HostConfig.NetworkMode}} mounts={{len .Mounts}} user={{.Config.User}} mem={{.HostConfig.Memory}} pids={{.HostConfig.PidsLimit}}'
```
Expected: the `ss` line shows `pid=$pid`; inspect shows `ro=true`, all capabilities dropped (Podman lists them expanded), `no-new-privileges`, `net=pasta`, `mounts=0`, `user=pwuser`, memory 2147483648, pids 256. If the socket is owned by a different PID than recorded (for example because rootless Podman re-executed into a child), stop and report: `stop` would then signal the wrong process.

- [ ] **Step 4: Measure pasta reachability (spec section 4.4)**

Run:
```bash
probe=$(mktemp -d)
printf '<title>aegis-webkit-probe</title>' > "$probe/index.html"
python3 -m http.server 18080 --bind 127.0.0.1 --directory "$probe" >/dev/null 2>&1 & allowed=$!
python3 -m http.server 18081 --bind 127.0.0.1 --directory "$probe" >/dev/null 2>&1 & other=$!
sleep 1
(cd frontend && node --input-type=module -e '
import {webkit} from "@playwright/test";
const browser = await webkit.connect(process.env.E2E_WEBKIT_WS_ENDPOINT);
const page = await browser.newPage();
await page.goto("http://127.0.0.1:18080/");
console.log("allowed:", await page.title());
try {
  await page.goto("http://127.0.0.1:18081/", {timeout: 5000});
  console.log("other: REACHABLE");
} catch { console.log("other: refused"); }
await browser.close();')
kill "$allowed" "$other"; rm -r "$probe"
```
Expected: `allowed: aegis-webkit-probe` and `other: refused`. If the allowed page is unreachable, **stop and ask the user** before considering host networking (spec section 4.4). If `other` is reachable, stop and report: the container could reach unrelated host loopback services.

- [ ] **Step 5: Stop and compare**

Run:
```bash
uv run --locked python -m scripts.podman_dev stop; echo "exit=$?"
test ! -e "$XDG_RUNTIME_DIR/aegis-podman-dev" && echo "state removed"
podman ps -a --format '{{.Names}} {{.Status}}' | sort | cut -d' ' -f1 > "$EVIDENCE/after-names.txt"
cut -d' ' -f1 "$EVIDENCE/containers-before.txt" | diff - "$EVIDENCE/after-names.txt" && echo "same containers"
```
Expected: `webkit: removed`, `api: stopped`, `inventory: no pre-existing Podman object removed or changed`, `exit=0`, `state removed`, `same containers`.

- [ ] **Step 6: Record evidence**

Write `$EVIDENCE/task5.md` with: the commands above, their key outputs, the WebKit image ID, the measured pasta result, and the socket ownership check.

---

### Task 6: `make verify` under Podman (triage loop)

**Files:** determined by failures; each fix follows the triage policy below.

**Interfaces:**
- Consumes: a running, loaded session (Task 5 Step 2).
- Produces: a passing `make verify` log in `$EVIDENCE/`, and one commit per distinct cause.

Triage policy (applies to Tasks 6, 8 and 9; spec section 5.3):

| Class | Fix |
| --- | --- |
| Stale Docker-only test expectation | Pin the exact per-engine form, as `expected_security_options()` in `tests/deployment/test_compose.py` does |
| Real Podman runtime difference | Fix in `compose.podman.yaml` or engine-routed harness code (`selected_engine() == "podman"` branches), never weakening a protection |
| O11 (client source address lost) | Measure and confirm, then list the test ID in the O11 exception list (Task 8) |
| Product bug that also affects Docker | Fix with a regression test |

Never use as a fix: `pytest.skip`, `xfail`, relaxed assertions, `:z`/`:Z`/`:U` on originals, `label=disable`, `--privileged`, rootful Podman, or name/label/prefix cleanup. Host-level or trust-model changes need user approval first. If a failure reveals that a harness needs restructuring, stop and report to the root session, which re-scopes with the user.

- [ ] **Step 1: Preconditions**

Run:
```bash
ss -ltnH '( sport = :18080 or sport = :55432 )'
df --output=avail -BG / | tail -1
uv run --locked python -m scripts.podman_dev status || uv run --locked python -m scripts.podman_dev start
eval "$(uv run --locked python -m scripts.podman_dev env)"
```
Expected: no listeners, at least 40G, healthy status.

- [ ] **Step 2: Run the gate**

Run:
```bash
log="$EVIDENCE/make-verify-$(date +%Y%m%dT%H%M%S).log"
make verify 2>&1 | tee "$log"; echo "exit=${PIPESTATUS[0]}"
grep -E '^(FAILED|ERROR) |passed|failed' "$log" | tail -20
```
Expected on success: `exit=0`, backend and frontend summaries with zero failures. On failure, continue with Step 3.

- [ ] **Step 3: Reproduce each failure narrowly**

For each `FAILED`/`ERROR` line `path::test`:
```bash
uv run --locked python scripts/verify.py backend --test-target <path>::<test_function>
```
(Use `deployment` mode for paths under `tests/deployment/`; selectors are identifier-only, so select the function, not a bracketed parameter.) For frontend failures: `npm --prefix frontend test -- <file>`. Record the error excerpt in `$EVIDENCE/task6-triage.md`.

- [ ] **Step 4: Fix per class, one cause per change**

For each cause: write or adjust the failing test to pin the correct per-engine behavior, make the minimal fix, re-run the narrow selection until it passes, run Ruff and mypy, then hand the change to review. Root session commits each cause separately, for example:
```bash
git commit -m "test: pin the Podman form of <assertion> in <file>"
```

- [ ] **Step 5: Re-run the full gate**

Repeat Step 2 until `exit=0`. Record final counts and duration in `$EVIDENCE/task6.md`.

---

### Task 7: Stale core-services NNP assertion

**Files:**
- Modify: `tests/deployment/test_compose.py:340`

**Interfaces:**
- Consumes: `expected_security_options()` already defined at `tests/deployment/test_compose.py:56`.
- Produces: nothing new.

- [ ] **Step 1: Reproduce the failure under Podman**

Run (session loaded):
```bash
uv run --locked python scripts/verify.py deployment \
  --test-target tests/deployment/test_compose.py::test_core_services_are_unprivileged_and_postgres_is_private
```
Expected: FAIL; the assertion shows `['no-new-privileges:true', 'unmask=/sys/devices/virtual/powercap']` against `['no-new-privileges:true']`.

- [ ] **Step 2: Fix the stale literal**

In `tests/deployment/test_compose.py`, change line 340 from:
```python
        assert services[name]["security_opt"] == ["no-new-privileges:true"]
```
to:
```python
        assert services[name]["security_opt"] == expected_security_options()
```
`expected_security_options()` returns the Docker literal under Docker, so Docker CI keeps the identical assertion.

- [ ] **Step 3: Verify**

Run the Step 1 command again.
Expected: PASS. Then `uv run --locked ruff check backend tests scripts`: clean.

- [ ] **Step 4: Commit (root session only, after reviews pass)**

```bash
git add tests/deployment/test_compose.py
git commit -m "test: expect the engine-specific security options for core services"
```

---

### Task 8: `make verify-compose` under Podman (triage, known items, O11 list)

**Files:** determined by failures. Known candidates: `tests/deployment/test_rendered_mounts.py`, `tests/deployment/test_database_roles.py:414-418`.

**Interfaces:**
- Consumes: Task 6 triage policy; Task 7 fix.
- Produces: a `make verify-compose` log whose only failures are listed O11 test IDs, the O11 exception list in `$EVIDENCE/o11.md`, and one commit per cause.

- [ ] **Step 1: Preconditions** — same commands as Task 6 Step 1.

- [ ] **Step 2: Run the gate**

Run:
```bash
log="$EVIDENCE/make-verify-compose-$(date +%Y%m%dT%H%M%S).log"
make verify-compose 2>&1 | tee "$log"; echo "exit=${PIPESTATUS[0]}"
grep -E '^(FAILED|ERROR) ' "$log" | sort | uniq > "$EVIDENCE/verify-compose-failures.txt"
wc -l < "$EVIDENCE/verify-compose-failures.txt"
podman ps -a --format '{{.Names}}' | sort | diff "$EVIDENCE/after-names.txt" - && echo "no leftover containers"
```
Expected: a failure list; `no leftover containers` (harness cleanup is exact). If containers remain, record their identities and do not remove them by hand.

- [ ] **Step 3: `test_rendered_mounts.py` refusals**

For each listed `test_rendered_mounts.py` failure, reproduce it with `scripts/verify.py deployment --test-target tests/deployment/test_rendered_mounts.py::<test_function>`, record the exact refusal text, and classify:
- An expectation that hard-codes Docker's security options or mask list: route it through the per-engine helper, as in Task 7.
- An observer or mask refusal raised by `backend/aegisctl/podman_mask_compatibility.py` or `backend/aegisctl/mounts.py`: the guard is correct by default. Identify which measured behavior differs on this host (Fedora Server on xfs versus the Silverblue/Btrfs evidence) and report it to the root session before changing guard code; guard changes need explicit review.

- [ ] **Step 4: `test_database_roles.py:414` host networking**

Reproduce:
```bash
uv run --locked python scripts/verify.py deployment \
  --test-target tests/deployment/test_database_roles.py::<function containing line 414>
```
(Find the function name with `awk 'NR<=414 && /^def test_/ {name=$2} END {print name}' tests/deployment/test_database_roles.py`.)
If it passes, record "no change needed" and leave the code alone. If it fails on mountinfo duplication or host-network behavior, apply the indexer's measured fix. Replace:
```python
        host = "host.docker.internal" if sys.platform == "darwin" else "127.0.0.1"
        network = [] if sys.platform == "darwin" else ["--network", "host"]
```
with:
```python
        host = "host.docker.internal" if sys.platform == "darwin" else "127.0.0.1"
        if sys.platform == "darwin":
            network = []
        elif selected_engine() == "podman":
            # Rootless Podman host networking rbinds the host /sys (root O12); pasta -T
            # forwards only the database's loopback port into the container (O13).
            network = ["--network", f"pasta:-T,{role_database.port}"]
        else:
            network = ["--network", "host"]
```
and extend the import to `from aegisctl.container_engine import container_command, selected_engine`. Re-run until it passes; commit as `test: route the installed-wheel role check through pasta on Podman`.

- [ ] **Step 5: Attribute O11 failures**

For each remaining failure in `tests/deployment/test_tls_gateway.py`:
1. Confirm the failure is the gateway's readiness rejection or a client-address mismatch (the anti-spoofing branch at `deploy/nginx/nginx.conf:58` returns 400 when the forwarded address equals Caddy's peer address). Record the exact assertion excerpt.
2. Re-run the single test three times with `--test-target`; record pass/fail per attempt to show intermittency.
3. Append to `$EVIDENCE/o11.md`: test ID, assertion excerpt, the three outcomes.
A failure that is not this mechanism is not O11: triage it under the policy instead.

- [ ] **Step 6: Unknown failures**

Triage every remaining entry under the Task 6 policy, one commit per cause, with narrow reproduction first.

- [ ] **Step 7: Re-run the full gate**

Repeat Step 2 until the only failures are the O11 IDs in `$EVIDENCE/o11.md`, with zero skips added. Record counts, duration and the final list in `$EVIDENCE/task8.md`.

---

### Task 9: `make test-e2e` under Podman (triage loop)

**Files:** determined by failures.

**Interfaces:**
- Consumes: loaded session including `E2E_WEBKIT_WS_ENDPOINT`; Task 6 triage policy.
- Produces: a 22/22 e2e log and one commit per cause.

- [ ] **Step 1: Preconditions** — same commands as Task 6 Step 1, plus `echo "$E2E_WEBKIT_WS_ENDPOINT"` shows a `ws://127.0.0.1:<port>/<32 hex>` value.

- [ ] **Step 2: Run the gate**

Run:
```bash
log="$EVIDENCE/make-test-e2e-$(date +%Y%m%dT%H%M%S).log"
make test-e2e 2>&1 | tee "$log"; echo "exit=${PIPESTATUS[0]}"
grep -E 'AEGIS_E2E (begin|end|project=)' "$log" | tail -30
grep -E '^\s*Container aegis-phase1-e2e' "$log" | tail -3
```
Expected on success: `AEGIS_E2E begin cases=22`, 22 `status=passed` lines, `AEGIS_E2E end status=passed`, `exit=0`. The harness's failing phase name (`config`, `build`, `up`, `seed`, `index-wait`, `playwright`, …) locates any failure.

- [ ] **Step 3: Triage by phase**

- Phases before `playwright` (harness, Compose, mounts, seeding): reproduce by re-running `make test-e2e` after each fix; inspect `$log` around the failing phase. Classify under the policy. Typical Podman-specific causes are UID mapping of generated files and tmpfs options; fix them in engine-routed harness code (`scripts/e2e_support.py` Podman branches) or `compose.podman.yaml`.
- Phase `playwright` with `mobile-webkit` failures only: confirm the WebKit container is healthy (`uv run --locked python -m scripts.podman_dev status`) and that pasta reachability still holds (Task 5 Step 4).
- Phase `playwright` failures in both browsers: treat as application or harness behavior, not browser plumbing.

- [ ] **Step 4: Fix, re-run, record**

One commit per cause. Repeat Step 2 until 22/22. Record counts and duration in `$EVIDENCE/task9.md`. After the run, confirm `ss -ltnH '( sport = :18080 or sport = :55432 )'` is empty and the container list matches `$EVIDENCE/after-names.txt` plus the WebKit server.

---

### Task 10: `phase2a.spec.ts:339` flake investigation

**Files:**
- Modify: `scripts/test-e2e.sh` (argument allowlist near the top; forward arguments on the final line)
- Create: `frontend/e2e/safe-location.ts`
- Create: `frontend/e2e/safe-location.unit.ts`
- Modify: `frontend/e2e/safe-test.ts`
- Modify: `frontend/e2e/safe-reporter.ts`
- Modify: `frontend/e2e/safe-reporter.unit.ts`
- Modify: `frontend/vite.config.ts` (add the new unit file to `include`)
- Modify: `tests/deployment/test_e2e_harness.py` (refusal test)

**Interfaces:**
- Consumes: a passing `make test-e2e` (Task 9).
- Produces: `specFailureLine(stack: string | undefined): number | undefined`; `AEGIS_E2E … status=failed failed_line=<n>` reporter output; `bash scripts/test-e2e.sh [--project=mobile-chromium|mobile-webkit] [--repeat-each=1-99] [e2e/<name>.spec.ts:<line>]`.

- [ ] **Step 1: Write the failing harness test**

Append to `tests/deployment/test_e2e_harness.py`:

```python
@pytest.mark.parametrize("argument", ["--headed", "--repeat-each=100", "--grep=x", "../x.ts:1"])
def test_e2e_refuses_unlisted_playwright_arguments(argument: str) -> None:
    repository = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["bash", str(repository / "scripts/test-e2e.sh"), argument],
        capture_output=True, text=True, timeout=30, env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 64
    assert "Unsupported e2e argument" in result.stderr


def test_e2e_forwards_only_validated_playwright_arguments() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/test-e2e.sh").read_text(
        encoding="utf-8",
    )
    forward = 'npm --prefix frontend run test:e2e -- "${playwright_args[@]}"'
    assert script.rstrip().endswith(forward)
    assert script.index("playwright_args=()") < script.index("container_engine=(")
```

Add any missing imports (`os`, `subprocess`, `pytest`, `Path`) at the top of the file, keeping Ruff's import order.

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run --locked pytest tests/deployment/test_e2e_harness.py -k "playwright_arguments" -q -p no:cacheprovider`
Expected: FAIL (the script does not validate arguments yet).

- [ ] **Step 3: Add the allowlist to `scripts/test-e2e.sh`**

Insert directly after the `cd "$repository_dir"` line (before `case "${AEGIS_CONTAINER_ENGINE-docker}" in`):

```bash
# Local investigation only: CI passes no arguments. Each argument must match this
# allowlist exactly; anything else stops before any resource is created.
playwright_args=()
for argument in "$@"; do
    if [[ "$argument" =~ ^--project=mobile-(chromium|webkit)$ ||
          "$argument" =~ ^--repeat-each=[1-9][0-9]?$ ||
          "$argument" =~ ^e2e/[a-z0-9]+\.spec\.ts:[1-9][0-9]*$ ]]; then
        playwright_args+=("$argument")
    else
        printf '%s\n' "Unsupported e2e argument; allowed: --project=mobile-chromium|mobile-webkit, --repeat-each=1-99, e2e/<name>.spec.ts:<line>." >&2
        exit 64
    fi
done
```

Replace the final line `npm --prefix frontend run test:e2e` with:

```bash
npm --prefix frontend run test:e2e -- "${playwright_args[@]}"
```

- [ ] **Step 4: Run the harness tests**

Run: `uv run --locked pytest tests/deployment/test_e2e_harness.py tests/deployment/test_frozen_commands.py -q -p no:cacheprovider`
Expected: all pass, including the existing frozen-command check for `run test:e2e`.

- [ ] **Step 5: Write the failing failure-line tests**

Create `frontend/e2e/safe-location.unit.ts`:

```ts
import {expect, test} from "vitest";
import {specFailureLine} from "./safe-location";

test("only a spec line number survives", () => {
  const stack = [
    "Error: expect(locator).toBeFocused() failed",
    "    at /home/someone/aegis/frontend/e2e/phase2a.spec.ts:377:72",
    "    at /home/someone/aegis/frontend/e2e/auth.ts:12:3",
  ].join("\n");
  expect(specFailureLine(stack)).toBe(377);
});

test.each([
  [undefined],
  ["Error: no frames"],
  ["    at /home/someone/aegis/frontend/e2e/auth.ts:12:3"],
  ["    at /elsewhere/phase2a.spec.ts:9:1"],
])("returns undefined without an e2e spec frame: %s", (stack) => {
  expect(specFailureLine(stack)).toBeUndefined();
});
```

Append to `frontend/e2e/safe-reporter.unit.ts`:

```ts
test("a failed case reports only the sanitizer's spec line", () => {
  const output = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const reporter = new SafeReporter();
  const testCase = {
    id: "t1", location: {line: 339}, parent: {project: () => ({name: "mobile-chromium"})},
  };
  reporter.onBegin(
    {projects: []} as never, {allTests: () => [testCase]} as never,
  );
  reporter.onTestEnd(testCase as never, {
    status: "failed", attachments: [],
    errors: [{message: "withheld", location: {file: "withheld.spec.ts", line: 377, column: 0}}],
  } as never);
  reporter.onTestEnd(testCase as never, {
    status: "failed", attachments: [],
    errors: [{message: "x", location: {file: "/private/path/phase2a.spec.ts", line: 5, column: 1}}],
  } as never);
  expect(output.mock.calls).toEqual([
    ["AEGIS_E2E begin cases=1"],
    ["AEGIS_E2E project=mobile-chromium case=1 line=339 status=failed failed_line=377"],
    ["AEGIS_E2E project=mobile-chromium case=1 line=339 status=failed"],
  ]);
});
```

Add `"e2e/safe-location.unit.ts",` to the `include` list in `frontend/vite.config.ts`, next to `"e2e/safe-reporter.unit.ts",`.

- [ ] **Step 6: Run them to verify they fail**

Run: `npm --prefix frontend test -- e2e/safe-location.unit.ts e2e/safe-reporter.unit.ts`
Expected: FAIL (`safe-location` does not exist; no `failed_line`).

- [ ] **Step 7: Implement the failure-line diagnostic**

Create `frontend/e2e/safe-location.ts`:

```ts
// Only a line number from one of this directory's spec files survives sanitization;
// paths, messages and values never do.
const SPEC_FRAME = /[\\/]e2e[\\/][a-z0-9]+\.spec\.ts:([1-9][0-9]*):[0-9]+/;

export function specFailureLine(stack: string | undefined): number | undefined {
  const match = stack === undefined ? null : SPEC_FRAME.exec(stack);
  if (match === null) return undefined;
  const line = Number(match[1]);
  return Number.isSafeInteger(line) ? line : undefined;
}
```

In `frontend/e2e/safe-test.ts`, add `import {specFailureLine} from "./safe-location";` after the existing import, and replace the sanitizing loop with:

```ts
      for (const error of testInfo.errors) {
        const line = specFailureLine(error.stack);
        for (const key of Object.keys(error)) delete error[key as keyof TestInfoError];
        error.message = "Browser check failed; free-text diagnostics withheld.";
        if (line !== undefined) error.location = {file: "withheld.spec.ts", line, column: 0};
      }
```

In `frontend/e2e/safe-reporter.ts`, replace the `console.log` line in `onTestEnd` with:

```ts
    const failedLine = result.errors
      .map((error) => (error.location?.file === "withheld.spec.ts" ? error.location.line : 0))
      .find((value) => Number.isSafeInteger(value) && value > 0);
    const suffix = failedLine === undefined ? "" : ` failed_line=${failedLine}`;
    console.log(`AEGIS_E2E project=${project} case=${this.cases.get(test.id) ?? 0} line=${line} status=${status}${suffix}`);
```

- [ ] **Step 8: Verify the unit tests, lint and types**

Run:
```bash
npm --prefix frontend test -- e2e/safe-location.unit.ts e2e/safe-reporter.unit.ts
npm --prefix frontend run lint
npm --prefix frontend run typecheck
```
Expected: all pass.

- [ ] **Step 9: Prove the diagnostic end to end with a throwaway spec (not committed)**

Run:
```bash
cat > frontend/e2e/zzprobe.spec.ts <<'EOF'
import {expect} from "@playwright/test";
import {test} from "./safe-test";
test("probe", async ({page}) => {
  await page.goto("about:blank");
  expect(1).toBe(2);
});
EOF
(cd frontend && npx playwright test e2e/zzprobe.spec.ts --project=mobile-chromium); echo "exit=$?"
rm frontend/e2e/zzprobe.spec.ts
git status --short frontend/e2e
```
Expected: output contains `status=failed failed_line=5` and no error text; `exit=1`; `git status` shows only the intended changes (no probe file). If `failed_line` is missing, Playwright did not propagate the sanitized location: report it and keep only the unit-tested parts after review.

- [ ] **Step 10: Commit the diagnostics (root session only, after reviews pass)**

The reviewer must confirm the output carries only integers and constant tokens (the privacy rule allows the test id and exception type; a spec line number is of the same kind).
```bash
git add scripts/test-e2e.sh tests/deployment/test_e2e_harness.py frontend/e2e/safe-location.ts \
  frontend/e2e/safe-location.unit.ts frontend/e2e/safe-test.ts frontend/e2e/safe-reporter.ts \
  frontend/e2e/safe-reporter.unit.ts frontend/vite.config.ts
git commit -m "test: allow focused e2e repeats and report the failing spec line"
```

- [ ] **Step 11: Repeat the test 50 times on four CPUs**

Run:
```bash
uv run --locked python -m scripts.podman_dev stop
taskset -c 0-3 uv run --locked python -m scripts.podman_dev start
eval "$(uv run --locked python -m scripts.podman_dev env)"
log="$EVIDENCE/flake-$(date +%Y%m%dT%H%M%S).log"
taskset -c 0-3 bash scripts/test-e2e.sh --project=mobile-chromium --repeat-each=50 \
  e2e/phase2a.spec.ts:339 2>&1 | tee "$log" &
runner=$!
until podman container exists aegis-phase1-e2e-web-1 2>/dev/null; do sleep 5; done
sleep 30
web_pid=$(podman container inspect aegis-phase1-e2e-web-1 --format '{{.State.Pid}}')
grep Cpus_allowed_list "/proc/$web_pid/status" | tee -a "$EVIDENCE/flake-cpus.txt"
wait "$runner"; echo "exit=$?"
grep -c 'status=passed' "$log"; grep 'status=failed' "$log"
```
Expected: `Cpus_allowed_list: 0-3` for the web process (proof the constraint reached the stack; if not, record the actual list); 50 result lines. The name-based `inspect` above is read-only and is never used for deletion.

- [ ] **Step 12: Act on the result**

- One or more failures: read `failed_line=<n>` and open `frontend/e2e/phase2a.spec.ts` at that line. Diagnose with superpowers:systematic-debugging before changing anything. Fix the root cause (a test race or an application bug) with a regression check, commit, then repeat Step 11 and require 50/50.
- Zero failures: record the repeat count, CPU constraint and duration in `$EVIDENCE/task10.md` as non-reproduction evidence. Make no code change for the flake.

---

### Task 11: Evidence, documentation, final gates and push

**Files:**
- Modify: `docs/verification/linux-podman-prerequisite.md` (new final section)
- Modify: `README.md` (Running on Podman section, around lines 193–202)
- Modify: `docs/development.md` (Prerequisites Podman paragraph)
- Modify: `HANDOFF.md`

**Interfaces:**
- Consumes: evidence files from Tasks 0–10.
- Produces: committed documentation, a final fresh-shell gate run, a pushed `main` (after user approval) with green Docker CI.

- [ ] **Step 1: Final fresh-shell gate run**

In a new shell, from the repository root:
```bash
uv run --locked python -m scripts.podman_dev stop
ss -ltnH '( sport = :18080 or sport = :55432 )'; df --output=avail -BG / | tail -1
uv run --locked python -m scripts.podman_dev start
eval "$(uv run --locked python -m scripts.podman_dev env)"
for gate in verify verify-compose test-e2e; do
  log="$EVIDENCE/final-$gate-$(date +%Y%m%dT%H%M%S).log"
  start=$(date +%s); make "$gate" 2>&1 | tee "$log"; status=${PIPESTATUS[0]}
  echo "$gate exit=$status seconds=$(( $(date +%s) - start ))" | tee -a "$EVIDENCE/final.txt"
done
git diff --check && echo "diff-check ok" | tee -a "$EVIDENCE/final.txt"
uv run --locked python -m scripts.podman_dev stop; echo "stop exit=$?" | tee -a "$EVIDENCE/final.txt"
```
Expected: `verify exit=0`, `test-e2e exit=0`, `verify-compose` failures only the O11 IDs from `$EVIDENCE/o11.md`, `diff-check ok`, `stop exit=0` with the unchanged-inventory line.

- [ ] **Step 2: Add the verification section**

Append to `docs/verification/linux-podman-prerequisite.md` a section titled `## Fedora Server development host, October 2026` containing:
1. An environment table: 28 logical CPUs, 62 GiB RAM, Fedora 44 Server, SELinux enforcing, xfs graphroot, Podman 5.8.7, crun 1.28, netavark 1.17.2, pasta, Docker Compose 2.39.4 (SHA-256 `7af95166…`), uv 0.12.8, Python 3.13.15, Node.js 24.20.0, Playwright 1.63.0, the WebKit image tag and ID from Task 3, and the pinned base digests.
2. The helper: commands, the measured pasta result (Task 5 Step 4), and the socket ownership check.
3. A results table with each gate's passed/failed counts and duration from `$EVIDENCE/final.txt` and the per-task evidence.
4. Every failure found, its cause, and its fixing commit hash.
5. The O11 exception list by test ID with the three-run outcomes, stating plainly that these tests fail and are not skipped.
6. The inventory comparison result.
7. The flake result from Task 10.
8. A closing statement that full Podman compatibility is not claimed while O11 is open.
Do not paste raw logs, credentials, private paths beyond the documented tool locations, or screenshots.

- [ ] **Step 3: Update the README's Podman section**

Replace the opening sentence of `### Running on Podman` so it states the measured gate status (from Step 1), keep the existing bullets and the known-limitation paragraph, and add this subsection after them:

````markdown
#### Developing on Podman

A committed helper prepares a rootless Podman development session; Docker users can ignore it.

1. Install or verify the pinned user-local tools: uv, Node.js 24 and the Docker Compose provider, which stays outside `PATH`. On a machine without uv, use the system Python from the repository root:

   ```bash
   python3 -m scripts.podman_dev tools
   uv sync --locked --group dev
   ```

2. Start the session, load its environment and install the frontend dependencies:

   ```bash
   uv run --locked python -m scripts.podman_dev start
   eval "$(uv run --locked python -m scripts.podman_dev env)"
   npm --prefix frontend ci
   (cd frontend && npm exec -- playwright install chromium)
   ```

3. Run the canonical gates sequentially: `make verify`, `make verify-compose`, `make test-e2e`, `git diff --check`.
4. Stop the session. It removes only what it created, by recorded identity, and reports any pre-existing Podman object that changed:

   ```bash
   uv run --locked python -m scripts.podman_dev stop
   ```

The session is a private foreground `podman system service` socket under `$XDG_RUNTIME_DIR` (never TCP, never the systemd socket unit) plus a locally built Playwright WebKit server container from `docker/dev-webkit.Dockerfile`, because Playwright's WebKit build needs Ubuntu libraries. `status` reports both.

**When Podman updates:** the guarded launcher accepts exactly Podman 5.8.7, crun 1.28 and Docker Compose 2.39.4. After a host update it refuses with `unsupported Podman version`. Do not pin or downgrade the host to get past it: re-verify the new version (the focused mask checks, then the full gates) and extend the guard in a reviewed change.
````

- [ ] **Step 4: Update `docs/development.md`**

In the Prerequisites paragraph that begins "Rootless Linux/Podman is a secondary, optional runtime", replace the sentence "Full local Podman application gates (`make verify`, `make verify-compose`, `make test-e2e` under Podman) have not run to completion and full Podman compatibility is not accepted." with a sentence stating the measured result from Step 1 (for example: on the October 2026 Fedora Server host, `make verify` and `make test-e2e` passed and `make verify-compose` passed except the listed O11 tests), followed by a link to the README's "Developing on Podman" subsection and the new verification section. Keep "Docker Engine with Compose v2 is the primary, supported container runtime" unchanged.

- [ ] **Step 5: Update `HANDOFF.md`**

- Top status: add a line that part 1 (Podman development environment) is complete, with the commit range and the final gate results.
- Next actions: replace items 1–2 with "Brainstorm part 2: persistent private-network deployment on this host" and list its agreed requirements: free ports (default ports may already be taken on a shared host) and a firewall rule that admits only private-network sources, local-CA HTTPS, fixing O11 real client addresses, SELinux and UID mapping for read-only originals without relabeling or ownership changes, the user's chosen test root as the first root, pull-and-run published images with no checkout or local build on the server, existing services left untouched until the user approves replacing anything.
- Record the October 4 decision on the revocation window: keep the 60 s web pool lifetime; part 3 still adds runbook note m8.
- Keep items 4–6 (docs sync, disk opt-in, Tasks 16–18) as part 3, after part 2.
- In "Setting up the new computer", point to the README's "Developing on Podman" subsection for this host.

- [ ] **Step 6: Consistency check and commit**

Run:
```bash
git diff --check
grep -n "have not run to completion" README.md docs/development.md || echo "stale claim removed"
uv run --locked ruff check backend tests scripts
```
Expected: clean; `stale claim removed`. Root session commits:
```bash
git add docs/verification/linux-podman-prerequisite.md README.md docs/development.md HANDOFF.md
git commit -m "docs: record the Fedora Podman development environment and gate results"
```

- [ ] **Step 7: Ask the user before pushing**

Show the user `git log --oneline origin/main..HEAD` and the final gate summary, and ask for explicit permission to push to `origin/main`. Do not push without a yes.

- [ ] **Step 8: Push and watch CI (only after approval)**

Run:
```bash
git push origin main
run_id=$(gh run list --branch main --limit 1 --json databaseId --jq '.[0].databaseId')
gh run watch "$run_id" --exit-status
gh run view "$run_id" --json jobs --jq '.jobs[] | "\(.name) \(.conclusion)"'
```
Expected: all four jobs (`backend`, `frontend`, `deployment`, `e2e`) `success`. If a job fails, diagnose it (`gh run view "$run_id" --log-failed`) before any other work, and report to the user.
