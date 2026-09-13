from __future__ import annotations

import os
import runpy
from pathlib import Path

import psycopg
import pytest

RUNNER = Path(__file__).resolve().parents[2] / "scripts/verify.py"


def test_verification_database_is_isolated_and_removed_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helpers = runpy.run_path(str(RUNNER))
    monkeypatch.setenv("AEGIS_DB_HOST", "do-not-contact.invalid")
    monkeypatch.setenv("AEGIS_DB_PASSWORD", "do-not-inherit")
    secret = None
    with pytest.raises(RuntimeError, match="child failed"), helpers["test_database"]() as env:
        assert env["AEGIS_DB_HOST"] == "127.0.0.1"
        assert "AEGIS_DB_PASSWORD" not in env
        secret = Path(env["AEGIS_DB_PASSWORD_FILE"])
        assert secret.stat().st_mode & 0o777 == 0o600
        with psycopg.connect(
            host=env["AEGIS_DB_HOST"], port=env["AEGIS_DB_PORT"],
            dbname=env["AEGIS_DB_NAME"], user=env["AEGIS_DB_USER"],
            password=secret.read_text().strip(), connect_timeout=3,
        ) as connection:
            assert connection.info.server_version // 10000 == 18
        raise RuntimeError("child failed")
    assert secret is not None and not secret.exists()
    assert not secret.parent.exists()
    assert os.environ["AEGIS_DB_HOST"] == "do-not-contact.invalid"
