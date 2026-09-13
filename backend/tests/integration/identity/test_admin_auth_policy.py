from __future__ import annotations

from typing import Any

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.identity.models import User
from django.contrib.sessions.models import Session
from django.test import Client, override_settings
from django.test.client import MULTIPART_CONTENT

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
PASSWORD = "synthetic-administrator-password"


@pytest.fixture
def administrator() -> User:
    return User.objects.create_superuser(username="administrator", password=PASSWORD)


def _login(client: Client, route: str, password: str = PASSWORD) -> Any:
    client.get("/api/v1/auth/csrf")
    payload = {"username": "administrator", "password": password}
    if route.startswith("/admin/"):
        payload["next"] = "/admin/"
    return client.post(
        route,
        payload,
        content_type=(
            "application/json"
            if route.startswith("/api/")
            else MULTIPART_CONTENT
        ),
        headers={"X-CSRFToken": client.cookies["csrftoken"].value},
    )


@pytest.mark.parametrize("blocked_route", ["/api/v1/auth/login", "/admin/login/"])
def test_admin_and_api_login_share_account_throttle(
    administrator: User, blocked_route: str
) -> None:
    client = Client(enforce_csrf_checks=True)
    exhausting_route = (
        "/admin/login/" if blocked_route.startswith("/api/") else "/api/v1/auth/login"
    )
    for _ in range(5):
        _login(client, exhausting_route, "wrong fixture password")
    response = _login(client, blocked_route)
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0
    assert client.get("/api/v1/auth/session").status_code == 401
    assert not AuditEvent.objects.filter(event_type="auth.login.succeeded").exists()
    assert AuditEvent.objects.filter(event_type="auth.login.throttled").count() == 1


def test_admin_login_failure_success_and_logout_use_auth_audit(administrator: User) -> None:
    client = Client(enforce_csrf_checks=True)
    _login(client, "/admin/login/", "wrong fixture password")
    response = _login(client, "/admin/login/")
    assert response.status_code == 302
    assert response.url == "/admin/"
    session_key = client.cookies["sessionid"].value
    assert client.get("/admin/").status_code == 200
    assert client.get("/api/v1/auth/session").status_code == 200
    assert client.post("/admin/logout/").status_code == 403
    assert client.get("/admin/logout/").status_code == 405
    response = client.post(
        "/admin/logout/", headers={"X-CSRFToken": client.cookies["csrftoken"].value}
    )
    assert response.status_code == 200
    assert not Session.objects.filter(session_key=session_key).exists()
    assert client.get("/api/v1/auth/session").status_code == 401
    assert list(
        AuditEvent.objects.order_by("occurred_at").values_list("event_type", "actor_id")
    ) == [
        ("auth.login.failed", None),
        ("auth.login.succeeded", administrator.pk),
        ("auth.logout", administrator.pk),
    ]


def test_admin_login_fails_closed_without_throttle_secret(administrator: User) -> None:
    with override_settings(AEGIS_AUTH_THROTTLE_HMAC_KEY=None):
        client = Client(enforce_csrf_checks=True)
        response = _login(client, "/admin/login/")
    assert response.status_code == 503
    assert b"Unable to sign in" in response.content
    assert client.get("/api/v1/auth/session").status_code == 401


def test_admin_login_keeps_csrf_and_staff_boundary(administrator: User) -> None:
    client = Client(enforce_csrf_checks=True)
    assert (
        client.post(
            "/admin/login/", {"username": "administrator", "password": PASSWORD}
        ).status_code
        == 403
    )
    administrator.is_superuser = False
    administrator.is_staff = False
    administrator.save(update_fields=["is_superuser", "is_staff"])
    response = _login(client, "/admin/login/")
    assert response.status_code == 200
    assert b"Unable to sign in" in response.content
    assert client.get("/api/v1/auth/session").status_code == 401
    assert AuditEvent.objects.filter(event_type="auth.login.failed").count() == 1


@pytest.mark.parametrize("method", ["get", "post"])
def test_site_wide_password_change_is_disabled(administrator: User, method: str) -> None:
    client = Client(enforce_csrf_checks=True)
    assert _login(client, "/admin/login/").status_code == 302
    original_hash = administrator.password
    response = getattr(client, method)(
        "/admin/password_change/",
        {
            "old_password": PASSWORD,
            "new_password1": "different-fixture-password",
            "new_password2": "different-fixture-password",
        },
        headers={"X-CSRFToken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 404
    administrator.refresh_from_db()
    assert administrator.password == original_hash
    assert b"/admin/password_change/" not in client.get("/admin/").content
