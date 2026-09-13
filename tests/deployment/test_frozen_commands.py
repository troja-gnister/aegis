from __future__ import annotations

import os
import shutil
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
    for name, source in {
        "docker": '#!/bin/sh\nexit 0\n',
        "node": '#!/bin/sh\nprintf "24\\n"\n',
        "uv": '#!/bin/sh\nprintf "%s\\n" "${UV_LOCKED:-unset} $*" >> "$UV_PROBE_LOG"\n'
        'exit 77\n',
    }.items():
        executable = binary / name
        executable.write_text(source, encoding="utf-8")
        executable.chmod(0o700)
    environment = os.environ | {
        "PATH": str(binary) + os.pathsep + os.environ["PATH"], "UV_PROBE_LOG": str(log),
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
