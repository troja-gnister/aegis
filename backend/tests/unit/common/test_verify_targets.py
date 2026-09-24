from __future__ import annotations

import json
import os
import runpy
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[4]
RUNNER = REPOSITORY / "scripts/verify.py"


def configure_podman(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    provider = tmp_path / "docker-compose"
    provider.write_text("provider", encoding="ascii")
    provider.chmod(0o700)
    socket_parent = tmp_path / "podman-service"
    socket_parent.mkdir(mode=0o700)
    socket_path = socket_parent / "podman.sock"
    socket_path.touch(mode=0o600)
    real_lstat = Path.lstat

    def fake_lstat(path: Path) -> os.stat_result:
        metadata = real_lstat(path)
        values = list(metadata)
        if path == socket_path:
            values[stat.ST_MODE] = stat.S_IFSOCK | stat.S_IMODE(metadata.st_mode)
        elif path == Path("/tmp"):
            values[stat.ST_UID] = 0
        return os.stat_result(values)

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "podman")
    monkeypatch.setenv("PODMAN_COMPOSE_PROVIDER", str(provider))
    monkeypatch.setenv("AEGIS_PODMAN_SOCKET", str(socket_path))
    return provider, socket_path


def invoke_runner(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str],
) -> tuple[Any, list[list[str]], list[str]]:
    helpers = runpy.run_path(str(RUNNER))
    commands: list[list[str]] = []
    database_events: list[str] = []

    @contextmanager
    def database() -> Iterator[dict[str, str]]:
        database_events.append("created")
        try:
            yield {"isolated": "yes"}
        finally:
            database_events.append("removed")

    def run(arguments: list[str], env: dict[str, str]) -> None:
        assert env == {"isolated": "yes"}
        commands.append(arguments)

    monkeypatch.setattr(sys, "argv", [str(RUNNER), *arguments])
    monkeypatch.setitem(helpers["main"].__globals__, "test_database", database)
    monkeypatch.setitem(helpers["main"].__globals__, "run", run)
    return helpers["main"], commands, database_events


@pytest.mark.parametrize(
    ("mode", "target"),
    [
        ("backend", "../backend/tests"),
        ("backend", str(REPOSITORY / "backend/tests")),
        ("backend", "--collect-only"),
        ("backend", ""),
        ("backend", "backend/tests/unit/catalog/test_names.py::"),
        ("backend", "backend/tests/unit/catalog/test_names.py::test_names[x]"),
        ("backend", "backend/tests/unit/catalog/test_names.py::-q"),
        ("backend", "backend/tests/unit/catalog/test_names.py::test_names::"),
        ("backend", "backend/tests::test_names"),
        ("backend", "backend/tests/not-present.py"),
        ("backend", "tests/deployment"),
        ("deployment", "backend/tests"),
        ("compose", "tests/deployment"),
    ],
)
def test_unsafe_targets_fail_before_database_creation(
    monkeypatch: pytest.MonkeyPatch, mode: str, target: str,
) -> None:
    main, commands, events = invoke_runner(monkeypatch, [mode, "--test-target", target])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert commands == []
    assert events == []


def test_symlink_escape_fails_before_database_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    (tmp_path / "backend/tests").mkdir(parents=True)
    (tmp_path / "outside.py").touch()
    (tmp_path / "backend/tests/escape.py").symlink_to(tmp_path / "outside.py")
    main, commands, events = invoke_runner(
        monkeypatch, ["backend", "--test-target", "backend/tests/escape.py"],
    )
    monkeypatch.setitem(main.__globals__, "REPOSITORY", tmp_path)
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 2
    assert commands == []
    assert events == []


