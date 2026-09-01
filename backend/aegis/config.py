from __future__ import annotations

import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload
from urllib.parse import urlparse


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration cannot be loaded safely."""


@overload
def read_secret(
    environ: Mapping[str, str],
    name: str,
    *,
    production: bool,
    required: Literal[True] = True,
) -> str: ...


@overload
def read_secret(
    environ: Mapping[str, str],
    name: str,
    *,
    production: bool,
    required: Literal[False],
) -> str | None: ...


def read_secret(
    environ: Mapping[str, str],
    name: str,
    *,
    production: bool,
    required: bool = True,
) -> str | None:
    file_name = f"{name}_FILE"
    if production and name in environ:
        raise ConfigurationError(f"{name} must be file-backed in production")
    if file_name in environ:
        path = Path(environ[file_name])
        try:
            info = path.lstat()
        except OSError:
            raise ConfigurationError(f"{file_name} could not be read") from None
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            raise ConfigurationError(f"{file_name} must name a regular file")
        if stat.S_IMODE(info.st_mode) != 0o600:
            raise ConfigurationError(f"{file_name} must have mode 0600")
        if info.st_size > 4096:
            raise ConfigurationError(f"{file_name} exceeds 4096 bytes")
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise ConfigurationError(f"{file_name} could not be read") from None
        if value.endswith("\n"):
            value = value[:-1]
        if not value:
            raise ConfigurationError(f"{file_name} is empty")
        return value
    value = environ.get(name, "")
    if not value and required:
        raise ConfigurationError(f"{file_name} is required")
    return value or None


@dataclass(frozen=True)
class RuntimeConfig:
    environment: str
    public_url: str
    allowed_hosts: tuple[str, ...]
    django_secret_key: str
    db_name: str
    db_user: str
    db_password: str
    db_host: str
    db_port: int
    secure_cookies: bool
    trust_proxy_headers: bool

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> RuntimeConfig:
        environment = environ.get("AEGIS_ENV", "development").strip().lower()
        if environment not in {"development", "test", "production"}:
            raise ConfigurationError("AEGIS_ENV is invalid")
        production = environment == "production"
        django_secret_key = read_secret(
            environ, "AEGIS_DJANGO_SECRET_KEY", production=production
        )
        db_password = read_secret(environ, "AEGIS_DB_PASSWORD", production=production)

        public_url = environ.get("AEGIS_PUBLIC_URL", "http://localhost:8080").strip()
        parsed = urlparse(public_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ConfigurationError("AEGIS_PUBLIC_URL must be an absolute HTTP(S) URL")
        if production and parsed.scheme != "https":
            raise ConfigurationError("AEGIS_PUBLIC_URL must use HTTPS in production")

        hosts = tuple(
            host.strip()
            for host in environ.get("AEGIS_ALLOWED_HOSTS", parsed.hostname).split(",")
            if host.strip()
        )
        if production and (not hosts or "*" in hosts):
            raise ConfigurationError("AEGIS_ALLOWED_HOSTS must list explicit production hosts")

        try:
            db_port = int(environ.get("AEGIS_DB_PORT", "5432"))
        except ValueError:
            raise ConfigurationError("AEGIS_DB_PORT must be an integer") from None
        if not 1 <= db_port <= 65535:
            raise ConfigurationError("AEGIS_DB_PORT must be between 1 and 65535")

        trust_proxy_value = environ.get("AEGIS_TRUST_PROXY_HEADERS", "false").strip().lower()
        if trust_proxy_value not in {"true", "false"}:
            raise ConfigurationError("AEGIS_TRUST_PROXY_HEADERS must be true or false")

        return cls(
            environment=environment,
            public_url=public_url,
            allowed_hosts=hosts,
            django_secret_key=django_secret_key,
            db_name=environ.get("AEGIS_DB_NAME", "aegis"),
            db_user=environ.get("AEGIS_DB_USER", "aegis_web"),
            db_password=db_password,
            db_host=environ.get("AEGIS_DB_HOST", "127.0.0.1"),
            db_port=db_port,
            secure_cookies=parsed.scheme == "https",
            trust_proxy_headers=trust_proxy_value == "true",
        )
