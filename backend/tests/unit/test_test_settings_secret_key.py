"""Test settings sign with a deployment's protected key file only when one is provided."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROBE = (
    "import django; django.setup(); from django.conf import settings; "
    "import sys; sys.stdout.write(settings.SECRET_KEY)"
)


def _secret_key(extra: dict[str, str]) -> str:
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AEGIS_", "DJANGO_"))}
    environment |= {"DJANGO_SETTINGS_MODULE": "aegis.settings.test", "AEGIS_ENV": "test",
                    "PYTHONPATH": str(BACKEND), **extra}
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=BACKEND, env=environment,
                            capture_output=True, text=True, timeout=60, check=True)
    return result.stdout


def test_ordinary_test_runs_keep_the_fixed_key() -> None:
    assert _secret_key({}) == "test-only-secret-key"


def test_a_provided_protected_key_file_signs_the_disposable_stack(tmp_path: Path) -> None:
    key = tmp_path / "django-secret-key"
    descriptor = os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write("generated-benchmark-key-" + "b" * 40 + "\n")
    assert _secret_key({"AEGIS_DJANGO_SECRET_KEY_FILE": str(key)}) == (
        "generated-benchmark-key-" + "b" * 40)
