import os
import subprocess
import sys
from pathlib import Path

import pytest
from aegis.config import ConfigurationError, RuntimeConfig, read_secret


def _production_environ(secret: Path) -> dict[str, str]:
    return {
        "AEGIS_ENV": "production",
        "AEGIS_PUBLIC_URL": "https://files.example.test",
        "AEGIS_ALLOWED_HOSTS": "files.example.test",
        "AEGIS_DJANGO_SECRET_KEY_FILE": str(secret),
        "AEGIS_DB_PASSWORD_FILE": str(secret),
        "AEGIS_DB_NAME": "aegis",
        "AEGIS_DB_USER": "aegis_web",
        "AEGIS_DB_HOST": "postgres",
    }


def _write_secret(path: Path, value: str = "a" * 64) -> Path:
    path.write_text(value, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_production_reads_secrets_from_files(tmp_path: Path) -> None:
    secret = _write_secret(tmp_path / "django-secret")

    config = RuntimeConfig.from_environ(_production_environ(secret))

    assert config.secure_cookies is True
    assert config.allowed_hosts == ("files.example.test",)
    assert config.django_secret_key == "a" * 64


def test_production_rejects_inline_secret() -> None:
    with pytest.raises(ConfigurationError, match="file-backed"):
        RuntimeConfig.from_environ(
            {
                "AEGIS_ENV": "production",
                "AEGIS_DJANGO_SECRET_KEY": "not-allowed",
            }
        )


def test_secret_file_strips_one_trailing_newline(tmp_path: Path) -> None:
    secret = _write_secret(tmp_path / "secret", "value\n\n")

    assert read_secret(
        {"TOKEN_FILE": str(secret)}, "TOKEN", production=False
    ) == "value\n"


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
def test_secret_file_requires_mode_0600(tmp_path: Path, mode: int) -> None:
    secret = _write_secret(tmp_path / "secret")
    os.chmod(secret, mode)

    with pytest.raises(ConfigurationError, match="mode 0600"):
        read_secret({"TOKEN_FILE": str(secret)}, "TOKEN", production=False)


def test_secret_file_rejects_symlink(tmp_path: Path) -> None:
    target = _write_secret(tmp_path / "target")
    link = tmp_path / "secret"
    link.symlink_to(target)

    with pytest.raises(ConfigurationError, match="regular file"):
        read_secret({"TOKEN_FILE": str(link)}, "TOKEN", production=False)


def test_secret_file_rejects_non_regular_file(tmp_path: Path) -> None:
    directory = tmp_path / "secret"
    directory.mkdir(mode=0o700)

    with pytest.raises(ConfigurationError, match="regular file"):
        read_secret({"TOKEN_FILE": str(directory)}, "TOKEN", production=False)


def test_secret_file_rejects_more_than_4096_bytes(tmp_path: Path) -> None:
    secret = _write_secret(tmp_path / "secret", "a" * 4097)

    with pytest.raises(ConfigurationError, match="exceeds 4096 bytes"):
        read_secret({"TOKEN_FILE": str(secret)}, "TOKEN", production=False)


def test_secret_read_stays_bound_to_opened_file_during_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _write_secret(tmp_path / "secret", "trusted")
    original = tmp_path / "original"
    real_lstat = Path.lstat
    real_open = os.open
    replaced = False

    def replace_path() -> None:
        nonlocal replaced
        if replaced:
            return
        secret.replace(original)
        _write_secret(secret, "attacker")
        replaced = True

    def racing_lstat(path: Path) -> os.stat_result:
        info = real_lstat(path)
        if path == secret:
            replace_path()
        return info

    def racing_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if os.fsdecode(path) == str(secret):
            replace_path()
        return descriptor

    monkeypatch.setattr(Path, "lstat", racing_lstat)
    monkeypatch.setattr(os, "open", racing_open)

    assert read_secret({"TOKEN_FILE": str(secret)}, "TOKEN", production=False) == "trusted"


def test_secret_bounded_read_rejects_growth_after_fstat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = _write_secret(tmp_path / "secret", "trusted")
    real_fstat = os.fstat

    def grow_after_fstat(descriptor: int) -> os.stat_result:
        info = real_fstat(descriptor)
        with secret.open("ab") as stream:
            stream.write(b"a" * 4097)
        return info

    monkeypatch.setattr(os, "fstat", grow_after_fstat)

    with pytest.raises(ConfigurationError, match="exceeds 4096 bytes"):
        read_secret({"TOKEN_FILE": str(secret)}, "TOKEN", production=False)


@pytest.mark.parametrize("value", ["", "\n"])
def test_secret_file_rejects_empty_value(tmp_path: Path, value: str) -> None:
    secret = _write_secret(tmp_path / "secret", value)

    with pytest.raises(ConfigurationError, match="is empty"):
        read_secret({"TOKEN_FILE": str(secret)}, "TOKEN", production=False)


def test_secret_file_read_error_does_not_disclose_path(tmp_path: Path) -> None:
    missing = tmp_path / "sensitive-location" / "secret"

    with pytest.raises(ConfigurationError) as caught:
        read_secret({"TOKEN_FILE": str(missing)}, "TOKEN", production=False)

    assert str(missing) not in str(caught.value)


def test_optional_secret_returns_none_when_absent() -> None:
    assert read_secret({}, "TOKEN", production=False, required=False) is None


def test_production_requires_https(tmp_path: Path) -> None:
    secret = _write_secret(tmp_path / "secret")
    environ = _production_environ(secret)
    environ["AEGIS_PUBLIC_URL"] = "http://files.example.test"

    with pytest.raises(ConfigurationError, match="HTTPS"):
        RuntimeConfig.from_environ(environ)


def test_production_requires_allowed_hosts_to_match_public_hostname(
    tmp_path: Path,
) -> None:
    secret = _write_secret(tmp_path / "secret")
    environ = _production_environ(secret)
    environ["AEGIS_ALLOWED_HOSTS"] = "other.example.test"

    with pytest.raises(ConfigurationError, match="public URL hostname"):
        RuntimeConfig.from_environ(environ)


def test_production_allowed_host_leading_dot_matches_public_hostname(
    tmp_path: Path,
) -> None:
    secret = _write_secret(tmp_path / "secret")
    environ = _production_environ(secret)
    environ["AEGIS_ALLOWED_HOSTS"] = ".example.test"

    config = RuntimeConfig.from_environ(environ)

    assert config.allowed_hosts == (".example.test",)


@pytest.mark.parametrize(
    "public_url",
    [
        "https://files.example.test:not-a-port",
        "https://files.example.test:99999",
    ],
)
def test_public_url_rejects_invalid_port(public_url: str) -> None:
    with pytest.raises(ConfigurationError, match="AEGIS_PUBLIC_URL"):
        RuntimeConfig.from_environ(
            {
                "AEGIS_DJANGO_SECRET_KEY": "dev-secret",
                "AEGIS_DB_PASSWORD": "dev-password",
                "AEGIS_PUBLIC_URL": public_url,
            }
        )


def test_environment_is_normalized(tmp_path: Path) -> None:
    secret = _write_secret(tmp_path / "secret")
    environ = _production_environ(secret)
    environ["AEGIS_ENV"] = " Production "

    assert RuntimeConfig.from_environ(environ).environment == "production"


def test_invalid_database_port_is_bounded_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="AEGIS_DB_PORT"):
        RuntimeConfig.from_environ(
            {
                "AEGIS_DJANGO_SECRET_KEY": "dev-secret",
                "AEGIS_DB_PASSWORD": "dev-password",
                "AEGIS_DB_PORT": "not-a-port",
            }
        )


def test_production_settings_pass_cookie_host_csrf_and_https_checks(
    tmp_path: Path,
) -> None:
    secret = _write_secret(tmp_path / "secret")
    result = subprocess.run(
        [
            sys.executable,
            "backend/manage.py",
            "check",
            "--deploy",
            "--settings=aegis.settings.production",
        ],
        cwd=Path(__file__).parents[3],
        env=os.environ | _production_environ(secret),
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "security.W008" not in output
    assert "security.W012" not in output
    assert "security.W016" not in output
    assert "security.W018" not in output
    assert "security.W020" not in output
