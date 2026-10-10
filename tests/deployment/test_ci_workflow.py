from __future__ import annotations

import os
import py_compile
import re
import runpy
import shlex
import subprocess
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"
GATES = ("backend", "frontend", "deployment", "e2e")
ROLES = ("backend", "gateway", "postgres", "caddy")
PUBLISH_IF = "github.event_name == 'push' && github.ref == 'refs/heads/main'"
PUBLISH_PERMISSIONS = {"contents": "read", "packages": "write"}
LOGIN = """printf '%s' "$GITHUB_TOKEN" | docker login ghcr.io -u "$GITHUB_ACTOR" --password-stdin"""
FIXTURE_SHA = "0123456789abcdef0123456789abcdef01234567"
FIXTURE_CREATED = "2026-10-10T08:09:10+02:00"
FIXTURE_TOKEN = "ghs_" + "Tk9zYw" * 6
CONTEXT = {
    "github.repository": "Example-Owner/aegis-fixture",
    "github.repository_owner": "Example-Owner",
    "github.sha": FIXTURE_SHA,
    "secrets.GITHUB_TOKEN": FIXTURE_TOKEN,
}
# Record every docker argv (unit-separated) and its stdin for login; fail a build on demand.
FAKE_DOCKER = r"""#!/usr/bin/env bash
set -euo pipefail
(IFS=$'\x1f'; printf '%s\n' "$*") >> "$FAKE_LOG"
case "${1-}" in
  login) cat > "$FAKE_STDIN" ;;
  build) for argument in "$@"; do
           [[ "$argument" != "docker/${FAKE_FAIL_ROLE:-none}.Dockerfile" ]] || exit 1
         done ;;
  image) image="${!#}"; printf '%s@sha256:%064d\n' "${image%:*}" 7 ;;
esac
"""
FAKE_GIT = r"""#!/usr/bin/env bash
case "$*" in
  "log -1 --format=%cI") printf '%s\n' "$FAKE_CREATED" ;;
  "rev-parse HEAD") printf '%s\n' "$FAKE_HEAD" ;;
  *) exit 97 ;;
esac
"""


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


def test_ci_is_unprivileged_and_has_four_bounded_gates_and_one_publisher(
    workflow: dict[str, Any],
) -> None:
    assert set(workflow["on"]) == {"pull_request", "push"}
    assert workflow["on"]["push"]["branches"] == ["main"]
    assert workflow["permissions"] == {"contents": "read"}
    # Superseded pull request runs are cancelled; a main run never is, so a publish cannot
    # stop halfway through pushing a release.
    assert workflow["concurrency"]["cancel-in-progress"] == "${{ github.ref != 'refs/heads/main' }}"
    assert set(workflow["jobs"]) == {*GATES, "publish"}
    for name, job in workflow["jobs"].items():
        assert job["runs-on"] == "ubuntu-24.04"
        assert 0 < job["timeout-minutes"] <= 30
        assert "services" not in job  # scripts/verify.py owns a memory-only PostgreSQL 18.
        assert "container" not in job
        if name in GATES:
            assert "permissions" not in job
            assert "secrets." not in str(job)


def test_publish_runs_only_for_main_pushes_after_every_gate(workflow: dict[str, Any]) -> None:
    publish = workflow["jobs"]["publish"]
    assert publish["needs"] == list(GATES)
    assert publish["if"] == PUBLISH_IF
    assert publish["permissions"] == PUBLISH_PERMISSIONS
    assert publish["runs-on"] == "ubuntu-24.04"
    assert 0 < publish["timeout-minutes"] <= 30
    assert not {"strategy", "environment", "continue-on-error"} & set(publish)


def test_only_the_publisher_may_write_packages(workflow: dict[str, Any]) -> None:
    assert workflow["permissions"] == {"contents": "read"}
    grants = {name: job.get("permissions") for name, job in workflow["jobs"].items()}
    assert grants == {**dict.fromkeys(GATES), "publish": PUBLISH_PERMISSIONS}
    assert WORKFLOW.read_text(encoding="utf-8").count("packages: write") == 1


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


def _publish_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = workflow["jobs"]["publish"]["steps"]
    assert len(steps) == 5, "checkout, pin enforcement, login, build and push, logout"
    return steps


def _render(value: str) -> str:
    rendered = re.sub(r"\$\{\{\s*([\w.]+)\s*\}\}", lambda match: CONTEXT[match.group(1)], value)
    assert "${{" not in rendered
    return rendered