@pytest.mark.parametrize("mode", ["backend", "deployment"])
def test_default_targets_and_backend_checks_are_preserved(
    monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    main, commands, events = invoke_runner(monkeypatch, [mode])
    main()
    assert commands[-1] == [
        sys.executable, "-m", "pytest",
        "backend/tests" if mode == "backend" else "tests/deployment", "-q", "--tb=short",
    ]
    assert len(commands) == (3 if mode == "backend" else 1)
    assert events == ["created", "removed"]


def test_repeated_targets_and_identifier_selectors_build_fixed_pytest_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [
        "backend/tests/unit/catalog",
        "backend/tests/unit/common/test_verify_targets.py::test_default_targets_and_backend_checks_are_preserved",
    ]
    main, commands, events = invoke_runner(
        monkeypatch, ["backend", "--test-target", targets[0], "--test-target", targets[1]],
    )
    main()
    assert commands == [
        [sys.executable, "backend/manage.py", "check"],
        [sys.executable, "backend/manage.py", "makemigrations", "--check", "--dry-run"],
        [sys.executable, "-m", "pytest", *targets, "-q", "--tb=short"],
    ]
    assert events == ["created", "removed"]


def test_deployment_target_skips_backend_management_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    target = "tests/deployment/test_verification_runner.py"
    main, commands, events = invoke_runner(monkeypatch, ["deployment", "--test-target", target])
    main()
    assert commands == [[sys.executable, "-m", "pytest", target, "-q", "--tb=short"]]
    assert events == ["created", "removed"]


def test_verifier_routes_direct_and_compose_commands_to_selected_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    _provider, socket_path = configure_podman(monkeypatch, tmp_path)
    helpers = runpy.run_path(str(RUNNER))
    observed: list[list[str]] = []
    observed_environments: list[dict[str, str]] = []

    def completed(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        observed.append(arguments)
        observed_environments.append(kwargs["env"])
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(subprocess, "run", completed)

    helpers["container"]("inspect", "owned-id")
    helpers["compose"]("config", "--format", "json")

    assert observed == [
        ["podman", "--remote=false", "inspect", "owned-id"],
        ["podman", "--remote=false", "compose", "config", "--format", "json"],
    ]
    assert all(environment["AEGIS_CONTAINER_ENGINE"] == "podman"
               for environment in observed_environments)
    assert "DOCKER_HOST" not in observed_environments[0]
    assert observed_environments[1]["DOCKER_HOST"] == f"unix://{socket_path}"


def test_verifier_postgres_pin_uses_explicit_docker_hub_registry() -> None:
    helpers = runpy.run_path(str(RUNNER))

    assert helpers["POSTGRES_IMAGE"] == (
        "docker.io/library/postgres:18.6-alpine@"
        "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
    )


def test_verifier_refuses_hardlinked_bind_before_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    directory_identity = helpers["file_identity"](directory)
    original = tmp_path / "operator-original"
    original.write_text("preserve", encoding="ascii")
    password = directory / "password"
    os.link(original, password)
    password_identity = helpers["file_identity"](password)
    called = False

    def unexpected(*args: object, **kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setitem(
        helpers["prepare_verification_bind"].__globals__, "selected_engine", lambda: "podman",
    )
    monkeypatch.setattr(subprocess, "run", unexpected)
    with pytest.raises(RuntimeError, match="inventory changed"):
        helpers["prepare_verification_bind"](
            directory, directory_identity, password, password_identity,
        )
    assert called is False
    assert original.read_text(encoding="ascii") == "preserve"


def test_verifier_refuses_unknown_cleanup_entry_before_deletion(
    tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    directory_identity = helpers["file_identity"](directory)
    password_identity = helpers["file_identity"](password)

    unknown = directory / "unknown"
    unknown.write_text("retain", encoding="ascii")
    with pytest.raises(RuntimeError, match="preserving evidence"):
        helpers["cleanup_verification_bind"](
            directory, directory_identity, {password: password_identity},
        )
    assert password.exists() and unknown.exists()


def test_verifier_records_partial_create_id_before_exact_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    commands: list[tuple[str, ...]] = []
    removed = False
    identity = "d" * 64

    def fake_container(
        *arguments: str, check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal removed
        del check
        commands.append(arguments)
        if arguments[0] == "create":
            cidfile = Path(arguments[arguments.index("--cidfile") + 1])
            cidfile.write_text(identity + "\n", encoding="ascii")
            return subprocess.CompletedProcess(arguments, 125, "", "start failed")
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(
                arguments, 0, "" if removed else identity + "\n", "",
            )
        if arguments[:3] == ("container", "inspect", identity):
            # The dynamic owner is not the ID. Fill it from the create label.
            label = next(
                item.removeprefix("aegis.verify.owner=")
                for command in commands for item in command
                if item.startswith("aegis.verify.owner=")
            )
            payload: list[dict[str, Any]] = [{
                "Id": identity,
                "Config": {"Labels": {"aegis.verify.owner": label}},
            }]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ("rm", "--force"):
            removed = True
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    database_globals = helpers["test_database"].__wrapped__.__globals__
    monkeypatch.setitem(database_globals, "container", fake_container)
    monkeypatch.setitem(
        database_globals, "prepare_verification_bind", lambda *args: None,
    )
    monkeypatch.setitem(
        database_globals, "test_environment", lambda: {},
    )

    with pytest.raises(RuntimeError, match="creation failed"), helpers["test_database"]():
        pass

    assert removed is True
    assert any(command[:2] == ("rm", "--force") for command in commands)


@pytest.mark.parametrize("field", (stat.ST_UID, stat.ST_GID))
@pytest.mark.parametrize("target_name", ("directory", "password"))
def test_verifier_refuses_file_ownership_drift_before_cleanup_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: int, target_name: str,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    directory_identity = helpers["file_identity"](directory)
    files = {password: helpers["file_identity"](password)}
    original_lstat = Path.lstat
    changed_target = directory if target_name == "directory" else password

    def changed(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if path == changed_target:
            values = list(metadata)
            values[field] += 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "lstat", changed)
    with pytest.raises(RuntimeError, match="preserving evidence"):
        helpers["cleanup_verification_resources"](
            "owner", "container-id", directory, directory_identity, files,
        )
    assert password.exists()


@pytest.mark.parametrize("field", (stat.ST_UID, stat.ST_GID))
@pytest.mark.parametrize("target_name", ("directory", "password"))
def test_verifier_refuses_ownership_drift_before_bind_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, field: int, target_name: str,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    directory_identity = helpers["file_identity"](directory)
    password_identity = helpers["file_identity"](password)
    changed_target = directory if target_name == "directory" else password
    original_lstat = Path.lstat
    commands: list[list[str]] = []

    def changed(path: Path) -> os.stat_result:
        metadata = original_lstat(path)
        if path == changed_target:
            values = list(metadata)
            values[field] += 1
            return os.stat_result(values)
        return metadata

    monkeypatch.setattr(Path, "lstat", changed)
    monkeypatch.setattr(subprocess, "run", lambda arguments, **kwargs: commands.append(arguments))
    monkeypatch.setitem(
        helpers["prepare_verification_bind"].__globals__, "selected_engine", lambda: "podman",
    )
    with pytest.raises(RuntimeError, match="inventory changed"):
        helpers["prepare_verification_bind"](
            directory, directory_identity, password, password_identity,
        )
    assert commands == []


def test_verifier_prevalidates_complete_filesystem_before_resource_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    unknown = directory / "unknown"
    unknown.write_text("preserve\n", encoding="ascii")
    commands: list[tuple[str, ...]] = []
    monkeypatch.setitem(
        helpers["cleanup_verification_resources"].__globals__, "container",
        lambda *arguments, **kwargs: commands.append(arguments),
    )

    with pytest.raises(RuntimeError, match="preserving evidence"):
        helpers["cleanup_verification_resources"](
            "owner", "container-id", directory, helpers["file_identity"](directory),
            {password: helpers["file_identity"](password)},
        )
    assert commands == []
    assert password.exists() and unknown.exists()


def test_verifier_failed_initial_resource_query_preserves_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    commands: list[tuple[str, ...]] = []

    def failed(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        commands.append(arguments)
        return subprocess.CompletedProcess(arguments, 125, "", "query failed")

    monkeypatch.setitem(
        helpers["cleanup_verification_resources"].__globals__, "container", failed,
    )
    with pytest.raises(RuntimeError, match="query failed"):
        helpers["cleanup_verification_resources"](
            "owner", "container-id", directory, helpers["file_identity"](directory),
            {password: helpers["file_identity"](password)},
        )
    assert not any(command[0] == "rm" for command in commands)
    assert password.exists()


def test_verifier_failed_absence_query_preserves_files_and_reports_no_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    owner = "owned-verifier"
    identity = "f" * 64
    query_count = 0

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal query_count
        del check
        if arguments[:2] == ("container", "ls"):
            query_count += 1
            if query_count == 1:
                return subprocess.CompletedProcess(arguments, 0, identity + "\n", "")
            return subprocess.CompletedProcess(arguments, 125, "", "query failed")
        if arguments[:3] == ("container", "inspect", identity):
            payload = [{"Id": identity, "Config": {"Labels": {"aegis.verify.owner": owner}}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ("rm", "--force"):
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    monkeypatch.setitem(
        helpers["cleanup_verification_resources"].__globals__, "container", commands,
    )
    with pytest.raises(RuntimeError, match="query failed"):
        helpers["cleanup_verification_resources"](
            owner, identity, directory, helpers["file_identity"](directory),
            {password: helpers["file_identity"](password)},
        )
    assert password.exists()
    assert "Removed disposable" not in capsys.readouterr().out


def test_verifier_podman_cleanup_accepts_only_automatic_recorded_cidfile_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    cidfile = directory / "container.cid"
    identity = "a" * 64
    cidfile.write_text(identity + "\n", encoding="ascii")
    files = {
        password: helpers["file_identity"](password),
        cidfile: helpers["file_identity"](cidfile),
    }
    query_count = 0

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal query_count
        del check
        if arguments[:2] == ("container", "ls"):
            query_count += 1
            output = identity + "\n" if query_count == 1 else ""
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if arguments[:3] == ("container", "inspect", identity):
            payload = [{"Id": identity, "Config": {"Labels": {
                "aegis.verify.owner": "owner",
            }}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ("rm", "--force"):
            cidfile.unlink()
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    cleanup_globals = helpers["cleanup_verification_resources"].__globals__
    monkeypatch.setitem(cleanup_globals, "container", commands)
    monkeypatch.setitem(cleanup_globals, "selected_engine", lambda: "podman")

    assert helpers["cleanup_verification_resources"](
        "owner", identity, directory, helpers["file_identity"](directory), files,
    ) is True
    assert not directory.exists()


def test_verifier_podman_cidfile_transition_still_refuses_other_missing_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    cidfile = directory / "container.cid"
    identity = "b" * 64
    cidfile.write_text(identity + "\n", encoding="ascii")
    files = {
        password: helpers["file_identity"](password),
        cidfile: helpers["file_identity"](cidfile),
    }
    password.unlink()
    cidfile.unlink()
    cleanup_globals = helpers["cleanup_verification_resources"].__globals__
    monkeypatch.setitem(cleanup_globals, "selected_engine", lambda: "podman")

    with pytest.raises(RuntimeError, match="preserving evidence"):
        helpers["_accept_removed_podman_cidfile"](
            directory, helpers["file_identity"](directory), files,
        )


def test_verifier_podman_cleanup_accepts_recovered_container_without_cidfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "verification"
    directory.mkdir(mode=0o700)
    password = directory / "password"
    password.write_text("secret\n", encoding="ascii")
    identity = "c" * 64
    files = {password: helpers["file_identity"](password)}
    query_count = 0

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal query_count
        del check
        if arguments[:2] == ("container", "ls"):
            query_count += 1
            output = identity + "\n" if query_count == 1 else ""
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if arguments[:3] == ("container", "inspect", identity):
            payload = [{"Id": identity, "Config": {"Labels": {
                "aegis.verify.owner": "owner",
            }}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ("rm", "--force"):
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    cleanup_globals = helpers["cleanup_verification_resources"].__globals__
    monkeypatch.setitem(cleanup_globals, "container", commands)
    monkeypatch.setitem(cleanup_globals, "selected_engine", lambda: "podman")

    assert helpers["cleanup_verification_resources"](
        "owner", identity, directory, helpers["file_identity"](directory), files,
    ) is True
    assert not directory.exists()


def test_verifier_recovers_canonical_id_after_create_timeout_with_cidfile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    owner = "owned-verifier"
    identity = "a" * 64
    cidfile = tmp_path / "container.cid"
    files: dict[Path, tuple[int, ...]] = {}
    record = helpers["VerificationCreation"]()

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if arguments[0] == "create":
            cidfile.write_text(identity[:12] + "\n", encoding="ascii")
            raise subprocess.TimeoutExpired(arguments, 120)
        if arguments[:3] in (
            ("container", "inspect", identity[:12]),
            ("container", "inspect", identity),
        ):
            payload = [{"Id": identity, "Config": {"Labels": {"aegis.verify.owner": owner}}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, identity + "\n", "")
        raise AssertionError(arguments)

    monkeypatch.setitem(
        helpers["create_verification_container"].__globals__, "container", commands,
    )
    with pytest.raises(RuntimeError, match="creation failed"):
        helpers["create_verification_container"](owner, cidfile, files, record, "image")
    assert record.state_known is True
    assert record.container_id == identity
    assert cidfile in files


@pytest.mark.parametrize("interruption", (SystemExit(143), KeyboardInterrupt()))
def test_verifier_interrupted_create_recovers_id_for_outer_cleanup_then_propagates(
    monkeypatch: pytest.MonkeyPatch,
    interruption: BaseException,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    identity = "e" * 64
    removed = False

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        nonlocal removed
        del check
        if arguments[0] == "create":
            Path(arguments[arguments.index("--cidfile") + 1]).write_text(
                identity + "\n", encoding="ascii",
            )
            raise interruption
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(
                arguments, 0, "" if removed else identity + "\n", "",
            )
        if arguments[:3] == ("container", "inspect", identity):
            owner = next(
                value.removeprefix("aegis.verify.owner=")
                for value in arguments_seen[0]
                if value.startswith("aegis.verify.owner=")
            )
            payload = [{
                "Id": identity,
                "Config": {"Labels": {"aegis.verify.owner": owner}},
            }]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        if arguments[:2] == ("rm", "--force"):
            removed = True
            return subprocess.CompletedProcess(arguments, 0, "", "")
        raise AssertionError(arguments)

    arguments_seen: list[tuple[str, ...]] = []

    def recorded(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        arguments_seen.append(arguments)
        return commands(*arguments, check=check)

    database_globals = helpers["test_database"].__wrapped__.__globals__
    monkeypatch.setitem(database_globals, "container", recorded)
    monkeypatch.setitem(database_globals, "prepare_verification_bind", lambda *args: None)
    monkeypatch.setitem(database_globals, "test_environment", lambda: {})

    with pytest.raises(type(interruption)) as caught, helpers["test_database"]():
        pass
    if isinstance(caught.value, SystemExit):
        assert caught.value.code == 143
    assert removed is True


def test_verifier_uncertain_interrupted_create_preserves_evidence_without_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    identity = "f" * 64
    commands_seen: list[tuple[str, ...]] = []

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        commands_seen.append(arguments)
        if arguments[0] == "create":
            Path(arguments[arguments.index("--cidfile") + 1]).write_text(
                identity + "\n", encoding="ascii",
            )
            raise KeyboardInterrupt
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 125, "", "query failed")
        raise AssertionError(arguments)

    database_globals = helpers["test_database"].__wrapped__.__globals__
    monkeypatch.setitem(database_globals, "container", commands)
    monkeypatch.setitem(database_globals, "prepare_verification_bind", lambda *args: None)
    monkeypatch.setitem(database_globals, "test_environment", lambda: {})

    with pytest.raises(KeyboardInterrupt), helpers["test_database"]():
        pass
    assert not any(arguments[:2] == ("rm", "--force") for arguments in commands_seen)


@pytest.mark.parametrize("interruption", (SystemExit(143), KeyboardInterrupt()))
def test_verifier_transport_failure_during_interruption_recovery_propagates_interruption(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    interruption: BaseException,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    commands_seen: list[tuple[str, ...]] = []

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        commands_seen.append(arguments)
        if arguments[0] == "create":
            raise interruption
        if arguments[:2] == ("container", "ls"):
            raise OSError("transport failed")
        raise AssertionError(arguments)

    record = helpers["VerificationCreation"]()
    monkeypatch.setitem(
        helpers["create_verification_container"].__globals__, "container", commands,
    )
    with pytest.raises(type(interruption)) as caught:
        helpers["create_verification_container"](
            "owner", tmp_path / "missing.cid", {}, record, "image",
        )
    if isinstance(caught.value, SystemExit):
        assert caught.value.code == 143
    assert record.state_known is False
    assert record.container_id == ""
    assert not any(arguments[:2] == ("rm", "--force") for arguments in commands_seen)


def test_verifier_recovers_short_query_handle_as_canonical_id_after_nonzero_create(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    owner = "owned-verifier"
    identity = "b" * 64
    record = helpers["VerificationCreation"]()

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if arguments[0] == "create":
            return subprocess.CompletedProcess(arguments, 125, "", "create failed")
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, identity[:12] + "\n", "")
        if arguments[:3] == ("container", "inspect", identity[:12]):
            payload = [{"Id": identity, "Config": {"Labels": {"aegis.verify.owner": owner}}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    monkeypatch.setitem(
        helpers["create_verification_container"].__globals__, "container", commands,
    )
    with pytest.raises(RuntimeError, match="creation failed"):
        helpers["create_verification_container"](
            owner, tmp_path / "missing.cid", {}, record, "image",
        )
    assert record.state_known is True
    assert record.container_id == identity


def test_verify_compose_supplies_private_resource_token_to_config_and_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    directory = tmp_path / "aegis-phase1-e2e.unique-token"
    directory.mkdir()
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []
    support = {
        "checked_directory": lambda value: directory,
        "prepare": lambda value: None,
        "cleanup": lambda value: None,
    }

    def compose(*arguments: str, env: dict[str, str]) -> None:
        calls.append((arguments, dict(env)))

    function_globals = helpers["verify_compose"].__globals__
    monkeypatch.setitem(function_globals, "test_environment", lambda: {})
    monkeypatch.setitem(function_globals, "compose", compose)
    monkeypatch.setitem(
        function_globals["runpy"].__dict__, "run_path", lambda path: support,
    )
    monkeypatch.setitem(
        function_globals["tempfile"].__dict__, "mkdtemp", lambda **kwargs: str(directory),
    )

    helpers["verify_compose"]()

    assert calls[0][0][-2:] == ("config", "--quiet")
    assert calls[1][0][-4:] == ("build", "web", "gateway", "postgres")
    assert all(
        environment["AEGIS_TEST_RESOURCE_TOKEN"] == directory.name
        and environment["AEGIS_TEST_SECRET_DIR"] == str(directory / "secrets")
        for _arguments, environment in calls
    )


@pytest.mark.parametrize("failure", ("query", "ambiguous", "replacement"))
def test_verifier_partial_create_recovery_fails_closed_without_adopting_candidate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    owner = "owned-verifier"
    record = helpers["VerificationCreation"]()

    def commands(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        del check
        if arguments[0] == "create":
            return subprocess.CompletedProcess(arguments, 125, "", "create failed")
        if arguments[:2] == ("container", "ls"):
            if failure == "query":
                return subprocess.CompletedProcess(arguments, 125, "", "query failed")
            output = "first\nsecond\n" if failure == "ambiguous" else "candidate\n"
            return subprocess.CompletedProcess(arguments, 0, output, "")
        if arguments[:3] == ("container", "inspect", "candidate"):
            payload = [{
                "Id": "c" * 64,
                "Config": {"Labels": {"aegis.verify.owner": "replacement"}},
            }]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    monkeypatch.setitem(
        helpers["create_verification_container"].__globals__, "container", commands,
    )
    with pytest.raises(RuntimeError, match="recovery"):
        helpers["create_verification_container"](
            owner, tmp_path / "missing.cid", {}, record, "image",
        )
    assert record.state_known is False
    assert record.container_id == ""


def test_verifier_child_environment_rejects_operator_and_remote_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    provider, socket_path = configure_podman(monkeypatch, tmp_path)
    for key, value in {
        "AEGIS_DB_PASSWORD": "operator-secret",
        "PGPASSWORD": "operator-secret",
        "COMPOSE_FILE": "/operator/compose.yaml",
        "DOCKER_HOST": "tcp://remote.invalid:2375",
        "CONTAINER_HOST": "ssh://remote.invalid/run/podman.sock",
    }.items():
        monkeypatch.setenv(key, value)
    helpers = runpy.run_path(str(RUNNER))

    environment = helpers["test_environment"]()

    assert environment["AEGIS_CONTAINER_ENGINE"] == "podman"
    assert environment["PODMAN_COMPOSE_PROVIDER"] == str(provider)
    assert environment["AEGIS_PODMAN_SOCKET"] == str(socket_path)
    assert environment["AEGIS_ENV"] == "test"
    assert environment["AEGIS_DB_HOST"] == "127.0.0.1"
    for rejected in (
        "AEGIS_DB_PASSWORD", "PGPASSWORD", "COMPOSE_FILE", "DOCKER_HOST", "CONTAINER_HOST",
    ):
        assert rejected not in environment
