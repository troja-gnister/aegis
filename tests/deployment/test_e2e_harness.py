from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import runpy
import stat
import subprocess
import tempfile
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml
from aegisctl.container_resources import (
    ProjectInventory,
    ProjectResource,
    ProjectResourceError,
    admit_project_transition,
)

SUPPORT = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/e2e_support.py"))


def test_failure_logs_never_retain_unstructured_messages_or_credentials() -> None:
    for raw in (
        'password=private-sentinel /srv/aegis/roots/personal',
        json.dumps({"level": "ERROR", "status": 503, "message": "private-sentinel",
                    "metadata": {"password": "private-sentinel"}}),
        json.dumps({"level": {"secret": "private-sentinel"}, "status": "private-sentinel"}),
        "private-sentinel" * 8000,
    ):
        result = json.loads(SUPPORT["sanitize_line"](raw))
        assert "private-sentinel" not in json.dumps(result)
        assert "/srv/aegis" not in json.dumps(result)
        assert set(result) <= {"level", "status", "message"}


def test_generated_secrets_are_private_unique_and_cleanup_preserves_unknown_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    path = private_e2e_directory()
    SUPPORT["prepare"](path)
    files = list((path / "secrets").iterdir())
    assert len(files) == 11
    assert len({file.read_text() for file in files}) == len(files)
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in files)
    assert (path / "roots/alice").is_dir()
    assert (path / "roots/bob").is_dir()
    mounts = (path / "mounts.toml").read_text(encoding="ascii")
    assert str(path / "roots/alice") in mounts
    assert str(path / "roots/bob") in mounts
    assert str(Path(__file__).resolve().parents[2] / "tests/fixtures/roots") not in mounts
    sentinel = path / "unrecognized-data"
    sentinel.write_text("retain")
    with pytest.raises(ValueError, match="unknown"):
        SUPPORT["cleanup"](path)
    assert sentinel.read_text() == "retain"
    assert (path / "secrets/e2e-alice-password").exists()
    sentinel.unlink()
    SUPPORT["cleanup"](path)