def _run_publish_script(
    step: dict[str, Any], tmp_path: Path, **fake: str,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    assert "${{" not in step["run"], "expressions reach the script only through env"
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    for name, body in {"docker": FAKE_DOCKER, "git": FAKE_GIT}.items():
        (tools / name).write_text(body, encoding="utf-8")
        (tools / name).chmod(0o755)
    log = tmp_path / "docker.log"
    log.unlink(missing_ok=True)
    env = {
        "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
        "FAKE_LOG": str(log), "FAKE_STDIN": str(tmp_path / "docker.stdin"),
        "FAKE_CREATED": FIXTURE_CREATED, "FAKE_HEAD": FIXTURE_SHA,
        **fake,
        **{key: _render(value) for key, value in step.get("env", {}).items()},
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10,
    )
    calls = [line.split("\x1f") for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def test_publisher_adds_no_action_and_always_logs_out(workflow: dict[str, Any]) -> None:
    checkout, pins, _login, _build, logout = _publish_steps(workflow)
    gate = workflow["jobs"]["backend"]["steps"]
    assert checkout == gate[0]  # Same pinned SHA, no credentials; depth 1 holds HEAD.
    assert pins == gate[1]
    assert [step["uses"] for step in _publish_steps(workflow) if "uses" in step] == [
        checkout["uses"], pins["uses"],
    ]
    assert logout["if"] == "always()"
    assert shlex.split(logout["run"]) == ["docker", "logout", "ghcr.io"]
    assert set(logout) == {"name", "if", "run"}


def test_registry_token_reaches_docker_only_on_stdin_from_env(
    workflow: dict[str, Any], tmp_path: Path,
) -> None:
    login = _publish_steps(workflow)[2]
    assert login["env"] == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    assert [line.strip() for line in login["run"].splitlines() if line.strip()] == [
        "set -euo pipefail", LOGIN,
    ]
    assert str(workflow).count("secrets.") == 1
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if step is not login:
                assert "GITHUB_TOKEN" not in str(step)
                assert "github.token" not in str(step)
    result, calls = _run_publish_script(login, tmp_path, GITHUB_ACTOR="example-actor")
    assert result.returncode == 0, result.stderr
    assert calls == [["login", "ghcr.io", "-u", "example-actor", "--password-stdin"]]
    assert (tmp_path / "docker.stdin").read_text(encoding="utf-8") == FIXTURE_TOKEN
    assert FIXTURE_TOKEN not in result.stdout + result.stderr


def test_publisher_builds_labels_and_pushes_each_role_by_commit_only(
    workflow: dict[str, Any], tmp_path: Path,
) -> None:
    build = _publish_steps(workflow)[3]
    assert build["run"].lstrip().startswith("set -euo pipefail\n")
    assert "GITHUB_TOKEN" not in str(build)
    assert "latest" not in WORKFLOW.read_text(encoding="utf-8").lower()
    result, calls = _run_publish_script(build, tmp_path)
    assert result.returncode == 0, result.stderr
    images = [f"ghcr.io/example-owner/aegis-{role}:sha-{FIXTURE_SHA}" for role in ROLES]
    # Default build arguments and the repository-root context, as the deployment gate builds.
    assert calls == [
        *(
            ["build", "--file", f"docker/{role}.Dockerfile",
             "--label", "org.opencontainers.image.source="
             "https://github.com/Example-Owner/aegis-fixture",
             "--label", f"org.opencontainers.image.revision={FIXTURE_SHA}",
             "--label", f"org.opencontainers.image.created={FIXTURE_CREATED}",
             "--tag", image, "."]
            for role, image in zip(ROLES, images, strict=True)
        ),
        *(
            call
            for image in images
            for call in (["push", image],
                         ["image", "inspect", "--format", "{{index .RepoDigests 0}}", image])
        ),
    ]
    for role in ROLES:
        digest = f"ghcr.io/example-owner/aegis-{role}@sha256:{7:064d}"
        assert digest in result.stdout.splitlines()


@pytest.mark.parametrize(("failure", "fake"), [
    ("first build", {"FAKE_FAIL_ROLE": "backend"}),
    ("last build", {"FAKE_FAIL_ROLE": "caddy"}),
    ("checkout is not the labelled commit", {"FAKE_HEAD": "f" * 40}),
])
def test_publisher_pushes_nothing_unless_every_image_builds_from_the_commit(
    workflow: dict[str, Any], tmp_path: Path, failure: str, fake: dict[str, str],
) -> None:
    result, calls = _run_publish_script(_publish_steps(workflow)[3], tmp_path, **fake)
    assert result.returncode != 0, failure
    assert not [call for call in calls if call[0] != "build"], failure


@pytest.mark.parametrize(("dirty_path", "staged"), [
    (None, False),
    ("uv.lock", False),
    ("uv.lock", True),
    ("frontend/package-lock.json", False),
    ("frontend/package-lock.json", True),
    ("backend/aegis_apps/roots/migrations/__init__.py", False),
    ("backend/aegis_apps/roots/migrations/__init__.py", True),
    ("backend/aegis_apps/roots/migrations/0002_unexpected.py", False),
    ("backend/aegis_apps/roots/migrations/0002_unexpected.py", True),
    ("backend/aegis_apps/roots/migrations/0003_ignored.py", False),
    ("deploy/mounts.manifest.json", False),
    ("deploy/mounts.gateway.attestation", False),
    ("compose.mounts.generated.yaml", False),
    ("tracked.txt", False),
])
def test_hygiene_gate_rejects_locks_migrations_ignored_outputs_and_whitespace(
    workflow: dict[str, Any], tmp_path: Path, dirty_path: str | None, staged: bool,
) -> None:
    # The publisher only builds and pushes; its tree is the tree the gates checked.
    gates = [next(step for step in workflow["jobs"][name]["steps"] if step.get("id") == "hygiene")
             for name in GATES]
    assert all(step.get("if") == "always()" for step in gates)
    assert len({step["run"] for step in gates}) == 1
    for path, value in {
        "uv.lock": "locked\n", "frontend/package-lock.json": "{}\n",
        "backend/aegis_apps/roots/migrations/__init__.py": "",
        "tracked.txt": "clean\n", ".gitignore":
        "/deploy/mounts.manifest.json\n/deploy/mounts.gateway.attestation\n"
        "/compose.mounts.generated.yaml\n__pycache__/\n*.py[cod]\n"
        "/backend/aegis_apps/roots/migrations/0003_ignored.py\n",
    }.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(value, encoding="utf-8")
    for arguments in (["init", "--quiet"], ["add", "."],
                      ["-c", "user.name=CI test", "-c", "user.email=ci@e2e.invalid",
                       "commit", "--quiet", "-m", "fixture"]):
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)
    # Django imports migrations during checks/tests; these legitimate caches must
    # not become generated-source drift. Compile a real tracked module, not a mock.
    py_compile.compile(
        str(tmp_path / "backend/aegis_apps/roots/migrations/__init__.py"), doraise=True,
    )
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


def _deployment_annotation_steps(workflow: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    steps = workflow["jobs"]["deployment"]["steps"]
    tests = next(i for i, step in enumerate(steps) if shlex.split(step.get("run", "")) == [
        "uv", "run", "--locked", "python", "scripts/verify.py", "deployment",
    ])
    return steps[tests], steps[tests + 1]


def _annotate(workflow: dict[str, Any], runner_temp: Path) -> list[str]:
    _tests, annotate = _deployment_annotation_steps(workflow)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", annotate["run"]],
        env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(runner_temp),
             "JUNIT_ANNOTATIONS": annotate["env"]["JUNIT_ANNOTATIONS"]},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout.splitlines()


def test_deployment_failures_publish_bounded_junit_annotations(
    workflow: dict[str, Any],
) -> None:
    tests, annotate = _deployment_annotation_steps(workflow)
    assert tests["env"] == {
        "PYTEST_ADDOPTS": "--junitxml=${{ runner.temp }}/deployment-junit.xml -rfE",
    }
    assert annotate["if"] == "failure()"
    assert "uses" not in annotate
    assert shlex.split(annotate["run"]) == [
        "python3", "-c", "$JUNIT_ANNOTATIONS", "$RUNNER_TEMP/deployment-junit.xml",
    ]
    assert set(annotate["env"]) == {"JUNIT_ANNOTATIONS"}
    imports = re.findall(r"^(?:import|from) .*$", annotate["env"]["JUNIT_ANNOTATIONS"], re.M)
    assert imports == ["import re, sys", "import xml.etree.ElementTree as ElementTree"]


def _write_junit(runner_temp: Path, cases: list[str]) -> None:
    (runner_temp / "deployment-junit.xml").write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(cases) + "</testsuite></testsuites>",
        encoding="utf-8",
    )


