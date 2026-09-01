import os
import re
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
