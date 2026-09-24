from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("target", ["test", "lint", "typecheck", "verify-compose"])
def test_local_make_commands_refuse_metadata_drift_without_mutating_lock(
    tmp_path: Path, target: str,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None
    metadata = tmp_path / "pyproject.toml"
    metadata.write_text(
        '[project]\nname="frozen-command-probe"\nversion="0.1.0"\n'
        'requires-python=">=3.13"\ndependencies=[]\n', encoding="utf-8",
    )
    environment = os.environ | {
        "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_PYTHON": sys.executable,
        "UV_OFFLINE": "1",
    }
    environment.pop("UV_LOCKED", None)
    subprocess.run([uv, "lock", "--offline"], cwd=tmp_path, env=environment,
                   check=True, capture_output=True)
    lock = tmp_path / "uv.lock"
    original = lock.read_bytes()
    metadata.write_text(metadata.read_text().replace('version="0.1.0"', 'version="0.2.0"'))
    result = subprocess.run(
        ["make", "--file", str(REPOSITORY / "Makefile"), target], cwd=tmp_path,
        env=environment, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode != 0
    assert lock.read_bytes() == original, "Local commands must never update a drifting lock"
    assert "lockfile" in result.stderr.lower() and "locked" in result.stderr.lower()


def test_e2e_nested_uv_commands_are_frozen_even_during_failure_cleanup(tmp_path: Path) -> None:
    # Abort at the first helper before any Compose build/start. The executable
    # doubles form the runner's CLI boundary and never contact a Docker daemon.
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "uv-invocations"
    engine_log = tmp_path / "engine-invocations"
    for name, source in {
        "docker": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ENGINE_PROBE_LOG"\nexit 0\n',
        "node": '#!/bin/sh\nprintf "24\\n"\n',
        "uv": '#!/bin/sh\nprintf "%s\\n" "${UV_LOCKED:-unset} $*" >> "$UV_PROBE_LOG"\n'
        'exit 77\n',
    }.items():
        executable = binary / name
        executable.write_text(source, encoding="utf-8")
        executable.chmod(0o700)
    environment = os.environ | {
        "PATH": str(binary) + os.pathsep + os.environ["PATH"], "UV_PROBE_LOG": str(log),
        "ENGINE_PROBE_LOG": str(engine_log), "AEGIS_CONTAINER_ENGINE": "docker",
    }
    environment.pop("UV_LOCKED", None)
    result = subprocess.run(
        ["bash", str(REPOSITORY / "scripts/test-e2e.sh")], env=environment,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 77
    invocations = log.read_text().splitlines()
    # The deliberately rejecting helper never prepares files; remove only its
    # known empty work directory, inferred from the cleanup command argument.
    directory = Path(invocations[-1].split()[-1])
    assert directory.name.startswith("aegis-phase1-e2e.")
    assert directory.parent in (Path("/tmp"), Path("/private/tmp"))
    directory.rmdir()
    assert len(invocations) == 2  # prepare rejected, then EXIT cleanup rejected.
    assert all(line.startswith("1 ") or "run --locked " in line for line in invocations)
    assert not engine_log.exists()


@pytest.mark.parametrize("invalid_engine", ("podman --remote", ""))
def test_e2e_rejects_invalid_engine_before_resource_checks_or_temp_creation(
    tmp_path: Path, invalid_engine: str,
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    engine_log = tmp_path / "engine-invocations"
    engine = binary / "podman --remote"
    engine.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ENGINE_PROBE_LOG"\n', encoding="ascii",
    )
    engine.chmod(0o700)
    before = {path.name for path in Path("/tmp").glob("aegis-phase1-e2e.*")}
    result = subprocess.run(
        ["bash", str(REPOSITORY / "scripts/test-e2e.sh")],
        env=os.environ | {
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "ENGINE_PROBE_LOG": str(engine_log),
            "AEGIS_CONTAINER_ENGINE": invalid_engine,
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 64
    assert not engine_log.exists()
    assert {path.name for path in Path("/tmp").glob("aegis-phase1-e2e.*")} == before


def test_e2e_rejects_podman_without_explicit_local_socket_before_engine_use(
    tmp_path: Path,
) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    engine_log = tmp_path / "engine-invocations"
    provider = tmp_path / "docker-compose"
    provider.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    provider.chmod(0o700)
    podman = binary / "podman"
    podman.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$*" >> "$ENGINE_PROBE_LOG"\n', encoding="ascii",
    )
    podman.chmod(0o700)

    result = subprocess.run(
        ["bash", str(REPOSITORY / "scripts/test-e2e.sh")],
        env=os.environ | {
            "PATH": str(binary) + os.pathsep + os.environ["PATH"],
            "ENGINE_PROBE_LOG": str(engine_log),
            "AEGIS_CONTAINER_ENGINE": "podman",
            "AEGIS_PODMAN_SOCKET": "",
            "PODMAN_COMPOSE_PROVIDER": str(provider),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 64
    assert "AEGIS_PODMAN_SOCKET" in result.stderr
    assert not engine_log.exists()


@pytest.mark.parametrize("stop_at", ("build", "launch"))
def test_e2e_forces_local_podman_for_compose_boundary(tmp_path: Path, stop_at: str) -> None:
    binary = tmp_path / "bin"
    binary.mkdir()
    engine_log = tmp_path / "engine-invocations"
    work_dir = tmp_path / "fake-e2e-work"
    launch_log = tmp_path / "controlled-launch"
    provider = tmp_path / "docker-compose"
    provider.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    provider.chmod(0o700)
    private = tmp_path / "podman-service"
    private.mkdir(mode=0o700)
    socket_path = private / "podman.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    for name, source in {
        "mktemp": '#!/bin/sh\nmkdir -m 700 "$FAKE_E2E_WORK"\nprintf "%s\\n" "$FAKE_E2E_WORK"\n',
        "node": "#!/bin/sh\nprintf '24\\n'\n",
        "podman": (
            "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$ENGINE_PROBE_LOG\"\n"
            'if [ "$FAKE_STOP_POINT" = build ]; then exit 77; fi\nexit 0\n'
        ),
        "uv": (
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'e2e_support.py controlled-compose '*)\n"
            "    printf '%s\\n' \"$*\" >> \"$CONTROLLED_LAUNCH_LOG\"; exit 79;;\n"
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
    try:
        result = subprocess.run(
            ["bash", str(REPOSITORY / "scripts/test-e2e.sh")],
            env=os.environ | {
                "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                "ENGINE_PROBE_LOG": str(engine_log),
                "FAKE_E2E_WORK": str(work_dir),
                "FAKE_STOP_POINT": stop_at,
                "CONTROLLED_LAUNCH_LOG": str(launch_log),
                "AEGIS_CONTAINER_ENGINE": "podman",
                "AEGIS_PODMAN_SOCKET": str(socket_path),
                "PODMAN_COMPOSE_PROVIDER": str(provider),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
    finally:
        listener.close()

    assert work_dir.parent == tmp_path
    assert {p.name for p in (work_dir / "secrets").iterdir()} == {
        "e2e-alice-password", "e2e-bob-password", "e2e-admin-password",
    }
    invocations = engine_log.read_text(encoding="ascii").splitlines()
    expected_build = (
        "--remote=false compose --env-file /dev/null --project-name "
        "aegis-phase1-e2e --project-directory " + str(REPOSITORY) +
        " -f compose.yaml -f compose.test.yaml -f " + str(REPOSITORY / "compose.podman.yaml") +
        " build"
    )
    assert invocations[0] == expected_build
    if stop_at == "build":
        assert result.returncode == 77 and invocations == [expected_build]
        assert not launch_log.exists()
    else:
        assert result.returncode == 79
        launches = launch_log.read_text().splitlines()
        assert len(launches) == 1
        assert launches[0].startswith("run python scripts/e2e_support.py controlled-compose ")
        assert launches[0].endswith("up --build --wait --wait-timeout 180")
        assert "-f " + str(REPOSITORY / "compose.podman.yaml") in launches[0]
        assert all(not ({"up", "run", "create"} & set(line.split())) for line in invocations)
