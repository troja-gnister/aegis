"""The web server's pooled database logins: reuse, isolation, revocation and recovery.

Every test routes Django's default connection through the production pool
configuration with an actual ``aegis_web`` login and observes the server from a
separate owner session.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from aegis.config import WEB_DATABASE_POOL_ENV, web_database_pool_options
from aegis_apps.catalog.api import _database_is_unavailable
from aegis_apps.catalog.authorization import CatalogNotFound
from aegis_apps.common import database_pool
from aegis_apps.common.database_pool import PoolLifetimeEnforcer, enforce_pool_lifetime
from django.core.signals import request_started
from django.db import OperationalError, connection, transaction
from django.db.backends.signals import connection_created
from django.test import Client
from psycopg import sql

from tests.support.database_roles import RoleDatabase

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _pool(**overrides: object) -> dict[str, object]:
    options = web_database_pool_options({WEB_DATABASE_POOL_ENV: "enabled"})
    assert options is not None
    return {**options, "min_size": 1, "max_size": 1, **overrides}


@pytest.fixture
def owner() -> Iterator[psycopg.Connection[Any]]:
    """An independent session of the test database owner (outside Django's pool)."""
    settings = connection.settings_dict
    session = psycopg.connect(
        dbname=settings["NAME"], host=settings["HOST"], port=settings["PORT"],
        user=settings["USER"], password=settings["PASSWORD"], autocommit=True,
    )
    try:
        yield session
    finally:
        session.close()


def _backend() -> tuple[int, str]:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid(), session_user")
        row = cursor.fetchone()
    assert row is not None
    return int(row[0]), str(row[1])


def _web_backends(owner: psycopg.Connection[Any]) -> set[int]:
    rows = owner.execute(
        "SELECT pid FROM pg_stat_activity WHERE usename = 'aegis_web' "
        "AND datname = current_database()"
    ).fetchall()
    return {int(row[0]) for row in rows}


def _session_settings() -> tuple[str, str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_setting('statement_timeout'), current_setting('lock_timeout'), "
            "current_setting('TimeZone')"
        )
        row = cursor.fetchone()
    assert row is not None
    return str(row[0]), str(row[1]), str(row[2])


@pytest.fixture
def checkout_pids() -> Iterator[list[int]]:
    """Backend PIDs handed to Django, recorded at every pool checkout."""
    pids: list[int] = []

    def record(sender: object, connection: Any, **_kwargs: object) -> None:
        pids.append(int(connection.connection.info.backend_pid))

    connection_created.connect(record)
    try:
        yield pids
    finally:
        connection_created.disconnect(record)


@pytest.fixture
def lifetime_enforcement(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The web server's request_started lifetime check, with a short test interval."""
    monkeypatch.setattr(database_pool, "_enforcer", PoolLifetimeEnforcer(0.2))
    request_started.connect(enforce_pool_lifetime, dispatch_uid="test-pool-lifetime")
    try:
        yield
    finally:
        request_started.disconnect(dispatch_uid="test-pool-lifetime")


def test_pooled_requests_reuse_one_web_login_without_leaking_session_state(
    role_database: RoleDatabase, owner: psycopg.Connection[Any],
) -> None:
    with role_database.as_pooled_django_role("aegis_web", _pool()):
        first = _backend()
        baseline = _session_settings()
        connection.close()

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '5s'")
            cursor.execute("SET LOCAL lock_timeout = '1s'")
            cursor.execute("SELECT pg_advisory_xact_lock(424242)")
        connection.close()

        assert _backend() == first
        assert _session_settings() == baseline
        assert first[1] == "aegis_web"

        # A request that ends inside an explicit transaction is rolled back before
        # its login serves anyone else: no lock or snapshot survives the checkout.
        raw = connection.connection
        assert raw is not None
        raw.execute("BEGIN")
        raw.execute("SELECT pg_advisory_xact_lock(434343)")
        connection.close()

        state, locks = owner.execute(
            "SELECT state, (SELECT count(*) FROM pg_locks WHERE pid = %s "
            "AND locktype = 'advisory') FROM pg_stat_activity WHERE pid = %s",
            [first[0], first[0]],
        ).fetchone() or (None, None)
        assert (state, locks) == ("idle", 0)
        assert _backend() == first
        assert _session_settings() == baseline


def test_revoked_grant_takes_effect_on_a_reused_pooled_login(
    browse_fixture: Any, role_database: RoleDatabase, owner: psycopg.Connection[Any],
) -> None:
    browse_fixture.entry(b"pooled-entry")
    with role_database.as_pooled_django_role("aegis_web", _pool()):
        assert len(browse_fixture.page()["entries"]) == 1
        before = _backend()
        connection.close()

        owner.execute("DELETE FROM roots_rootgrant WHERE root_id = %s", [browse_fixture.root.pk])

        with pytest.raises(CatalogNotFound):
            browse_fixture.page()
        assert _backend() == before


@pytest.mark.parametrize("revocation", ["rotated-password", "login-revoked"])
def test_rotated_or_revoked_web_login_stops_serving_within_the_pool_lifetime(
    role_database: RoleDatabase, owner: psycopg.Connection[Any], revocation: str,
    lifetime_enforcement: None, checkout_pids: list[int],
) -> None:
    lifetime = 2.0
    original = role_database.passwords["aegis_web"]
    restore = sql.SQL("ALTER ROLE aegis_web LOGIN PASSWORD {}").format(sql.Literal(original))
    with role_database.as_pooled_django_role(
        "aegis_web", _pool(max_lifetime=lifetime, timeout=1.0),
    ):
        stale, _role = _backend()
        connection.close()
        try:
            if revocation == "rotated-password":
                owner.execute(sql.SQL("ALTER ROLE aegis_web PASSWORD {}").format(
                    sql.Literal("rotated-" + secrets.token_hex(16))))
            else:
                owner.execute("ALTER ROLE aegis_web NOLOGIN")
            # The login authenticated before the change is retired once its lifetime
            # ends, before the next request can take it, even after an idle period.
            time.sleep(lifetime + 0.5)
            checkout_pids.clear()
            Client().get("/health/live")
            with pytest.raises(OperationalError) as refused:
                connection.ensure_connection()
            assert _database_is_unavailable(refused.value)
            assert stale not in checkout_pids
            assert stale not in _web_backends(owner)
        finally:
            owner.execute(restore)
            connection.close()


def test_an_idle_expired_login_is_replaced_before_a_request_uses_it(
    role_database: RoleDatabase, owner: psycopg.Connection[Any],
    lifetime_enforcement: None, checkout_pids: list[int],
) -> None:
    with role_database.as_pooled_django_role("aegis_web", _pool(max_lifetime=1.0)):
        stale, _role = _backend()
        connection.close()
        time.sleep(1.5)

        checkout_pids.clear()
        response = Client().get("/health/ready")
        connection.close()

        assert response.status_code in (200, 503)
        assert checkout_pids and stale not in checkout_pids
        assert stale not in _web_backends(owner)
        assert _backend()[1] == "aegis_web"


def test_a_terminated_backend_is_replaced_without_failing_the_request(
    role_database: RoleDatabase, owner: psycopg.Connection[Any], checkout_pids: list[int],
) -> None:
    with role_database.as_pooled_django_role("aegis_web", _pool()):
        terminated, _role = _backend()
        connection.close()
        owner.execute("SELECT pg_terminate_backend(%s)", [terminated])

        checkout_pids.clear()
        replacement, role = _backend()

        assert role == "aegis_web"
        assert replacement != terminated
        assert terminated not in checkout_pids
