"""Resource-free safety tests for the Phase 2A browser harness source fixture."""
from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from aegisctl.container_resources import ProjectInventory, ProjectResource, ProjectResourceError

REPOSITORY = Path(__file__).resolve().parents[2]
SUPPORT = runpy.run_path(str(REPOSITORY / "scripts/e2e_support.py"))


def _private_directory() -> Path:
    path = Path(tempfile.mkdtemp(prefix="aegis-phase1-e2e.", dir="/tmp"))
    path.chmod(0o700)
    return path


@pytest.fixture
def prepared(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    path = _private_directory()
    SUPPORT["prepare"](path)
    yield path
    if path.exists():
        SUPPORT["cleanup"](path)


def _sources(path: Path) -> dict[str, dict[str, Any]]:
    manifest = json.loads((path / SUPPORT["SOURCE_MANIFEST"]).read_text(encoding="ascii"))
    assert manifest["version"] == 1
    return manifest["entries"]


def test_owned_source_fixture_is_large_nested_tied_and_recorded_before_mounting(
    prepared: Path,
) -> None:
    entries = _sources(prepared)
    alice = {key for key in entries if key.startswith("roots/alice/")}
    assert len(entries) >= 603
    assert len([key for key in alice if key.count("/") == 2]) > 500  # beyond five pages
    assert entries["roots/alice/albums/2024/summer/deep.txt"]["kind"] == "file"
    assert entries["roots/alice/empty-folder"]["kind"] == "directory"
    # Tied names and timestamps; mixed, missing and literal-unknown extensions.
    assert {"roots/alice/Tie.txt", "roots/alice/tie.txt"} <= entries.keys()
    bulk = [value["mtime_ns"] for key, value in entries.items() if "/bulk-" in key]
    assert len(bulk) >= 500 and len(set(bulk)) == 1
    names = {key.rsplit("/", 1)[-1] for key in alice}
    assert {"README", "data.unknown", "photo.JPG", "clip.mp4", "large.bin"} <= names
    # The symlink names an unmounted synthetic sibling and is recorded, never followed.
    link = entries["roots/alice/link-outside"]
    assert link == {"kind": "symlink", "target": "../alice-sibling/sibling-secret.txt"}
    assert entries["roots/alice-sibling/sibling-secret.txt"]["kind"] == "file"
    mounts = (prepared / "mounts.toml").read_text(encoding="ascii")
    assert "alice-sibling" not in mounts
    assert entries["roots/bob/locked"] == {
        "kind": "directory", "mode": 0, "mtime_ns": entries["roots/bob/locked"]["mtime_ns"],
        "listed": False,
    }
    # Exact content hash, size and timestamp for every regular file.
    alice_only = entries["roots/alice/alice-only.txt"]
    assert alice_only["size"] == len(b"Synthetic alice fixture\n")
    assert len(alice_only["sha256"]) == 64
    assert (prepared / SUPPORT["SOURCE_MANIFEST"]).stat().st_mode & 0o777 == 0o600
    assert SUPPORT["source_snapshot"](prepared) == entries


def test_source_fixture_and_manifest_are_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    manifests = []
    for _ in range(2):
        path = _private_directory()
        try:
            SUPPORT["prepare"](path)
            manifests.append(_sources(path))
        finally:
            SUPPORT["cleanup"](path)
    assert manifests[0] == manifests[1]


def _rewrite_same_size(target: Path) -> Callable[[], None]:
    before = target.lstat()
    original = target.read_bytes()
    target.write_bytes(bytes(byte ^ 1 for byte in original))
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns), follow_symlinks=False)

    def undo() -> None:
        target.write_bytes(original)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns), follow_symlinks=False)
    return undo


