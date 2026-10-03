"""The web server's bounded process count and its pooled-connection switch."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from aegis.config import (
    DEFAULT_WEB_WORKERS,
    MAX_WEB_WORKERS,
    WEB_DATABASE_POOL_ENV,
    WEB_DATABASE_POOL_MAX_LIFETIME_SECONDS,
    WEB_DATABASE_POOL_SIZE,
    WEB_DATABASE_POOL_TIMEOUT_SECONDS,
    ConfigurationError,
    web_database_pool_options,
    web_workers_from_environ,
)

BACKEND = Path(__file__).resolve().parents[2]


def test_default_worker_count_is_bounded_and_fits_the_web_memory_budget() -> None:
    assert web_workers_from_environ({}) == DEFAULT_WEB_WORKERS
    assert 1 <= DEFAULT_WEB_WORKERS <= MAX_WEB_WORKERS
    # Measured peak per web process is under 140 MiB; keep the maximum, plus the
    # supervisor, well inside the 1024 MiB web limit.
    assert MAX_WEB_WORKERS * 140 + 64 <= 0.8 * 1024


@pytest.mark.parametrize("value", ["1", "2", str(MAX_WEB_WORKERS)])
def test_explicit_worker_count_is_accepted(value: str) -> None:
    assert web_workers_from_environ({"AEGIS_WEB_WORKERS": value}) == int(value)


@pytest.mark.parametrize(
    "value",
    ["", " ", "0", "-1", str(MAX_WEB_WORKERS + 1), "100", "2.0", "two", "+2", "٣", "1e1",
     "99999999999999999999"],
)
def test_invalid_or_unbounded_worker_counts_are_refused(value: str) -> None:
    with pytest.raises(ConfigurationError, match="AEGIS_WEB_WORKERS"):
        web_workers_from_environ({"AEGIS_WEB_WORKERS": value})


def test_pool_is_off_unless_the_web_server_enables_it() -> None:
    assert web_database_pool_options({}) is None


def test_web_server_pool_is_fixed_size_with_bounded_lifetime() -> None:
    options = web_database_pool_options({WEB_DATABASE_POOL_ENV: "enabled"})

    assert options is not None
    assert options["min_size"] == options["max_size"] == WEB_DATABASE_POOL_SIZE
    assert options["max_lifetime"] == WEB_DATABASE_POOL_MAX_LIFETIME_SECONDS
    assert 0 < WEB_DATABASE_POOL_MAX_LIFETIME_SECONDS <= 300
    assert options["timeout"] == WEB_DATABASE_POOL_TIMEOUT_SECONDS
    assert 0 < WEB_DATABASE_POOL_TIMEOUT_SECONDS <= 30
    assert MAX_WEB_WORKERS * WEB_DATABASE_POOL_SIZE <= 32


@pytest.mark.parametrize("value", ["", "1", "true", "ENABLED", "disabled"])
def test_unknown_pool_switch_values_are_refused(value: str) -> None:
    with pytest.raises(ConfigurationError, match=WEB_DATABASE_POOL_ENV):
        web_database_pool_options({WEB_DATABASE_POOL_ENV: value})


PROBE = (
    "import django, json, sys; django.setup(); from django.conf import settings; "
    "database = settings.DATABASES['default']; "
    "sys.stdout.write(json.dumps({'pool': database.get('OPTIONS', {}).get('pool'), "
    "'maxAge': database.get('CONN_MAX_AGE', 0), "
    "'checks': database.get('CONN_HEALTH_CHECKS', False)}))"
)


def _database_settings(extra: dict[str, str]) -> dict[str, object]:
    import json

    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("AEGIS_", "DJANGO_"))}
    environment |= {"DJANGO_SETTINGS_MODULE": "aegis.settings.test", "AEGIS_ENV": "test",
                    "PYTHONPATH": str(BACKEND), **extra}
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=BACKEND, env=environment,
                            capture_output=True, text=True, timeout=60, check=True)
    return dict(json.loads(result.stdout))


def test_ordinary_processes_keep_one_connection_per_request() -> None:
    assert _database_settings({}) == {"pool": None, "maxAge": 0, "checks": False}


def test_web_server_processes_use_a_health_checked_pool_without_persistent_mode() -> None:
    database = _database_settings({WEB_DATABASE_POOL_ENV: "enabled"})

    assert database["maxAge"] == 0
    assert database["checks"] is True
    assert database["pool"] == web_database_pool_options({WEB_DATABASE_POOL_ENV: "enabled"})
