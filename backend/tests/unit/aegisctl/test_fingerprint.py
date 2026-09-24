from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import pytest
from aegis_apps.roots.manifest import MountManifest
from aegisctl.mounts import (
    MAX_MOUNTINFO_BYTES,
    ConfigError,
    ValidatedSlot,
    attest_mounts,
    local_identity,
    observe_mount_fingerprints,
    parse_config,
    parse_mountinfo,
    preflight_slots,
    set_observer_output_limit,
)

from tests.support.observer_engine import (
    ObserverEngine as _ObserverEngine,
)
from tests.support.observer_engine import (
    assert_observer_engine_cleaned as _assert_observer_engine_cleaned,
)
from tests.support.observer_engine import (
    write_observer_record as _write_observer_record,
)


def _retained_path(error: BaseException) -> Path:
    marker = "diagnostics retained at "
    message = str(error)
    assert marker in message
    return Path(message.split(marker, maxsplit=1)[1])


def _observer_slots(tmp_path: Path) -> tuple[ValidatedSlot, ...]:
    source = tmp_path / "private-canary"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    config.write_text(
        f"""
version = 1
[[slots]]
slot_id = "photos"
source = "{source}"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return preflight_slots(parse_config(config))


def _direct_observer_slots(tmp_path: Path) -> tuple[ValidatedSlot, ...]:
    source = tmp_path / "direct-private-canary"
    source.mkdir()
    metadata = source.stat()
    return (ValidatedSlot(
        "photos", source, "/srv/aegis/roots/photos", "read_only",
        metadata.st_dev, metadata.st_ino, local_identity(source),
    ),)




def test_mount_fingerprint_excludes_volatile_ids_target_and_mode_options() -> None:
    first = parse_mountinfo(
        b"643 631 0:50 /host\\040root /srv/aegis/roots/photos ro,nosuid "
        b"- fakeowner /run/host_mark/private rw,fakeowner\n"
    )["/srv/aegis/roots/photos"]
    second = parse_mountinfo(
        b"999 888 0:50 /host\\040root /different/target rw,nodev "
        b"- fakeowner /run/host_mark/private ro,fakeowner\n"
    )["/different/target"]
    expected = hashlib.sha256(
        b"aegis.mount-fingerprint.v1\0"
        b"0:50\0"
        b"/host\\040root\0"
        b"fakeowner\0"
        b"/run/host_mark/private\0"
    ).hexdigest()

    assert first.mount_fingerprint == expected
    assert second.mount_fingerprint == expected
    assert first.effective_mode == "read_only"
    assert second.effective_mode == "read_only"


def test_backend_attestation_uses_mount_fingerprint_not_container_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    config.write_text(
        f"""
version = 1
[[slots]]
slot_id = "photos"
source = "{source}"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    host_validated = preflight_slots(parse_config(config))
    raw_record = (
        b"643 631 0:50 /private/source /srv/aegis/roots/photos ro "
        b"- fakeowner /run/host_mark/private rw\n"
    )
    fingerprint = parse_mountinfo(raw_record)[
        "/srv/aegis/roots/photos"
    ].mount_fingerprint
    from aegisctl.mounts import write_manifest

    manifest_path = tmp_path / "manifest.json"
    write_manifest(
        manifest_path,
        tuple(replace(slot, mount_fingerprint=fingerprint) for slot in host_validated),
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    manifest = MountManifest.load(manifest_path, digest)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_bytes(raw_record)
    real_open, real_access = os.open, os.access
    monkeypatch.setattr(os, "open", lambda path, *args, **kwargs:
                        real_open(source if str(path) == "/srv/aegis/roots/photos" else path,
                                  *args, **kwargs))
    monkeypatch.setattr(os, "access", lambda path, *args, **kwargs:
                        real_access(source if str(path) == "/srv/aegis/roots/photos" else path,
                                    *args, **kwargs))
    monkeypatch.setattr(
        os,
        "stat",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError),
    )

    attest_mounts(manifest, "indexer", mountinfo_path=mountinfo)


def test_container_observer_returns_fingerprinted_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source,with:punctuation"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    config.write_text(
        f"""
version = 1
[[slots]]
slot_id = "photos"
source = "{source}"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    validated = preflight_slots(parse_config(config))
    engine = _ObserverEngine(source)
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", engine)

    observed = observe_mount_fingerprints(validated)

    assert len(observed[0].mount_fingerprint) == 64
    _assert_observer_engine_cleaned(engine)
    assert engine.diagnostics is not None
    assert not engine.diagnostics.exists()


def test_observer_run_timeout_still_cleans_and_verifies_project_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    slots = _observer_slots(tmp_path)
    engine = _ObserverEngine(slots[0].source, run_timeout=True)
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", engine)

    with pytest.raises(ConfigError, match="observation") as caught:
        observe_mount_fingerprints(slots)

    _assert_observer_engine_cleaned(engine)
    assert (_retained_path(caught.value) / "mountinfo.out").read_bytes()
    assert str(slots[0].source) not in str(caught.value)


def test_observer_resource_cleanup_refusal_retains_inventoried_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aegisctl.mounts as mounts
    from aegisctl.container_resources import ProjectInventory, ProjectResourceError

    slots = _direct_observer_slots(tmp_path)
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(mounts, "ensure_outputs_outside_originals", lambda *args: None)
    monkeypatch.setattr(
        mounts, "require_empty_project", lambda project: ProjectInventory(project, ()),
    )

    def completed(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        _write_observer_record(kwargs["stdout"])
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", completed)
    monkeypatch.setattr(
        mounts, "_cleanup_observer_project",
        lambda *args: (_ for _ in ()).throw(ProjectResourceError("query failed")),
    )

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        observe_mount_fingerprints(slots)
    retained = _retained_path(caught.value)
    assert retained.is_dir()
    assert {path.name for path in retained.iterdir()} == {"compose.yaml", "mountinfo.out"}


def test_observer_unknown_diagnostic_file_refuses_before_engine_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import aegisctl.mounts as mounts
    from aegisctl.container_resources import ProjectInventory

    slots = _direct_observer_slots(tmp_path)
    cleanup_called = False
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(mounts, "ensure_outputs_outside_originals", lambda *args: None)
    monkeypatch.setattr(
        mounts, "require_empty_project", lambda project: ProjectInventory(project, ()),
    )

    def completed(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        compose_path = Path(arguments[arguments.index("-f") + 1])
        (compose_path.parent / "unknown").write_text("retain", encoding="ascii")
        _write_observer_record(kwargs["stdout"])
        return subprocess.CompletedProcess(arguments, 0)

    def cleanup(*args: object) -> None:
        nonlocal cleanup_called
        del args
        cleanup_called = True

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", completed)
    monkeypatch.setattr(mounts, "_cleanup_observer_project", cleanup)

    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        observe_mount_fingerprints(slots)
    retained = _retained_path(caught.value)
    assert cleanup_called is False
    assert (retained / "unknown").read_text(encoding="ascii") == "retain"
    assert {path.name for path in retained.iterdir()} == {
        "compose.yaml", "mountinfo.out", "unknown",
    }


@pytest.mark.parametrize(
    "cleanup_failure",
    ["exception", "timeout", "nonzero", "residual"],
)
def test_observer_rejects_unconfirmed_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_failure: str,
) -> None:
    slots = _observer_slots(tmp_path)
    engine = _ObserverEngine(slots[0].source, cleanup_failure=cleanup_failure)
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", engine)

    with pytest.raises(ConfigError, match="observation") as caught:
        observe_mount_fingerprints(slots)

    assert engine.resources == {"container": "observer-immutable", "network": "network-immutable"}
    expected_removals = [("rm", "--force", "observer-immutable")]
    if cleanup_failure == "residual":
        expected_removals.append(("network", "rm", "network-immutable"))
        assert engine.queries[-3:] == [
            ("container", "container-handle\n"), ("network", "network-handle\n"), ("volume", ""),
        ]
    assert engine.removals == expected_removals
    assert (_retained_path(caught.value) / "mountinfo.out").read_bytes()
    assert str(slots[0].source) not in str(caught.value)


def test_container_observer_never_captures_unbounded_subprocess_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    config.write_text(
        f"""
version = 1
[[slots]]
slot_id = "photos"
source = "{source}"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    validated = preflight_slots(parse_config(config))

    engine = _ObserverEngine(source, oversized=True)
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr("aegisctl.mounts.subprocess.run", engine)

    with pytest.raises(ConfigError, match="observation") as caught:
        observe_mount_fingerprints(validated)

    _assert_observer_engine_cleaned(engine)
    output = _retained_path(caught.value) / "mountinfo.out"
    assert output.stat().st_size == MAX_MOUNTINFO_BYTES + 1
    assert str(source) not in str(caught.value)


def test_observer_child_has_a_hard_output_file_size_limit(tmp_path: Path) -> None:
    output = tmp_path / "observer-output"
    with output.open("wb") as stream:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import os\nwhile True: os.write(1, b'x' * 65536)",
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=stream,
            stderr=subprocess.DEVNULL,
            preexec_fn=set_observer_output_limit,
        )

    assert result.returncode != 0
    assert output.stat().st_size <= MAX_MOUNTINFO_BYTES + 1


