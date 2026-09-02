import shutil
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "init-dev-secrets.sh"
SECRET_NAMES = (
    "postgres-superuser-password",
    "db-migrator-password",
    "db-web-password",
    "db-operations-password",
    "db-indexer-password",
    "db-media-password",
    "django-secret-key",
    "auth-throttle-hmac-key",
)


def install_script(tmp_path: Path) -> Path:
    assert SCRIPT.is_file(), "development secret initializer is missing"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / SCRIPT.name
    shutil.copy2(SCRIPT, target)
    return target


def test_initializer_creates_mode_0600_secrets_without_printing_values(tmp_path: Path) -> None:
    script = install_script(tmp_path)

    result = subprocess.run(
        ["bash", str(script)], cwd=tmp_path, check=True, capture_output=True, text=True
    )

    for name in SECRET_NAMES:
        secret = tmp_path / "deploy" / "secrets" / "dev" / name
        value = secret.read_text()
        assert value
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600
        assert name in result.stdout
        assert value not in result.stdout


def test_initializer_preserves_existing_nonempty_secrets(tmp_path: Path) -> None:
    script = install_script(tmp_path)
    subprocess.run(["bash", str(script)], cwd=tmp_path, check=True)
    secret = tmp_path / "deploy" / "secrets" / "dev" / "db-web-password"
    original = secret.read_text()

    subprocess.run(["bash", str(script)], cwd=tmp_path, check=True)

    assert secret.read_text() == original
