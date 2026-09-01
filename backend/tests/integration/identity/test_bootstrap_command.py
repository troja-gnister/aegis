import os
import re
import subprocess
import sys
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

pytestmark = pytest.mark.integration

VALID_PASSWORD = "Pine-River-Copper-Lantern-731!"
OUTPUT_PATTERN = re.compile(
    r"^user_id=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12} "
    r"created=(?:true|false)\n$"
)
BACKEND_DIR = Path(__file__).parents[3]


def _run_bootstrap_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "AEGIS_ENV": "test",
        "DJANGO_SETTINGS_MODULE": "aegis.settings.test",
    }
    return subprocess.run(
        [sys.executable, str(BACKEND_DIR / "manage.py"), "bootstrap_admin", *arguments],
        cwd=BACKEND_DIR.parent,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


def _write_secret(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.write_text(f"{value}\n")
    os.chmod(path, mode)


@pytest.mark.django_db(transaction=True)
def test_bootstrap_admin_command_is_idempotent_and_outputs_only_public_result(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "admin-password"
    _write_secret(secret, VALID_PASSWORD)
    first_stdout = StringIO()

    call_command(
        "bootstrap_admin",
        username="admin",
        email="admin@example.invalid",
        password_file=str(secret),
        stdout=first_stdout,
    )
    user = get_user_model().objects.get(username="admin")
    original_hash = user.password

    _write_secret(secret, "Quartz-Harbor-Orbit-984!")
    second_stdout = StringIO()
    call_command(
        "bootstrap_admin",
        username="admin",
        email="admin@example.invalid",
        password_file=str(secret),
        stdout=second_stdout,
    )
    user.refresh_from_db()

    assert OUTPUT_PATTERN.fullmatch(first_stdout.getvalue())
    assert first_stdout.getvalue().endswith(" created=true\n")
    assert OUTPUT_PATTERN.fullmatch(second_stdout.getvalue())
    assert second_stdout.getvalue().endswith(" created=false\n")
    assert first_stdout.getvalue().split()[0] == second_stdout.getvalue().split()[0]
    assert user.password == original_hash
    assert user.password.startswith("argon2$")
    combined_output = first_stdout.getvalue() + second_stdout.getvalue()
    assert VALID_PASSWORD not in combined_output
    assert str(secret) not in combined_output


@pytest.mark.django_db(transaction=True)
def test_bootstrap_admin_command_rejects_plaintext_password_option_without_leaking_it() -> None:
    credential = "Never-Accept-This-Plaintext-448!"

    with pytest.raises(TypeError) as caught:
        call_command(
            "bootstrap_admin",
            username="admin",
            email="admin@example.invalid",
            password_file="unused-private-file",
            password=credential,
        )

    assert "Unknown option" in str(caught.value)
    assert credential not in str(caught.value)


@pytest.mark.parametrize("option", ["--password", "--password-f"])
def test_bootstrap_admin_cli_rejects_password_file_abbreviations_without_echoing_value(
    option: str, tmp_path: Path
) -> None:
    sentinel = (
        "Never-Echo-This-Plaintext-Credential-552!"
        if option == "--password"
        else str(tmp_path / "must-not-read-private-file")
    )

    completed = _run_bootstrap_cli(
        "--username",
        "admin",
        "--email",
        "admin@example.invalid",
        option,
        sentinel,
    )

    assert completed.returncode != 0
    assert f"unrecognized argument: {option}" in completed.stderr
    assert sentinel not in completed.stdout
    assert sentinel not in completed.stderr
    assert "created=" not in completed.stdout


def test_bootstrap_admin_help_lists_only_exact_documented_options() -> None:
    completed = _run_bootstrap_cli("--help")

    assert completed.returncode == 0
    option_names = set(re.findall(r"--[a-z-]+", completed.stdout))
    documented = {"--username", "--email", "--password-file"}
    guarded_prefixes = {
        "--p",
        "--pa",
        "--pas",
        "--pass",
        "--passw",
        "--passwo",
        "--passwor",
        "--password",
        "--password-",
        "--password-f",
        "--password-fi",
        "--password-fil",
    }

    assert documented <= option_names
    assert guarded_prefixes.isdisjoint(option_names)


@pytest.mark.django_db(transaction=True)
def test_bootstrap_admin_command_translates_secret_file_errors_generically(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "sensitive-admin-password-path"
    _write_secret(secret, "Sensitive-Credential-Must-Not-Leak-193!", mode=0o640)
    stdout = StringIO()

    with pytest.raises(CommandError, match=r"^administrator bootstrap failed$") as caught:
        call_command(
            "bootstrap_admin",
            username="admin",
            email="admin@example.invalid",
            password_file=str(secret),
            stdout=stdout,
        )

    assert stdout.getvalue() == ""
    assert str(secret) not in str(caught.value)
    assert "Sensitive-Credential" not in str(caught.value)