def _mutate(path: Path, change: str) -> Callable[[], None]:
    alice = path / "roots/alice"
    target = alice / "alice-only.txt"
    before = target.lstat()
    if change == "bytes":
        return _rewrite_same_size(target)
    if change == "name":
        renamed = alice / "alice-renamed.txt"
        target.rename(renamed)
        return lambda: renamed.rename(target)
    if change == "size":
        original = target.read_bytes()
        with target.open("ab") as handle:
            handle.write(b"x")
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

        def undo_size() -> None:
            target.write_bytes(original)
            os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return undo_size
    if change == "mtime":
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1))
        return lambda: os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    if change == "link":
        link = alice / "link-outside"
        saved_link = alice / "link-saved"
        link.rename(saved_link)  # Keep the recorded inode for restoration.
        link.symlink_to("../bob/bob-only.txt")

        def undo_link() -> None:
            link.unlink()
            saved_link.rename(link)
        return undo_link
    if change == "unknown":
        unknown = alice / "albums/2024/unexpected.txt"
        unknown.write_bytes(b"retain")
        return unknown.unlink
    assert change == "sealed"
    locked = path / "roots/bob/locked"
    locked.chmod(0o700)
    return lambda: locked.chmod(0)


@pytest.mark.parametrize("change", ["bytes", "name", "size", "mtime", "link", "unknown", "sealed"])
def test_source_drift_fails_verification_and_retains_the_fixture(
    prepared: Path, change: str,
) -> None:
    undo = _mutate(prepared, change)
    try:
        with pytest.raises(ValueError, match="source fixture changed"):
            SUPPORT["verify_sources"](prepared)
        with pytest.raises(ValueError):
            SUPPORT["cleanup"](prepared)
        assert (prepared / SUPPORT["SOURCE_MANIFEST"]).exists()
        assert (prepared / "roots/bob/bob-only.txt").exists()
        assert (prepared / SUPPORT["LEDGER_NAME"]).exists()
    finally:
        undo()
    # Restored inputs are exactly the recorded fixture again (mtimes may be re-set).
    for directory in ("roots/alice", "roots/alice/albums/2024"):
        recorded = _sources(prepared)[directory]["mtime_ns"]
        os.utime(prepared / directory, ns=(recorded, recorded))
    SUPPORT["verify_sources"](prepared)


