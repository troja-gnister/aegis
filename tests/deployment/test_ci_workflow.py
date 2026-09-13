from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


@pytest.fixture
def workflow() -> dict[str, Any]:
    if not WORKFLOW.is_file():
        pytest.skip("workflow presence has its own failing gate")
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


def test_phase1_has_a_ci_workflow() -> None:
    assert WORKFLOW.is_file(), "Phase 1 needs a four-job CI workflow"


def commands(job: dict[str, Any]) -> list[list[str]]:
    return [
        shlex.split(line)
        for step in job["steps"]
        for line in step.get("run", "").replace("\\\n", " ").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_ci_is_unprivileged_and_has_exactly_four_bounded_jobs(workflow: dict[str, Any]) -> None:
    assert set(workflow["on"]) == {"pull_request", "push"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert set(workflow["jobs"]) == {"backend", "frontend", "deployment", "e2e"}
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
        assert 0 < job["timeout-minutes"] <= 30
        assert "permissions" not in job
        assert "services" not in job  # scripts/verify.py owns a memory-only PostgreSQL 18.
        assert "container" not in job
    assert "secrets." not in str(workflow)


def test_every_action_is_immutable_and_the_first_check_enforces_pins(
    workflow: dict[str, Any],
) -> None:
    for job in workflow["jobs"].values():
        steps = job["steps"]
        assert steps[0]["uses"].startswith("actions/checkout@")
        assert steps[0]["with"]["persist-credentials"] is False
        assert steps[1]["uses"].startswith(
            "zgosalvez/github-actions-ensure-sha-pinned-actions@"
        )
        assert not steps[1].get("with", {}).get("dry_run", False)
        assert not steps[1].get("with", {}).get("allowlist")
        for step in steps:
            if "uses" in step:
                assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", step["uses"])


def test_python_installation_and_cache_are_lock_bound(workflow: dict[str, Any]) -> None:
    for name in ("backend", "deployment", "e2e"):
        job = workflow["jobs"][name]
        setup = next(step for step in job["steps"] if step.get("uses", "").startswith(
            "astral-sh/setup-uv@"
        ))
        assert setup["with"]["version"] == "0.12.8"
        assert setup["with"]["python-version"] == "3.13"
        assert setup["with"]["enable-cache"] is True
        assert setup["with"]["cache-dependency-glob"] == "uv.lock"
        runs = commands(job)
        assert ["uv", "lock", "--check"] in runs
        assert ["uv", "sync", "--locked", "--group", "dev"] in runs
    backend = commands(workflow["jobs"]["backend"])
    assert ["uv", "run", "--locked", "ruff", "check", "backend", "tests", "scripts"] in backend
    assert ["uv", "run", "--locked", "mypy", "backend"] in backend
    assert ["uv", "run", "--locked", "python", "scripts/verify.py", "backend"] in backend


def test_frontend_is_frozen_and_runs_all_build_gates(workflow: dict[str, Any]) -> None:
    for name in ("frontend", "e2e"):
        job = workflow["jobs"][name]
        setup = next(step for step in job["steps"] if step.get("uses", "").startswith(
            "actions/setup-node@"
        ))
        assert setup["with"]["node-version"] == "24.20.0"
        assert setup["with"]["cache"] == "npm"
        assert setup["with"]["cache-dependency-path"] == "frontend/package-lock.json"
        assert ["npm", "--prefix", "frontend", "ci"] in commands(job)
    frontend = commands(workflow["jobs"]["frontend"])
    for gate in ("lint", "typecheck", "test", "build"):
        assert ["npm", "--prefix", "frontend", "run", gate] in frontend


def test_deployment_builds_before_real_role_tests_and_e2e_installs_both_browsers(
    workflow: dict[str, Any],
) -> None:
    deployment = commands(workflow["jobs"]["deployment"])
    build = ["uv", "run", "--locked", "python", "scripts/verify.py", "compose"]
    tests = ["uv", "run", "--locked", "python", "scripts/verify.py", "deployment"]
    assert deployment.index(build) < deployment.index(tests)
    e2e_job = workflow["jobs"]["e2e"]
    e2e = commands(e2e_job)
    browsers = ["npm", "exec", "--", "playwright", "install",
                "--with-deps", "chromium", "webkit"]
    browser_step = next(step for step in e2e_job["steps"] if step.get("run") and
                        shlex.split(step["run"]) == browsers)
    assert browser_step["working-directory"] == "frontend"
    assert e2e.index(["npm", "--prefix", "frontend", "ci"]) < e2e.index(browsers)
    assert e2e.index(browsers) < e2e.index(["bash", "scripts/test-e2e.sh"])


def test_failure_artifacts_are_only_masked_synthetic_screenshots(
    workflow: dict[str, Any],
) -> None:
    uploads = [
        (name, step)
        for name, job in workflow["jobs"].items()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    name, step = uploads[0]
    assert name == "e2e"
    assert step["if"] == "failure()"
    assert step["with"]["path"] == "frontend/test-results/**/sanitized.png"
    assert step["with"]["retention-days"] == 3
    assert step["with"]["include-hidden-files"] is False
    assert step["with"]["if-no-files-found"] == "ignore"


@pytest.mark.parametrize(("dirty_path", "staged"), [
    (None, False),
    ("uv.lock", False),
    ("uv.lock", True),
    ("frontend/package-lock.json", False),
    ("frontend/package-lock.json", True),
    ("backend/aegis_apps/roots/migrations/__init__.py", False),
    ("backend/aegis_apps/roots/migrations/0002_unexpected.py", False),
    ("deploy/mounts.manifest.json", False),
    ("deploy/mounts.gateway.attestation", False),
    ("compose.mounts.generated.yaml", False),
    ("tracked.txt", False),
])
def test_hygiene_gate_rejects_locks_migrations_ignored_outputs_and_whitespace(
    workflow: dict[str, Any], tmp_path: Path, dirty_path: str | None, staged: bool,
) -> None:
    gates = [next(step for step in job["steps"] if step.get("id") == "hygiene")
             for job in workflow["jobs"].values()]
    assert all(step.get("if") == "always()" for step in gates)
    assert len({step["run"] for step in gates}) == 1
    for path, value in {
        "uv.lock": "locked\n", "frontend/package-lock.json": "{}\n",
        "backend/aegis_apps/roots/migrations/__init__.py": "",
        "tracked.txt": "clean\n", ".gitignore":
        "/deploy/mounts.manifest.json\n/deploy/mounts.gateway.attestation\n"
        "/compose.mounts.generated.yaml\n",
    }.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    for arguments in (["init", "--quiet"], ["add", "."],
                      ["-c", "user.name=CI test", "-c", "user.email=ci@e2e.invalid",
                       "commit", "--quiet", "-m", "fixture"]):
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)
    if dirty_path is not None:
        target = tmp_path / dirty_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "unexpected  \n" if dirty_path == "tracked.txt" else "unexpected\n",
            encoding="utf-8",
        )
        if staged:
            subprocess.run(
                ["git", "add", "--", dirty_path], cwd=tmp_path, check=True, capture_output=True,
            )
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", gates[0]["run"]],
        cwd=tmp_path, capture_output=True, text=True, timeout=10,
    )
    assert (result.returncode == 0) is (dirty_path is None), result.stderr