def test_junit_annotations_publish_only_test_id_and_exception_type_capped(
    workflow: dict[str, Any], tmp_path: Path,
) -> None:
    secret = "0123456789abcdef" * 4
    cases = [
        '<testcase classname="tests.deployment.test_a" name="test_escape[x,y:z \'q\']">'
        '<failure message="AssertionError: 100% of /srv/aegis/roots/private-path&#10;'
        f'second-line-sentinel">trace {secret}</failure>'
        '<system-out>stdout-sentinel</system-out><system-err>stderr-sentinel</system-err>'
        '</testcase>',
        '<testcase classname="tests.deployment.test_b" name="test_env">'
        f'<error message="failed on setup with &quot;RuntimeError: token {secret}&quot;"/>'
        '</testcase>',
        '<testcase classname="tests.deployment.test_c" name="test_passes"/>',
        '<testcase classname="tests.deployment.test_d" name="test_body">'
        f'<failure>aegisctl.container_resources.ProjectResourceError: {secret}</failure>'
        '</testcase>',
        *(
            f'<testcase classname="tests.deployment.test_many" name="test_{index}">'
            '<failure message="assert False"/></testcase>'
            for index in range(9)
        ),
    ]
    _write_junit(tmp_path, cases)
    lines = _annotate(workflow, tmp_path)
    assert lines == [
        "::error title=tests.deployment.test_a%3A%3Atest_escape[x_y%3Az__q_]::AssertionError",
        "::error title=tests.deployment.test_b%3A%3Atest_env::error",
        "::error title=tests.deployment.test_d%3A%3Atest_body::"
        "aegisctl.container_resources.ProjectResourceError",
        *(f"::error title=tests.deployment.test_many%3A%3Atest_{index}::failure"
          for index in range(6)),
        "::error title=deployment-junit::3 more failures omitted",
    ]
    output = "\n".join(lines)
    for leaked in ("sentinel", "private-path", secret, "test_passes", "100"):
        assert leaked not in output

    _write_junit(tmp_path, ['<testcase classname="a" name="b"/>'])
    assert _annotate(workflow, tmp_path) == [
        "::error title=deployment-junit::No failing test case; failure was outside pytest",
    ]
    (tmp_path / "deployment-junit.xml").unlink()
    assert _annotate(workflow, tmp_path) == [
        "::error title=deployment-junit::No JUnit report; failure preceded pytest",
    ]