def test_verified_cleanup_removes_only_recorded_entries_without_following_links(
    prepared: Path, tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-sentinel.txt"
    outside.write_bytes(b"preserve")
    summary = SUPPORT["verify_sources"](prepared)
    assert summary["entries"] == len(_sources(prepared))
    assert summary["sealed"] == 1 and summary["symlinks"] == 1
    assert len(summary["manifest_sha256"]) == 64
    SUPPORT["cleanup"](prepared)
    assert not prepared.exists()
    assert outside.read_bytes() == b"preserve"


def test_cleanup_refuses_identity_replacement_inside_the_source_fixture(prepared: Path) -> None:
    target = prepared / "roots/alice/notes.md"
    saved = prepared / "roots/alice/notes.saved"
    target.rename(saved)
    target.write_bytes(saved.read_bytes())
    try:
        with pytest.raises(ValueError):
            SUPPORT["cleanup"](prepared)
        assert saved.exists() and target.exists()
    finally:
        target.unlink()
        saved.rename(target)
    recorded = _sources(prepared)["roots/alice"]["mtime_ns"]
    os.utime(prepared / "roots/alice", ns=(recorded, recorded))


def test_hidden_content_in_sealed_directory_refuses_before_any_deletion(
    prepared: Path,
) -> None:
    locked = prepared / "roots/bob/locked"
    recorded = _sources(prepared)["roots/bob/locked"]["mtime_ns"]
    locked.chmod(0o700)
    hidden = locked / "hidden.txt"
    hidden.write_bytes(b"retain")
    # Reset the timestamp so the source snapshot alone cannot see the addition.
    os.utime(locked, ns=(recorded, recorded))
    locked.chmod(0)
    before = SUPPORT["_current_entries"](prepared)
    try:
        SUPPORT["verify_sources"](prepared)
        with pytest.raises(ValueError, match="unknown inputs"):
            SUPPORT["cleanup"](prepared)
        assert SUPPORT["_current_entries"](prepared) == before  # Nothing was removed.
    finally:
        locked.chmod(0o700)
        hidden.unlink()
        os.utime(locked, ns=(recorded, recorded))
        locked.chmod(0)


def _resource(token: str) -> ProjectResource:
    fingerprint = json.dumps({
        "Id": "preexisting", "Name": "preexisting", "Labels": {
            "com.docker.compose.project": SUPPORT["PROJECT"],
            "com.docker.compose.service": "postgres",
            "com.docker.compose.oneoff": "False",
            "aegis.test.resource-token": token,
        },
    }, sort_keys=True, separators=(",", ":"))
    return ProjectResource("container", "preexisting", "preexisting", fingerprint)


def test_preexisting_project_resources_are_refused_and_never_removed(
    prepared: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aegisctl.container_resources as resources

    removals: list[tuple[str, ...]] = []
    existing = ProjectInventory(SUPPORT["PROJECT"], (_resource("an-older-run"),))
    monkeypatch.setattr(resources, "capture_project_inventory",
                        lambda project, environment=None, *, runner=None: existing)
    monkeypatch.setattr(resources, "_run", lambda arguments, environment: removals.append(
        tuple(arguments)) or subprocess.CompletedProcess(arguments, 0, "", ""))
    with pytest.raises(ProjectResourceError, match="unknown resources"):
        SUPPORT["resources_check"](prepared)
    with pytest.raises(ProjectResourceError, match="unknown resources"):
        SUPPORT["resources_cleanup"](prepared)
    assert removals == []
    # The fixture stays complete for diagnosis; sources are still provably untouched.
    SUPPORT["verify_sources"](prepared)
    assert (prepared / "roots/alice/alice-only.txt").exists()


def _fake_harness(
    tmp_path: Path, fail_at: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    binary = tmp_path / "bin"
    binary.mkdir()
    work_dir = tmp_path / "fake-e2e-work"
    log = tmp_path / "calls"
    record = 'printf "%s|%s\\n" "$*" "${E2E_ALICE_PASSWORD:+env}" >> "$FAKE_CALL_LOG"\n'
    fail = 'case "$*" in *"$FAKE_FAIL"*) [ -n "$FAKE_FAIL" ] && exit 77;; esac\n'
    for name, source in {
        "mktemp": '#!/bin/sh\nmkdir -m 700 "$FAKE_E2E_WORK"\nprintf "%s\\n" "$FAKE_E2E_WORK"\n',
        "node": "#!/bin/sh\nprintf '24\\n'\n",
        "docker": "#!/bin/sh\n" + record + fail + "exit 0\n",
        "npm": "#!/bin/sh\n" + record + fail + "exit 0\n",
        "uv": (
            "#!/bin/sh\n" + record + fail +
            "case \"$*\" in\n"
            "  *'e2e_support.py prepare '*)\n"
            "    for work do :; done; mkdir -p \"$work/secrets\"\n"
            "    for name in e2e-alice-password e2e-bob-password e2e-admin-password; do "
            "printf 'synthetic\\n' > \"$work/secrets/$name\"; done;;\n"
            "esac\nexit 0\n"
        ),
    }.items():
        executable = binary / name
        executable.write_text(source, encoding="ascii")
        executable.chmod(0o700)
    environment = os.environ | {
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "FAKE_E2E_WORK": str(work_dir), "FAKE_FAIL": fail_at, "FAKE_CALL_LOG": str(log),
        "AEGIS_CONTAINER_ENGINE": "docker", "GITHUB_ACTIONS": "true",
    }
    for name in ("E2E_ALICE_PASSWORD", "E2E_BOB_PASSWORD", "E2E_ADMIN_PASSWORD"):
        environment.pop(name, None)
    result = subprocess.run(
        ["bash", str(REPOSITORY / "scripts/test-e2e.sh")], env=environment,
        capture_output=True, text=True, timeout=30,
    )
    return result, log.read_text(encoding="ascii").splitlines()


def _position(calls: list[str], fragment: str, *, last: bool = False) -> int:
    matches = [index for index, call in enumerate(calls) if fragment in call]
    assert matches and (last or len(matches) == 1), fragment
    return matches[-1]


def test_harness_waits_for_real_indexing_before_browsers_with_env_only_credentials(
    tmp_path: Path,
) -> None:
    result, calls = _fake_harness(tmp_path, "")
    assert result.returncode == 0
    wait = _position(calls, "e2e_support.py index-wait ")
    assert _position(calls, "seed_phase1_e2e", last=True) < wait < _position(calls, "run test:e2e")
    assert calls[wait].endswith("|env")  # Supplied by environment, not as an argument.
    assert "synthetic" not in calls[wait]


@pytest.mark.parametrize("fail_at", ["index-wait", "run test:e2e", "up --build --wait"])
def test_failed_run_stops_workers_then_verifies_sources_before_fixture_cleanup(
    tmp_path: Path, fail_at: str,
) -> None:
    result, calls = _fake_harness(tmp_path, fail_at)
    assert result.returncode == 77
    stop = _position(calls, "e2e_support.py resources-cleanup ")
    verify = _position(calls, "e2e_support.py sources-verify ")
    remove = _position(calls, "e2e_support.py cleanup ")
    assert stop < verify < remove


def test_source_verification_failure_retains_fixture_and_names_its_phase(tmp_path: Path) -> None:
    result, calls = _fake_harness(tmp_path, "sources-verify")
    assert result.returncode == 77
    assert not any("e2e_support.py cleanup " in call for call in calls)
    assert [line for line in result.stdout.splitlines() if line.startswith("::")] == [
        "::error title=e2e-phase::sources-verify",
    ]


def test_resource_cleanup_refusal_skips_source_verification_and_deletion(tmp_path: Path) -> None:
    result, calls = _fake_harness(tmp_path, "resources-cleanup")
    assert result.returncode == 77
    assert not any("sources-verify" in call or "e2e_support.py cleanup " in call
                   for call in calls)


def _status(state: str, completed: int, degraded: int = 0) -> dict[str, Any]:
    return {
        "state": state, "completedDirectories": str(completed),
        "degradedDirectories": str(degraded),
        "lastCompletedAt": "2026-01-01T00:00:00+00:00" if state == "ready" else None,
    }


def test_index_wait_is_bounded_and_publishes_only_fixed_refusals(
    prepared: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    clock = iter(range(0, 10_000, 30))
    support = SUPPORT["index_wait"].__globals__
    monkeypatch.setitem(support, "_monotonic", lambda: next(clock))
    monkeypatch.setitem(support, "_sleep", lambda seconds: None)
    monkeypatch.setitem(support, "_index_states",
                        lambda base, user, password: [_status("scanning", 3)])
    monkeypatch.setenv("E2E_ALICE_PASSWORD", "private-sentinel")
    monkeypatch.setenv("E2E_BOB_PASSWORD", "private-sentinel")
    monkeypatch.setenv("AEGIS_PUBLIC_URL", "http://127.0.0.1:18080")
    with pytest.raises(ValueError, match="did not settle") as raised:
        SUPPORT["index_wait"](prepared)
    assert "private-sentinel" not in str(raised.value)
    assert capsys.readouterr().err == "AEGIS_E2E index-wait user=alice states=scanning:3/0\n"
    annotation = SUPPORT["refusal_annotation"]("index-wait", raised.value)
    assert annotation == (
        "::error title=e2e-support index-wait::"
        "ValueError: synthetic E2E index did not settle"
    )


def test_index_wait_requires_every_owned_directory_to_be_terminal(
    prepared: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    # Alice owns five directories; Bob's root plus its sealed directory are two.
    states = iter([
        [_status("scanning", 2)], [_status("ready", 4)], [_status("ready", 5)],
        [_status("degraded", 0, 1)], [_status("degraded", 1, 1)],
    ])
    support = SUPPORT["index_wait"].__globals__
    monkeypatch.setitem(support, "_sleep", lambda seconds: None)

    def report(base: str, user: str, password: str) -> list[dict[str, Any]]:
        observed.append(user)
        return next(states)

    monkeypatch.setitem(support, "_index_states", report)
    monkeypatch.setenv("E2E_ALICE_PASSWORD", "a")
    monkeypatch.setenv("E2E_BOB_PASSWORD", "b")
    monkeypatch.setenv("AEGIS_PUBLIC_URL", "http://127.0.0.1:18080")
    SUPPORT["index_wait"](prepared)
    assert observed == ["alice", "alice", "alice", "bob", "bob"]


def test_sealed_fixture_directory_is_owned_and_unlistable(prepared: Path) -> None:
    locked = prepared / "roots/bob/locked"
    assert stat.S_IMODE(locked.lstat().st_mode) == 0
    assert locked.lstat().st_uid == os.geteuid()
