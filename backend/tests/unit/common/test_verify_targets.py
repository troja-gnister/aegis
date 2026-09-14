from __future__ import annotations

import runpy
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[4]
RUNNER = REPOSITORY / "scripts/verify.py"


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