HEX_SECRET = "60bf5362c6655b0bef7b" + "a1" * 22
# Independent review leak cases: each message would have been published (in part)
# by a denylist scrubber. Only the exception type may ever reach an annotation.
REVIEW_LEAK_CASES = (
    ("AssertionError: assert 'R8FLuKPRUbj8KIFCBw62Hw' == 'x'", ("R8FLuKPRUbj8KIFCBw62Hw",)),
    ("AssertionError: password='hunter2'", ("hunter2",)),
    ("AssertionError: password=Zq9xYt2k and Password=Wk3mNp8q", ("Zq9xYt2k", "Wk3mNp8q")),
    ("RuntimeError: db_password: s3cr3tpass", ("s3cr3tpass",)),
    ("RuntimeError: Authorization: Bearer abcDEFghiJKLmnoP", ("abcDEFghiJKLmnoP",)),
    ("AssertionError: {'AEGIS_DB_PASSWORD': 'kRtUvWxYzAbCdEfG'}", ("kRtUvWxYzAbCdEfG",)),
    ("AssertionError: assert 'qwertyuiopasdfghjklzxcvbnmQWERTYUIOP' == ''",
     ("qwertyuiopasdfghjklzxcvbnmQWERTYUIOP", "qwertyuiop")),
    ("AssertionError: " + "x" * 165 + " " + HEX_SECRET, (HEX_SECRET[:20], HEX_SECRET[:8])),
    ("AssertionError: assert \"super-'$`; Yh7uKp0LmQ\" != ''", ("super-", "Yh7uKp0LmQ")),
    ("RuntimeError: cannot read tmp/aegis-verify-abc/password", ("aegis-verify-abc", "tmp/")),
    ("AssertionError: 100%0A::warning::injected", ("warning", "injected")),
)


@pytest.mark.parametrize(
    ("message", "secrets"), REVIEW_LEAK_CASES,
    ids=[f"leak{index}" for index in range(len(REVIEW_LEAK_CASES))],
)
def test_junit_annotations_never_publish_message_text(
    workflow: dict[str, Any], tmp_path: Path, message: str, secrets: tuple[str, ...],
) -> None:
    kind = message.split(":", 1)[0]
    _write_junit(tmp_path, [
        '<testcase classname="tests.deployment.test_leak" name="test_case">'
        f"<failure message={quoteattr(message)}>{escape(message)}</failure>"
        f"<system-out>{escape(message)}</system-out></testcase>",
        '<testcase classname="tests.deployment.test_leak" name="test_body">'
        f"<error>{escape(message)}</error></testcase>",
    ])
    lines = _annotate(workflow, tmp_path)
    output = "\n".join(lines)
    for secret in secrets:
        assert secret not in output, f"annotation leaked {secret!r}"
    assert lines == [
        f"::error title=tests.deployment.test_leak%3A%3Atest_case::{kind}",
        f"::error title=tests.deployment.test_leak%3A%3Atest_body::{kind}",
    ]


def test_verification_runner_passes_pytest_addopts_to_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = runpy.run_path(str(WORKFLOW.parents[2] / "scripts/verify.py"))
    monkeypatch.setenv("AEGIS_CONTAINER_ENGINE", "docker")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--junitxml=/tmp/report.xml -rfE")
    assert helpers["test_environment"]()["PYTEST_ADDOPTS"] == "--junitxml=/tmp/report.xml -rfE"