def test_observer_preexec_failure_is_translated_without_path_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "private-canary"
    source.mkdir()
    config = tmp_path / "mounts.toml"
    config.write_text(
        f"""
version = 1
[[slots]]
slot_id = "photos"
source = "{source}"
container_path = "/srv/aegis/roots/photos"
mode = "read_only"
expected_identity = "{local_identity(source)}"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    validated = preflight_slots(parse_config(config))

    def fail_run(*args: object, **kwargs: object) -> NoReturn:
        raise subprocess.SubprocessError(str(source))

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", fail_run)

    with pytest.raises(ConfigError, match="observation") as caught:
        observe_mount_fingerprints(validated)

    assert str(source) not in str(caught.value)


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "read", "invalid", "slot"])
def test_observer_failed_observation_retains_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    import aegisctl.mounts as mounts
    from aegisctl.container_resources import ProjectInventory

    slots = _direct_observer_slots(tmp_path)
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(mounts, "ensure_outputs_outside_originals", lambda *args: None)
    monkeypatch.setattr(mounts, "require_empty_project", lambda p: ProjectInventory(p, ()))
    cleaned: list[str] = []
    monkeypatch.setattr(mounts, "_cleanup_observer_project", lambda p, _: cleaned.append(p))
    original_open = Path.open

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if failure == "invalid":
            kwargs["stdout"].write(b"malformed\n")
        elif failure != "slot":
            _write_observer_record(kwargs["stdout"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(arguments, 30)
        return subprocess.CompletedProcess(arguments, 125 if failure == "nonzero" else 0)

    def open_output(path: Path, *args: Any, **kwargs: Any) -> Any:
        if failure == "read" and path.name == "mountinfo.out" and args == ("rb",):
            raise OSError("read failed")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", run)
    monkeypatch.setattr(Path, "open", open_output)
    with pytest.raises(ConfigError, match="diagnostics retained") as caught:
        observe_mount_fingerprints(slots)
    retained = _retained_path(caught.value)
    assert len(cleaned) == 1
    assert {p.name for p in retained.iterdir()} == {"compose.yaml", "mountinfo.out"}


@pytest.mark.parametrize("interruption", [SystemExit, KeyboardInterrupt])
@pytest.mark.parametrize("uncertainty", [None, "diagnostics", "resources"])
def test_observer_interruption_retains_original_and_recovers_when_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException], uncertainty: str | None,
) -> None:
    import aegisctl.mounts as mounts
    from aegisctl.container_resources import ProjectInventory, ProjectResourceError

    slots = _direct_observer_slots(tmp_path)
    monkeypatch.setattr("aegisctl.mounts.tempfile.tempdir", str(tmp_path))
    monkeypatch.setattr(mounts, "ensure_outputs_outside_originals", lambda *args: None)
    monkeypatch.setattr(mounts, "require_empty_project", lambda p: ProjectInventory(p, ()))
    original = interruption(17)
    recovery_error = ProjectResourceError("query failed")
    cleaned: list[str] = []
    roots: list[Path] = []

    def run(arguments: list[str], **kwargs: Any) -> NoReturn:
        root = Path(arguments[arguments.index("-f") + 1]).parent
        roots.append(root)
        _write_observer_record(kwargs["stdout"])
        if uncertainty == "diagnostics":
            (root / "unknown").write_text("retain")
        raise original

    def cleanup(project: str, expected: ProjectInventory) -> None:
        cleaned.append(project)
        if uncertainty == "resources":
            raise recovery_error

    monkeypatch.setattr("aegisctl.mounts.subprocess.run", run)
    monkeypatch.setattr(mounts, "_cleanup_observer_project", cleanup)
    with pytest.raises(interruption) as caught:
        observe_mount_fingerprints(slots)
    assert caught.value is original
    assert len(cleaned) == (0 if uncertainty == "diagnostics" else 1)
    assert (roots[0] / "mountinfo.out").read_bytes()
    assert str(roots[0]) in " ".join(getattr(original, "__notes__", []))
    if uncertainty is None:
        assert original.__cause__ is None
    elif uncertainty == "resources":
        assert original.__cause__ is recovery_error
    else:
        assert isinstance(original.__cause__, ConfigError)