def test_prepared_roots_are_gateway_readable_regardless_of_process_umask(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    previous_umask = os.umask(0o077)
    try:
        path = private_e2e_directory()
        try:
            SUPPORT["prepare"](path)
            for name in SUPPORT["ROOT_NAMES"]:
                assert (path / "roots" / name).stat().st_mode & 0o777 == 0o755
            # Unaffected: parent directories and secrets keep their restrictive modes.
            assert path.stat().st_mode & 0o777 == 0o700
            assert (path / "roots").stat().st_mode & 0o777 == 0o700
            assert (path / "secrets").stat().st_mode & 0o777 == 0o700
            for secret in (path / "secrets").iterdir():
                assert secret.stat().st_mode & 0o777 == 0o600
        finally:
            SUPPORT["cleanup"](path)
    finally:
        os.umask(previous_umask)


def test_prepared_photos_are_real_images_carrying_the_metadata_the_journeys_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pillow_heif
    from PIL import ExifTags, Image

    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    path = private_e2e_directory()
    try:
        SUPPORT["prepare"](path)
        alice = path / "roots/alice"
        with Image.open(alice / "photo.JPG") as photo:
            photo.load()
            exif = photo.getexif()
            assert (photo.format, photo.size) == ("JPEG", (160, 120))
            assert exif[ExifTags.Base.Orientation] == 6
            assert exif.get_ifd(ExifTags.IFD.Exif)[ExifTags.Base.DateTimeOriginal] == (
                "2024:05:01 10:20:30"
            )
            location = exif.get_ifd(ExifTags.IFD.GPSInfo)
            assert [float(value) for value in location[ExifTags.GPS.GPSLatitude]] == (
                pytest.approx(list(SUPPORT["GPS_LATITUDE"]))
            )
        pillow_heif.register_heif_opener()
        for name, kind, size in (
            ("phone.heic", "HEIF", (128, 96)),
            ("albums/cover.png", "PNG", (96, 64)),
            ("albums/2024/summer/beach.jpg", "JPEG", (128, 96)),
        ):
            with Image.open(alice / name) as image:
                image.load()
                assert (image.format, image.size) == (kind, size)
        with Image.open(alice / "corrupt.jpg") as corrupt:
            # The header decodes; the scan data stops halfway.
            assert corrupt.size == (64, 64)
            with pytest.raises(OSError):
                corrupt.load()
        for name in ("photo.JPG", "phone.heic", "corrupt.jpg", "albums/cover.png"):
            assert (alice / name).stat().st_size < 64 * 1024
        # The clips come later, from the built media image (media-fixtures).
        assert not any((alice / name).exists() for name in SUPPORT["VIDEO_FIXTURES"])
    finally:
        SUPPORT["cleanup"](path)


MEDIA_IMAGE_ID = "sha256:" + "a" * 64


def _clip(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 2048


def _clip_output(clips: dict[str, bytes]) -> bytes:
    return json.dumps({
        name: base64.b64encode(content).decode("ascii") for name, content in clips.items()
    }).encode("ascii")


def _media_runner(
    commands: list[list[str]], output: bytes, returncode: int = 0, *, timeout: bool = False,
) -> Callable[..., subprocess.CompletedProcess[Any]]:
    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        del kwargs
        commands.append(list(arguments))
        if arguments[1:3] == ["image", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, MEDIA_IMAGE_ID + "\n", "")
        if timeout:
            raise subprocess.TimeoutExpired(arguments, 300)
        return subprocess.CompletedProcess(arguments, returncode, output, b"")
    return run


def _generator(path: Path) -> ProjectResource:
    """The stopped clip generator as the engine reports it: labeled by run and purpose."""
    identity = "media-fixture-container"
    return ProjectResource("container", identity, identity, json.dumps(
        {"Id": identity, "Name": identity, "Labels": {
            "com.docker.compose.project": SUPPORT["PROJECT"],
            "aegis.test.media-fixtures": "True",
            "aegis.test.resource-token": path.name,
        }}, sort_keys=True, separators=(",", ":"),
    ))


def _with_inventory(
    monkeypatch: pytest.MonkeyPatch, resources: tuple[ProjectResource, ...] = (),
) -> list[ProjectInventory]:
    """The engine's project inventory, and every exact removal the harness asks for."""
    removed: list[ProjectInventory] = []
    support_globals = SUPPORT["resources_record"].__globals__
    monkeypatch.setitem(
        support_globals, "capture_project_inventory",
        lambda project: ProjectInventory(project, resources),
    )
    monkeypatch.setitem(support_globals, "cleanup_project_inventory", removed.append)
    return removed


def test_media_fixtures_add_the_clips_from_a_mountless_offline_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    path = private_e2e_directory()
    clips = {"clip.mp4": _clip(b"isom"), "clip-hevc.mov": _clip(b"qt  ")}
    commands: list[list[str]] = []
    try:
        SUPPORT["prepare"](path)
        generator = _generator(path)
        removed = _with_inventory(monkeypatch, (generator,))
        monkeypatch.setattr(subprocess, "run", _media_runner(commands, _clip_output(clips)))

        SUPPORT["media_fixtures"](path)

        assert commands[0] == ["docker", "image", "inspect", "--format", "{{.Id}}", "aegis-media"]
        (run,) = [command for command in commands if command[1] == "run"]
        image = run.index(MEDIA_IMAGE_ID)
        options = run[2:image]
        assert run[image + 1:image + 3] == ["-I", "-c"]
        assert "--read-only" in options
        # Never auto-removed: it is recorded, then removed by its immutable ID.
        assert "--rm" not in options
        assert removed == [ProjectInventory(SUPPORT["PROJECT"], (generator,))]
        for name, value in (("--network", "none"), ("--pull", "never"), ("--cap-drop", "ALL"),
                            ("--entrypoint", "python"), ("--user", "10001:10001")):
            assert options[options.index(name) + 1] == value
        assert options[options.index("--tmpfs") + 1].startswith("/tmp:")
        # No bind, volume, port, environment or extra privilege: only stdout comes back.
        assert not {
            "-v", "--volume", "--mount", "-p", "--publish", "-e", "--env", "--env-file",
            "--cap-add", "--privileged", "--group-add",
        } & set(options)
        assert [options[index + 1] for index, option in enumerate(options)
                if option == "--label"] == [
            "com.docker.compose.project=aegis-phase1-e2e",
            f"aegis.test.resource-token={path.name}",
            "aegis.test.media-fixtures=True",
        ]
        alice = path / "roots/alice"
        ledger = SUPPORT["_load_ledger"](path)
        for name, content in clips.items():
            assert (alice / name).read_bytes() == content
            assert f"roots/alice/{name}" in ledger["entries"]
        assert ledger["resources"] == []
        assert alice.stat().st_mtime_ns == SUPPORT["DIRECTORY_MTIME_NS"]
        # The manifest was re-recorded in place, so the sources verify with the clips.
        summary = SUPPORT["verify_sources"](path)
        recorded, _ = SUPPORT["_load_source_manifest"](path, ledger)
        assert recorded["roots/alice/clip.mp4"]["sha256"] == hashlib.sha256(
            clips["clip.mp4"],
        ).hexdigest()
        assert summary["files"] == sum(1 for value in recorded.values()
                                       if value["kind"] == "file")
        assert SUPPORT["fixture_digest"](path) == hashlib.sha256(
            (alice / "photo.JPG").read_bytes(),
        ).hexdigest()
    finally:
        SUPPORT["cleanup"](path)


@pytest.mark.parametrize("failure", ("rendered", "returncode", "output", "magic", "large",
                                     "timeout"))
def test_media_fixtures_refuse_late_runs_failures_and_invalid_clips_and_remove_the_container(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    path = private_e2e_directory()
    clips = {"clip.mp4": _clip(b"isom"), "clip-hevc.mov": _clip(b"qt  ")}
    output = _clip_output(clips)
    if failure == "output":
        output = _clip_output({"clip.mp4": clips["clip.mp4"]})
    elif failure == "magic":
        output = _clip_output({**clips, "clip.mp4": b"\x00" * 4096})
    elif failure == "large":
        # Above the bound, so a clip can never reach Phase 2A's 1 MB size filter.
        output = _clip_output({**clips, "clip.mp4": _clip(b"isom") + b"\x00" * (256 << 10)})
    commands: list[list[str]] = []
    messages = {
        "rendered": "out of order", "returncode": "generation failed",
        "output": "output is invalid", "magic": "output is invalid", "large": "output is invalid",
        "timeout": "generation failed",
    }
    try:
        SUPPORT["prepare"](path)
        if failure == "rendered":
            complete_runtime_inputs(path)
        generator = _generator(path)
        removed = _with_inventory(monkeypatch, (generator,))
        monkeypatch.setattr(subprocess, "run", _media_runner(
            commands, output, 1 if failure == "returncode" else 0, timeout=failure == "timeout",
        ))
        with pytest.raises(ValueError, match=messages[failure]):
            SUPPORT["media_fixtures"](path)
        if failure == "rendered":
            assert commands == [] and removed == []
        else:
            # Whatever happened to the run, its container is removed by exact identity.
            assert removed == [ProjectInventory(SUPPORT["PROJECT"], (generator,))]
        alice = path / "roots/alice"
        assert not any((alice / name).exists() for name in clips)
        assert SUPPORT["_load_ledger"](path)["resources"] == []
        SUPPORT["verify_sources"](path)
    finally:
        SUPPORT["cleanup"](path)


def test_media_wait_is_bounded_counts_states_and_publishes_only_fixed_refusals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    path = private_e2e_directory()
    clock = iter(range(0, 10_000, 30))
    support = SUPPORT["media_wait"].__globals__
    monkeypatch.setitem(support, "_monotonic", lambda: next(clock))
    monkeypatch.setitem(support, "_sleep", lambda seconds: None)
    states = dict.fromkeys(SUPPORT["MEDIA_SETTLED"], "ready") | {
        "clip-hevc.mov": "pending", "corrupt.jpg": "pending",
    }
    del states["phone.heic"]
    monkeypatch.setitem(support, "_media_states", lambda base, password: states)
    monkeypatch.setenv("E2E_ALICE_PASSWORD", "private-sentinel")
    monkeypatch.setenv("AEGIS_PUBLIC_URL", "http://127.0.0.1:18080")
    try:
        with pytest.raises(ValueError, match="did not settle") as raised:
            SUPPORT["media_wait"](path)
        # Counts per fixed state only: never a name, a value or a credential.
        assert capsys.readouterr().err == (
            "AEGIS_E2E media-wait states=pending:2,ready:4,missing:1\n"
        )
        assert SUPPORT["refusal_annotation"]("media-wait", raised.value) == (
            "::error title=e2e-support media-wait::"
            "ValueError: synthetic E2E media did not settle"
        )
    finally:
        path.rmdir()


def test_media_wait_returns_once_every_fixture_settled(monkeypatch: pytest.MonkeyPatch) -> None:
    path = private_e2e_directory()
    settled = dict.fromkeys(SUPPORT["MEDIA_SETTLED"], "ready") | {"corrupt.jpg": "quarantined"}
    reports = iter([{}, settled | {"corrupt.jpg": "pending"}, settled])
    observed: list[str] = []
    support = SUPPORT["media_wait"].__globals__
    monkeypatch.setitem(support, "_sleep", lambda seconds: None)

    def report(base: str, password: str) -> dict[str, str]:
        observed.append(base)
        return next(reports)

    monkeypatch.setitem(support, "_media_states", report)
    monkeypatch.setenv("E2E_ALICE_PASSWORD", "a")
    monkeypatch.setenv("AEGIS_PUBLIC_URL", "http://127.0.0.1:18080")
    try:
        SUPPORT["media_wait"](path)
        assert observed == ["http://127.0.0.1:18080"] * 3
    finally:
        path.rmdir()


def test_media_states_read_alice_timeline_thumbnail_states_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = "0" * 8 + "-0000-0000-0000-" + "0" * 12
    routes: list[str] = []

    def api(opener: object, base: str, method: str, route: str, *args: Any) -> tuple[int, Any]:
        del opener, base, args
        routes.append(f"{method} {route}")
        if route == "/api/v1/roots":
            return 200, {"roots": [{"id": root}]}
        return 200, {"items": [
            {"displayName": "photo.JPG", "media": {"thumbnail": "ready"}},
            {"displayName": "corrupt.jpg", "media": {"thumbnail": "failed"}},
            {"displayName": "notes.md", "media": None},
            {"displayName": "odd.jpg", "media": {"thumbnail": "<script>"}},
        ]}

    support = SUPPORT["media_wait"].__globals__
    monkeypatch.setitem(support, "_session", lambda base, username, password: object())
    monkeypatch.setitem(support, "_api", api)
    assert SUPPORT["_media_states"]("http://127.0.0.1:18080", "a") == {
        "photo.JPG": "ready", "corrupt.jpg": "failed",
    }
    assert routes == ["GET /api/v1/roots", f"GET /api/v1/roots/{root}/timeline?limit=120"]


@pytest.mark.parametrize("directory", ("/", "/tmp", "/Users", "/srv/aegis"))
def test_cleanup_refuses_broad_or_unowned_directory_targets(directory: str) -> None:
    with pytest.raises((ValueError, OSError)):
        SUPPORT["checked_directory"](directory)


def _safe_compose() -> dict:
    services = {}
    for role in ("web", "migrate", "operations", "indexer", "media", "gateway"):
        services[role] = {
            "read_only": True, "cap_drop": ["ALL"], "networks": {"backend": {}},
            "volumes": [] if role in ("web", "migrate") else [
                {"type": "bind", "source": f"/fixture/{name}",
                 "target": f"/srv/aegis/roots/e2e-{name}", "read_only": True}
                for name in ("alice", "bob")
            ],
        }
    # The media decoder: no network at all, no credential, the roots read-only (spec M2).
    services["media-processor"] = {
        "read_only": True, "cap_drop": ["ALL"], "network_mode": "none",
        "volumes": [
            {"type": "volume", "source": "media-scratch", "target": "/srv/aegis/scratch"},
            *(dict(volume) for volume in services["media"]["volumes"]),
        ],
    }
    services["gateway"]["ports"] = [{"host_ip": "127.0.0.1"}]
    services["postgres"] = {"ports": [{"host_ip": "127.0.0.1"}]}
    return {
        "name": "aegis-phase1-e2e", "services": services,
        "networks": {"backend": {"internal": True}},
        "volumes": {"postgres-data": {"name": "aegis-phase1-e2e_postgres-data"}},
    }


@pytest.mark.parametrize("drift", (
    "protected_volume", "external", "writable", "egress", "port", "processor_network",
    "processor_bridge", "processor_secret", "processor_setting", "processor_writable",
    "processor_missing",
))
def test_compose_gate_rejects_unsafe_resource_or_original_mount_changes(drift: str) -> None:
    original = _safe_compose()
    SUPPORT["check_compose"](original)
    changed = deepcopy(original)
    if drift == "protected_volume":
        changed["volumes"]["postgres-data"]["name"] = "aegis_postgres-data"
    elif drift == "external":
        changed["volumes"]["postgres-data"]["external"] = True
    elif drift == "writable":
        changed["services"]["operations"]["volumes"][0]["read_only"] = False
    elif drift == "egress":
        changed["services"]["indexer"]["networks"]["edge"] = {}
    elif drift == "processor_network":
        changed["services"]["media-processor"]["networks"] = {"backend": {}}
    elif drift == "processor_bridge":
        changed["services"]["media-processor"]["network_mode"] = "bridge"
    elif drift == "processor_secret":
        changed["services"]["media-processor"]["secrets"] = [{"source": "django-secret-key"}]
    elif drift == "processor_setting":
        changed["services"]["media-processor"]["environment"] = {"AEGIS_DB_HOST": "postgres"}
    elif drift == "processor_writable":
        changed["services"]["media-processor"]["volumes"][1]["read_only"] = False
    elif drift == "processor_missing":
        del changed["services"]["media-processor"]
    else:
        changed["services"]["postgres"]["ports"][0]["host_ip"] = "0.0.0.0"
    with pytest.raises(ValueError):
        SUPPORT["check_compose"](changed)


RESTART_POLICIES = (
    {"restart": "unless-stopped"}, {"restart": "always"}, {"restart": "on-failure:3"},
    {"deploy": {"restart_policy": {}}}, {"deploy": {"restart_policy": {"condition": "any"}}},
    {"restart": "no", "deploy": {"restart_policy": {"condition": "on-failure"}}},
)


@pytest.mark.parametrize("policy", RESTART_POLICIES)
@pytest.mark.parametrize("role", ("postgres", "web", "gateway"))
def test_compose_gate_refuses_any_restart_policy(role: str, policy: dict[str, Any]) -> None:
    # A host boot or engine restart must never start a test container.
    changed = _safe_compose()
    changed["services"][role].update(deepcopy(policy))
    message = r"^E2E services must not have a restart policy$"
    with pytest.raises(ValueError, match=message) as refused:
        SUPPORT["check_compose"](changed)
    assert SUPPORT["refusal_annotation"]("check-compose", refused.value) == (
        "::error title=e2e-support check-compose::"
        "ValueError: E2E services must not have a restart policy"
    )


def test_compose_gate_accepts_absent_or_disabled_restart_policies() -> None:
    config = _safe_compose()
    config["services"]["migrate"]["restart"] = "no"
    config["services"]["web"]["deploy"] = {"restart_policy": {"condition": "none"}}
    config["services"]["gateway"]["deploy"] = {"resources": {}}
    SUPPORT["check_compose"](config)


def test_test_profile_keeps_fixture_credentials_out_of_the_production_compose() -> None:
    repository = Path(__file__).resolve().parents[2]
    assert "e2e-alice-password" not in (repository / "compose.yaml").read_text()
    assert "e2e-alice-password" in (repository / "compose.test.yaml").read_text()
    assert os.access(repository / "scripts/test-e2e.sh", os.X_OK)


def private_e2e_directory() -> Path:
    path = Path(tempfile.mkdtemp(prefix="aegis-phase1-e2e.", dir="/tmp"))
    path.chmod(0o700)
    return path


def complete_runtime_inputs(path: Path) -> None:
    for name in SUPPORT["GENERATED_NAMES"]:
        target = path / name
        if not target.exists():
            target.write_text("generated\n", encoding="ascii")
    SUPPORT["record_generated"](path)


def test_podman_preparation_targets_only_inventoried_synthetic_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    provider = tmp_path / "provider"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)
    commands: list[list[str]] = []

    def completed(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(arguments)
        output = ""
        if arguments[2:4] == ["unshare", "stat"]:
            output = "1000:1000\n" * (len(SUPPORT["SECRET_NAMES"]) + 1)
        return subprocess.CompletedProcess(arguments, 0, output, "")

    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")
        monkeypatch.setattr(subprocess, "run", completed)

        SUPPORT["prepare_sources"](path)
        complete_runtime_inputs(path)
        SUPPORT["prepare_runtime"](path)

        secrets = [str(path / "secrets" / name) for name in SUPPORT["SECRET_NAMES"]]
        label_targets = [
            *secrets,
            str(path / "mounts.manifest.json"),
            str(path / "mounts.gateway.attestation"),
        ]
        # Every recorded entry of the two mounted source roots; never the unmounted sibling.
        mounted = sorted(
            name for name in SUPPORT["_load_ledger"](path)["entries"]
            if name.startswith(("roots/alice/", "roots/bob/")) or name in (
                "roots/alice", "roots/bob",
            )
        )
        assert len(mounted) > 600 and "roots/alice/link-outside" in mounted
        assert commands == [
            ["chcon", "--no-dereference", "--type", "container_file_t", "--",
             *(str(path / name) for name in mounted)],
            ["chcon", "--no-dereference", "--type", "container_file_t", "--", *label_targets],
            ["podman", "--remote=false", "unshare", "chown", "--no-dereference", "1000:1000", "--",
             *secrets, str(path / "mounts.manifest.json")],
            ["podman", "--remote=false", "unshare", "stat", "--format=%u:%g", "--",
             *secrets, str(path / "mounts.manifest.json")],
        ]
    finally:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_e2e_mapping_refuses_added_entry_without_updating_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    unknown = path / "added-during-map"
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        ledger_path = path / SUPPORT["LEDGER_NAME"]
        original_ledger = ledger_path.read_bytes()
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")

        def completed(
            arguments: list[str], **kwargs: Any,
        ) -> subprocess.CompletedProcess[str]:
            del kwargs
            if arguments[2:4] == ["unshare", "chown"]:
                unknown.write_text("retain\n", encoding="ascii")
            output = ""
            if arguments[2:4] == ["unshare", "stat"]:
                output = "1000:1000\n" * (len(SUPPORT["SECRET_NAMES"]) + 1)
            return subprocess.CompletedProcess(arguments, 0, output, "")

        monkeypatch.setattr(subprocess, "run", completed)
        with pytest.raises(ValueError, match="ownership inventory changed"):
            SUPPORT["prepare_runtime"](path)
        assert ledger_path.read_bytes() == original_ledger
        assert unknown.read_text(encoding="ascii") == "retain\n"
    finally:
        unknown.unlink(missing_ok=True)
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


@pytest.mark.parametrize("field", (stat.ST_UID, stat.ST_GID))
def test_e2e_preparation_refuses_recorded_owner_drift_before_mutation(
    monkeypatch: pytest.MonkeyPatch, field: int,
) -> None:
    path = private_e2e_directory()
    commands: list[list[str]] = []
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        target = path / "mounts.manifest.json"
        original_lstat = Path.lstat

        def changed(candidate: Path) -> os.stat_result:
            metadata = original_lstat(candidate)
            if candidate == target:
                values = list(metadata)
                values[field] += 1
                return os.stat_result(values)
            return metadata

        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setattr(Path, "lstat", changed)
        monkeypatch.setattr(
            subprocess, "run", lambda arguments, **kwargs: commands.append(arguments),
        )

        with pytest.raises(ValueError, match="inventory changed"):
            SUPPORT["prepare_runtime"](path)
        assert commands == []
    finally:
        monkeypatch.setattr(Path, "lstat", original_lstat)
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_podman_preparation_refuses_replaced_or_unknown_inputs_before_ownership_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    provider = tmp_path / "provider"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)
    ownership_called = False
    original_manifest = path / "original-manifest"

    def replace_manifest(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal ownership_called
        del kwargs
        if arguments[:4] == ["podman", "--remote=false", "unshare", "chown"]:
            ownership_called = True
        else:
            manifest = path / "mounts.manifest.json"
            manifest.rename(original_manifest)
            manifest.symlink_to(path / "mounts.toml")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        complete_runtime_inputs(path)
        support_globals = SUPPORT["prepare_runtime"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setitem(
            support_globals, "container_command",
            lambda *arguments: ["podman", "--remote=false", *arguments],
        )
        monkeypatch.setenv("AEGIS_UID", "1000")
        monkeypatch.setenv("AEGIS_GID", "1000")
        monkeypatch.setattr(subprocess, "run", replace_manifest)

        with pytest.raises(ValueError, match=r"inventory|replaced"):
            SUPPORT["prepare_runtime"](path)
        assert ownership_called is False

        (path / "mounts.manifest.json").unlink()
        original_manifest.rename(path / "mounts.manifest.json")
        (path / "unexpected").write_text("retain\n", encoding="ascii")
        with pytest.raises(ValueError, match="unknown"):
            SUPPORT["prepare_runtime"](path)
        with pytest.raises(ValueError, match="unknown"):
            SUPPORT["cleanup"](path)
        assert (path / "secrets/e2e-alice-password").exists()
    finally:
        (path / "unexpected").unlink(missing_ok=True)
        if original_manifest.exists():
            (path / "mounts.manifest.json").unlink(missing_ok=True)
            original_manifest.rename(path / "mounts.manifest.json")
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["cleanup"](path)


def test_e2e_ledger_refuses_intermediate_symlink_hardlink_and_known_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    path = private_e2e_directory()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "alice").mkdir()
    (outside / "bob").mkdir()
    commands: list[list[str]] = []
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        roots = path / "roots"
        saved = path / "saved-roots"
        roots.rename(saved)
        roots.symlink_to(outside, target_is_directory=True)
        support_globals = SUPPORT["prepare_sources"].__globals__
        monkeypatch.setitem(support_globals, "selected_engine", lambda: "podman")
        monkeypatch.setattr(
            subprocess, "run", lambda arguments, **kwargs: commands.append(arguments),
        )
        with pytest.raises(ValueError, match=r"inventory|replaced"):
            SUPPORT["prepare_sources"](path)
        assert commands == []
        roots.unlink()
        saved.rename(roots)

        original = tmp_path / "user-original"
        original.write_text("preserve", encoding="ascii")
        linked = path / "unexpected-hardlink"
        os.link(original, linked)
        with pytest.raises(ValueError, match="hardlink"):
            SUPPORT["cleanup"](path)
        assert original.read_text(encoding="ascii") == "preserve"
        assert (path / "secrets/e2e-alice-password").exists()
        linked.unlink()
    finally:
        if path.exists():
            monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
            SUPPORT["cleanup"](path)


def test_e2e_resource_cleanup_refusal_preserves_complete_filesystem_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        cleanup_globals = SUPPORT["resources_cleanup"].__globals__

        def refuse(_inventory: object) -> None:
            raise RuntimeError("unknown project resource")

        monkeypatch.setitem(cleanup_globals, "cleanup_project_inventory", refuse)
        with pytest.raises(RuntimeError, match="unknown project resource"):
            SUPPORT["resources_cleanup"](path)
        assert (path / "secrets/e2e-alice-password").exists()
        assert (path / "roots/alice").is_dir()
        assert (path / SUPPORT["LEDGER_NAME"]).exists()
    finally:
        SUPPORT["cleanup"](path)


def test_e2e_resource_record_refuses_unknown_addition_without_overwriting_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = private_e2e_directory()
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        resource = ProjectResource(
            "container", "unknown-id", "unknown-id",
            json.dumps({
                "Id": "unknown-id", "Created": "now", "Name": "foreign-postgres",
                "Image": "image", "Labels": {
                    "com.docker.compose.project": SUPPORT["PROJECT"],
                    "com.docker.compose.service": "postgres",
                    "com.docker.compose.oneoff": "False",
                    "aegis.test.resource-token": "foreign-token",
                },
            }, sort_keys=True, separators=(",", ":")),
        )
        record_globals = SUPPORT["resources_record"].__globals__
        monkeypatch.setitem(
            record_globals, "capture_project_inventory",
            lambda project: ProjectInventory(project, (resource,)),
        )

        with pytest.raises(RuntimeError, match="unexpected"):
            SUPPORT["resources_record"](path, "up")
        assert SUPPORT["_stored_resources"](SUPPORT["_load_ledger"](path)).resources == ()
    finally:
        SUPPORT["cleanup"](path)


def test_e2e_reads_host_credentials_before_subordinate_ownership_preparation() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/test-e2e.sh").read_text()

    # Locate the commands themselves, never the public phase-name allowlist.
    preparation = script.index("e2e_support.py prepare-runtime ")
    for variable in ("E2E_ALICE_PASSWORD", "E2E_BOB_PASSWORD", "E2E_ADMIN_PASSWORD"):
        assert script.index(f"read -r {variable}") < preparation
    assert script.index("e2e_support.py prepare-sources ") < script.index(
        "aegisctl mounts preflight "
    )


def test_e2e_media_fixtures_follow_the_build_and_precede_every_source_reader() -> None:
    script = (Path(__file__).resolve().parents[2] / "scripts/test-e2e.sh").read_text()

    build = script.index('"${compose_base[@]}" build')
    fixtures = script.index("e2e_support.py media-fixtures ")
    assert script.index("e2e_support.py prepare ") < build < fixtures
    for reader in ("e2e_support.py prepare-sources ", "aegisctl mounts preflight ",
                   "aegisctl mounts render ", "controlled_compose up "):
        assert fixtures < script.index(reader), reader
    digest = script.index("E2E_PHOTO_SHA256=$(uv run python scripts/e2e_support.py fixture-digest ")
    media_wait = script.index("e2e_support.py media-wait ")
    assert script.index("e2e_support.py index-wait ") < media_wait < digest
    assert digest < script.index("run test:e2e")
    assert "export E2E_ALICE_PASSWORD E2E_BOB_PASSWORD E2E_ADMIN_PASSWORD E2E_PHOTO_SHA256" in (
        script
    )


def test_only_the_e2e_harness_shortens_media_passes_within_their_bounds() -> None:
    from aegis_apps.media.config import DISCOVERY_INTERVAL, ROOTS_REFRESH, MediaPolicy

    environment = _compose_file("compose.test.yaml")["services"]["media"]["environment"]
    script = (Path(__file__).resolve().parents[2] / "scripts/test-e2e.sh").read_text()
    exported = {}
    for name, default, _minimum, _maximum in (DISCOVERY_INTERVAL, ROOTS_REFRESH):
        # Unset (benchmarks, verification), the overlay passes the production default.
        assert environment[name] == f"${{{name}:-{default}}}"
        match = re.search(rf"^export {name}=([0-9]+)$", script, re.MULTILINE)
        assert match is not None, name
        exported[name] = match.group(1)
    policy = MediaPolicy.from_environ(exported)
    assert (policy.discovery_interval_seconds, policy.roots_refresh_seconds) == (5, 5)
    production = _compose_file("compose.yaml")["services"]["media"]["environment"]
    assert not {name for name, *_ in (DISCOVERY_INTERVAL, ROOTS_REFRESH)} & set(production)


def test_e2e_media_fixtures_are_generated_and_never_committed() -> None:
    from aegis_apps.media.formats import MEDIA_EXTENSIONS

    # Every extension the media allowlist (spec M4) recognizes.
    suffixes = frozenset(f".{extension}" for extension in MEDIA_EXTENSIONS)
    assert {".jpg", ".heic", ".mp4", ".mov"} <= suffixes
    repository = Path(__file__).resolve().parents[2]
    listed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=repository, check=True, capture_output=True, timeout=30,
    ).stdout.split(b"\0")
    tracked = [os.fsdecode(name) for name in listed if name]
    assert tracked, "no tracked files found"
    committed = [name for name in tracked if Path(name).suffix.lower() in suffixes]
    assert committed == [], "media fixtures are generated at e2e time, never committed"


class _ComposeLoader(yaml.SafeLoader):
    """Resource-free Compose YAML reader: merge tags only change override semantics."""


def _compose_tag(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    assert isinstance(node, yaml.ScalarNode)
    return loader.construct_scalar(node)


for _tag in ("!override", "!reset"):
    _ComposeLoader.add_constructor(_tag, _compose_tag)

E2E_COMPOSE_FILES = ("compose.yaml", "compose.test.yaml", "compose.podman.yaml")
TOKEN_LABEL = "aegis.test.resource-token"
TOKEN_EXPRESSION = "${AEGIS_TEST_RESOURCE_TOKEN:?AEGIS_TEST_RESOURCE_TOKEN is required}"


def _compose_file(name: str) -> dict[str, Any]:
    repository = Path(__file__).resolve().parents[2]
    loaded = yaml.load((repository / name).read_text(encoding="utf-8"), _ComposeLoader)
    assert isinstance(loaded, dict)
    return loaded


def _service_networks(service: dict[str, Any]) -> set[str]:
    networks = service.get("networks")
    if networks is None:
        return set() if "network_mode" in service else {"default"}
    return set(networks)


def _service_named_volumes(service: dict[str, Any]) -> set[str]:
    named = set()
    for mount in service.get("volumes", []):
        if isinstance(mount, str):
            source, separator, _target = mount.partition(":")
            if separator and not source.startswith(("/", ".", "~", "$")):
                named.add(source)
        elif mount.get("type") == "volume" and mount.get("source"):
            named.add(mount["source"])
    return named


def _e2e_project_resources() -> tuple[set[str], set[str], set[str]]:
    """Services, networks and volumes the canonical E2E file set can create.

    Every enabled (profile-free) service's networks and named volumes, plus any
    declared top-level resource that no service uses: rules are at-most-once
    allowances, so admitting an unused declaration is safe whether or not the
    Compose release prunes it. Resources used only by profile-gated services are
    never created by the E2E ``up`` and must not be admitted.
    """
    files = [_compose_file(name) for name in E2E_COMPOSE_FILES]
    canonical = files[0]["services"]
    enabled = {name for name, service in canonical.items() if not service.get("profiles")}
    used: dict[bool, dict[str, set[str]]] = {
        state: {"networks": set(), "volumes": set()} for state in (True, False)
    }
    declared: dict[str, set[str]] = {"networks": set(), "volumes": set()}
    for document in files:
        for kind in declared:
            declared[kind] |= set(document.get(kind) or {})
        for name, service in (document.get("services") or {}).items():
            assert name in canonical, "an E2E overlay introduced an unreviewed service"
            state = name in enabled
            if document is files[0] or "networks" in service:
                used[state]["networks"] |= _service_networks(service)
            used[state]["volumes"] |= _service_named_volumes(service)
    expected = {
        kind: (declared[kind] | used[True][kind]) - (used[False][kind] - used[True][kind])
        for kind in declared
    }
    return enabled, expected["networks"], expected["volumes"]


def _rule_names(rules: tuple[Any, ...], kind: str, label: str) -> list[str]:
    return [
        dict(rule.required_labels)[label] for rule in rules if rule.kind == kind
    ]


def test_e2e_up_admission_exactly_covers_rendered_project_resources() -> None:
    services, networks, volumes = _e2e_project_resources()
    assert "tls-hop" in networks  # gateway's private TLS hop (compose.yaml).
    assert not {"caddy-data", "caddy-local-data"} & volumes  # TLS profiles only.
    token = "aegis-phase1-e2e.abcdefgh"
    rules = SUPPORT["_resource_rules"]("up", token)
    assert sorted(_rule_names(rules, "container", "com.docker.compose.service")) == sorted(
        services
    )
    assert sorted(_rule_names(rules, "network", "com.docker.compose.network")) == sorted(
        networks
    )
    assert sorted(_rule_names(rules, "volume", "com.docker.compose.volume")) == sorted(volumes)
    assert all((TOKEN_LABEL, token) in rule.required_labels for rule in rules)
    assert {rule.kind for rule in rules} == {"container", "network", "volume"}

    test_overlay = _compose_file("compose.test.yaml")
    for kind, expected in (("services", services), ("networks", networks),
                           ("volumes", volumes)):
        labelled = {
            name for name, entry in (test_overlay.get(kind) or {}).items()
            if (entry or {}).get("labels", {}).get(TOKEN_LABEL) == TOKEN_EXPRESSION
        }
        assert labelled == expected, f"compose.test.yaml {kind} provenance labels drifted"

    def labelled_resource(kind: str, name: str, key: str, extra: dict[str, str]) -> Any:
        labels = {
            "com.docker.compose.project": SUPPORT["PROJECT"], key: name, **extra,
        }
        section = {"container": "services", "network": "networks", "volume": "volumes"}[kind]
        entry = (test_overlay.get(section) or {}).get(name) or {}
        if entry.get("labels", {}).get(TOKEN_LABEL) == TOKEN_EXPRESSION:
            labels[TOKEN_LABEL] = token  # Compose applies the overlay's provenance label.
        identity = f"{kind}-{name}"
        return ProjectResource(kind, identity, identity, json.dumps(
            {"Id": identity, "Name": identity, "Labels": labels},
            sort_keys=True, separators=(",", ":"),
        ))

    created = (
        *(labelled_resource("container", name, "com.docker.compose.service",
                            {"com.docker.compose.oneoff": "False"}) for name in services),
        *(labelled_resource("network", name, "com.docker.compose.network", {})
          for name in networks),
        *(labelled_resource("volume", name, "com.docker.compose.volume", {})
          for name in volumes),
    )
    empty = ProjectInventory(SUPPORT["PROJECT"], ())
    observed = ProjectInventory(SUPPORT["PROJECT"], tuple(sorted(created)))
    assert admit_project_transition(empty, observed, rules) == observed

    # Admission stays exact: an unknown network with valid provenance is refused.
    foreign = ProjectResource("network", "foreign", "foreign", json.dumps(
        {"Id": "foreign", "Name": "foreign", "Labels": {
            "com.docker.compose.project": SUPPORT["PROJECT"],
            "com.docker.compose.network": "foreign", TOKEN_LABEL: token,
        }}, sort_keys=True, separators=(",", ":"),
    ))
    with pytest.raises(ProjectResourceError, match="unexpected"):
        admit_project_transition(
            empty, ProjectInventory(SUPPORT["PROJECT"], (*observed.resources, foreign)), rules,
        )


def test_e2e_refusal_is_one_bounded_public_annotation_only_under_github_actions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    path = private_e2e_directory()
    try:
        monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
        SUPPORT["prepare"](path)
        record_globals = SUPPORT["main"].__globals__

        def refuse(project: str) -> ProjectInventory:
            raise ProjectResourceError("unexpected project resource transition")

        monkeypatch.setitem(record_globals, "capture_project_inventory", refuse)
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
        with pytest.raises(ProjectResourceError):
            SUPPORT["main"](["resources-record", str(path), "up"])
        assert capsys.readouterr().out == ""

        monkeypatch.setenv("GITHUB_ACTIONS", "true")
        with pytest.raises(ProjectResourceError):
            SUPPORT["main"](["resources-record", str(path), "up"])
        assert capsys.readouterr().out.splitlines() == [
            "::error title=e2e-support resources-record::"
            "ProjectResourceError: unexpected project resource transition",
        ]
    finally:
        SUPPORT["cleanup"](path)


@pytest.mark.parametrize("error", (
    ValueError("invalid literal /tmp/aegis-phase1-e2e.private-sentinel"),
    ValueError("line one\nprivate-sentinel=%s"),
    ValueError("x" * 201),
    ValueError("tmp/aegis-phase1-e2e.private-sentinel"),
    ValueError("private-sentinel"),
    ProjectResourceError("private-sentinel project resource query failed"),
    RuntimeError("unexpected project resource transition"),
    KeyError("private-sentinel"),
    OSError(2, "No such file", "/srv/aegis/roots/private-sentinel"),
))
def test_e2e_refusal_annotation_omits_unfixed_or_path_details(error: Exception) -> None:
    annotation = SUPPORT["refusal_annotation"]("prepare", error)
    assert annotation == (
        f"::error title=e2e-support prepare::{type(error).__name__} (details omitted)"
    )
    assert "\n" not in annotation and "private-sentinel" not in annotation
    unknown = SUPPORT["refusal_annotation"]("x,y:%\n", ValueError("unknown E2E support action"))
    assert unknown == (
        "::error title=e2e-support unknown-action::ValueError: unknown E2E support action"
    )
    assert SUPPORT["_workflow_escape"]("a%b\r\nc:d,e", property_value=True) == (
        "a%25b%0D%0Ac%3Ad%2Ce"
    )


def test_e2e_public_refusals_are_exactly_the_constant_raised_messages() -> None:
    import ast

    repository = Path(__file__).resolve().parents[2]
    raised: dict[str, set[str]] = {"ValueError": set(), "ProjectResourceError": set()}
    for name in ("scripts/e2e_support.py", "backend/aegisctl/container_resources.py"):
        for node in ast.walk(ast.parse((repository / name).read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name) and node.exc.func.id in raised
            ):
                argument = node.exc.args[0]
                assert isinstance(argument, ast.Constant) and isinstance(argument.value, str)
                raised[node.exc.func.id].add(argument.value)
    public = SUPPORT["_PUBLIC_REFUSALS"]
    assert {kind.__name__: set(messages) for kind, messages in public.items()} == raised


SKIP_CALL = re.compile(r"\.\s*(?:skip|fixme)\s*\(")


def test_e2e_specs_never_skip_or_defer_a_journey() -> None:
    specs = sorted((Path(__file__).resolve().parents[2] / "frontend/e2e").glob("*.spec.ts"))
    assert specs, "no e2e specs found"
    offenders = []
    for spec in specs:
        text = spec.read_text(encoding="utf-8")
        for call in SKIP_CALL.finditer(text):
            offenders.append(f"{spec.name}:{text.count(chr(10), 0, call.start()) + 1}")
    assert offenders == [], (
        "E2E journeys never skip themselves: a browser that lacks a capability takes an "
        "explicit, asserted branch instead (HANDOFF: do not replace failures with skips)."
    )


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


UNROUTE_CALL = re.compile(r"\.\s*unroute(?:All)?\s*\(")


def test_e2e_specs_never_tear_playwright_routes_down() -> None:
    specs = sorted((Path(__file__).resolve().parents[2] / "frontend/e2e").glob("*.spec.ts"))
    assert specs, "no e2e specs found"
    offenders = []
    for spec in specs:
        text = spec.read_text(encoding="utf-8")
        for call in UNROUTE_CALL.finditer(text):
            offenders.append(f"{spec.name}:{text.count(chr(10), 0, call.start()) + 1}")
    assert offenders == [], (
        "E2E specs must make a finished route pass-through instead of calling unroute or "
        "unrouteAll, because on Chromium any route intercepts every request and tearing it down "
        "mid-test can strand a request the app has in flight."
    )
