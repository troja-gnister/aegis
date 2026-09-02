from __future__ import annotations

import uuid

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.identity.models import LoginThrottleBucket, User
from django.contrib.auth import authenticate
from django.contrib.sessions.models import Session
from django.test import Client, override_settings

pytestmark = pytest.mark.integration
PASSWORD = "a-long-test-password"


@pytest.fixture
def user() -> User:
    return User.objects.create_user(username="alice", password=PASSWORD)


def _csrf_token(client: Client) -> str:
    response = client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    token = response.json()["csrfToken"]
    assert isinstance(token, str)
    return token


@pytest.mark.django_db(transaction=True)
def test_login_requires_csrf_rotates_session_and_logout_revokes(user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)
    original_csrf_cookie = client.cookies["csrftoken"].value

    rejected = client.post(
        "/api/v1/auth/login",
        {"username": "alice", "password": PASSWORD},
        content_type="application/json",
    )

    assert rejected.status_code == 403
    assert rejected.json() == {
        "type": "csrf_failed",
        "title": "Request verification failed",
    }

    login = client.post(
        "/api/v1/auth/login",
        {"username": "alice", "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )

    assert login.status_code == 200
    assert login.json() == {"user": {"id": str(user.pk), "username": "alice"}}
    assert client.cookies["sessionid"]["httponly"] is True
    assert client.cookies["sessionid"]["samesite"] == "Lax"
    assert client.cookies["csrftoken"].value != original_csrf_cookie
    session_key = client.cookies["sessionid"].value

    session = client.get("/api/v1/auth/session")

    assert session.status_code == 200
    assert session.json()["user"] == {"id": str(user.pk), "username": "alice"}
    cache_namespace = session.json()["cacheNamespace"]
    assert isinstance(cache_namespace, str)
    assert len(cache_namespace) >= 32
    assert session_key not in cache_namespace

    rotated_csrf = client.cookies["csrftoken"].value
    logout = client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRFToken": rotated_csrf},
    )

    assert logout.status_code == 204
    assert client.get("/api/v1/auth/session").status_code == 401
    assert [
        (event.event_type, event.actor_id) for event in AuditEvent.objects.order_by("occurred_at")
    ] == [
        ("auth.login.succeeded", user.pk),
        ("auth.logout", user.pk),
    ]


@pytest.mark.django_db(transaction=True)
def test_stale_authorization_epoch_is_revoked_on_non_auth_route(user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)
    login = client.post(
        "/api/v1/auth/login",
        {"username": user.username, "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert login.status_code == 200
    old_session_key = client.cookies["sessionid"].value
    User.objects.filter(pk=user.pk).update(authorization_epoch=1)

    health = client.get("/health/live")

    assert health.status_code == 200
    assert client.session.session_key != old_session_key
    revoked = AuditEvent.objects.get(event_type="auth.session.revoked")
    assert revoked.actor is None
    assert revoked.metadata == {"request_id": health.headers["X-Request-ID"]}
    assert uuid.UUID(revoked.request_id)


@pytest.mark.django_db(transaction=True)
def test_deactivated_user_session_is_flushed_and_audited_once(user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)
    login_response = client.post(
        "/api/v1/auth/login",
        {"username": user.username, "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert login_response.status_code == 200
    session_key = client.cookies["sessionid"].value
    assert Session.objects.filter(session_key=session_key).exists()
    User.objects.filter(pk=user.pk).update(is_active=False)

    health = client.get("/health/live")

    assert health.status_code == 200
    assert not Session.objects.filter(session_key=session_key).exists()
    assert client.get("/api/v1/auth/session").status_code == 401
    revoked_events = AuditEvent.objects.filter(event_type="auth.session.revoked")
    assert revoked_events.count() == 1
    revoked = revoked_events.get()
    assert revoked.actor is None
    assert revoked.metadata == {"request_id": health.headers["X-Request-ID"]}


@pytest.mark.django_db(transaction=True)
def test_same_user_login_rotates_session_and_cache_namespace(user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)
    first_login = client.post(
        "/api/v1/auth/login",
        {"username": user.username, "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert first_login.status_code == 200
    first_session_key = client.cookies["sessionid"].value
    first_namespace = client.get("/api/v1/auth/session").json()["cacheNamespace"]

    second_login = client.post(
        "/api/v1/auth/login",
        {"username": user.username, "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": client.cookies["csrftoken"].value},
    )

    assert second_login.status_code == 200
    second_session_key = client.cookies["sessionid"].value
    second_namespace = client.get("/api/v1/auth/session").json()["cacheNamespace"]
    assert second_session_key != first_session_key
    assert second_namespace != first_namespace
    assert not Session.objects.filter(session_key=first_session_key).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "payload",
    [
        {"username": 7, "password": PASSWORD},
        {"username": "alice", "password": [PASSWORD]},
        {"username": "a" * 151, "password": PASSWORD},
        {"username": "alice", "password": "£" * 513},
        {"username": "alice", "password": PASSWORD, "extra": "rejected"},
    ],
)
def test_invalid_login_shape_is_bounded_before_authentication(
    user: User, payload: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    del user
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)

    def authentication_must_not_run(**_kwargs: object) -> None:
        raise AssertionError("authentication ran for an invalid request")

    monkeypatch.setattr("aegis_apps.identity.api.authenticate", authentication_must_not_run)

    response = client.post(
        "/api/v1/auth/login",
        payload,
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 400
    assert response.json() == {"type": "invalid_request", "title": "Invalid request"}
    assert AuditEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_oversized_login_body_returns_safe_json_before_authentication(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    del user
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)

    def authentication_must_not_run(**_kwargs: object) -> None:
        raise AssertionError("authentication ran for an oversized request")

    monkeypatch.setattr("aegis_apps.identity.api.authenticate", authentication_must_not_run)
    response = client.post(
        "/api/v1/auth/login",
        {"username": "alice", "password": "x" * 5000},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 400
    assert response.json() == {"type": "invalid_request", "title": "Invalid request"}
    assert AuditEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_malformed_login_json_returns_safe_problem_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)

    def authentication_must_not_run(**_kwargs: object) -> None:
        raise AssertionError("authentication ran for malformed JSON")

    monkeypatch.setattr("aegis_apps.identity.api.authenticate", authentication_must_not_run)
    response = client.post(
        "/api/v1/auth/login",
        b'{"username":"alice","password":',
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )

    assert response.status_code == 400
    assert response.json() == {"type": "invalid_request", "title": "Invalid request"}
    assert AuditEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_invalid_accounts_share_problem_and_opaque_audit(
    user: User, caplog: pytest.LogCaptureFixture
) -> None:
    inactive = User.objects.create_user(
        username="inactive-account",
        password=PASSWORD,
        is_active=False,
    )
    attempts = [
        ("missing-account", PASSWORD),
        (user.username, "wrong-test-password"),
        (inactive.username, PASSWORD),
    ]

    for username, password in attempts:
        client = Client(enforce_csrf_checks=True)
        token = _csrf_token(client)
        response = client.post(
            "/api/v1/auth/login",
            {"username": username, "password": password},
            content_type="application/json",
            headers={"X-CSRFToken": token, "X-Request-ID": "safe-request-1234"},
            REMOTE_ADDR="192.0.2.15",
        )
        assert response.status_code == 401
        assert response.json() == {
            "type": "invalid_credentials",
            "title": "Unable to sign in",
        }

    events = list(AuditEvent.objects.order_by("occurred_at"))
    assert len(events) == 1
    assert all(event.event_type == "auth.login.failed" for event in events)
    assert all(event.actor is None for event in events)
    assert all(
        event.metadata
        == {
            "bucket_type": "account",
            "request_id": "safe-request-1234",
        }
        for event in events
    )
    serialized = " ".join([caplog.text, *(str(event.metadata) for event in events)])
    assert "missing-account" not in serialized
    assert "inactive-account" not in serialized
    assert "wrong-test-password" not in serialized
    assert "192.0.2.15" not in serialized


@pytest.mark.django_db(transaction=True)
def test_logout_requires_rotated_csrf_token(user: User) -> None:
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)
    login_response = client.post(
        "/api/v1/auth/login",
        {"username": user.username, "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": token},
    )
    assert login_response.status_code == 200

    response = client.post("/api/v1/auth/logout")

    assert response.status_code == 403
    assert response.json() == {
        "type": "csrf_failed",
        "title": "Request verification failed",
    }
    assert authenticate(username=user.username, password=PASSWORD) == user


@pytest.mark.django_db(transaction=True)
def test_non_api_csrf_failure_is_plain_and_safe() -> None:
    client = Client(enforce_csrf_checks=True)

    response = client.post("/admin/login/", {"username": "x", "password": "secret"})

    assert response.status_code == 403
    assert response.headers["Content-Type"].startswith("text/plain")
    assert response.content == b"Request verification failed"


@pytest.mark.django_db(transaction=True)
def test_account_throttle_returns_429_after_five_failures(
    user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    del user

    def invalid_credentials(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("aegis_apps.identity.api.authenticate", invalid_credentials)
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/login",
            {"username": "limited-account", "password": PASSWORD},
            content_type="application/json",
            headers={"X-CSRFToken": token},
            REMOTE_ADDR="192.0.2.41",
        )
        assert response.status_code == 401

    blocked = client.post(
        "/api/v1/auth/login",
        {"username": "LIMITED-ACCOUNT", "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": token},
        REMOTE_ADDR="192.0.2.42",
    )

    assert blocked.status_code == 429
    assert blocked.json() == {"type": "login_throttled", "title": "Unable to sign in"}
    assert int(blocked.headers["Retry-After"]) > 0
    events = list(AuditEvent.objects.order_by("occurred_at"))
    assert [event.event_type for event in events] == [
        "auth.login.failed",
        "auth.login.throttled",
    ]
    assert all(event.actor is None for event in events)
    assert all(set(event.metadata) == {"bucket_type", "request_id"} for event in events)


@pytest.mark.django_db(transaction=True)
def test_ip_throttle_uses_remote_addr_and_ignores_forwarding_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def invalid_credentials(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("aegis_apps.identity.api.authenticate", invalid_credentials)
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)
    for index in range(20):
        response = client.post(
            "/api/v1/auth/login",
            {"username": f"rotating-{index}", "password": PASSWORD},
            content_type="application/json",
            headers={
                "X-CSRFToken": token,
                "X-Forwarded-For": f"198.51.100.{index + 1}",
                "X-Real-IP": f"203.0.113.{index + 1}",
            },
            REMOTE_ADDR="192.0.2.51",
        )
        assert response.status_code == 401

    blocked = client.post(
        "/api/v1/auth/login",
        {"username": "next-account", "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": token, "X-Forwarded-For": "203.0.113.250"},
        REMOTE_ADDR="192.0.2.51",
    )

    assert blocked.status_code == 429
    assert LoginThrottleBucket.objects.filter(kind=LoginThrottleBucket.Kind.IP).count() == 1
    throttled = AuditEvent.objects.filter(event_type="auth.login.throttled")
    assert throttled.count() == 1
    assert throttled.get().metadata["bucket_type"] == "ip"


@pytest.mark.django_db(transaction=True)
@override_settings(AEGIS_AUTH_THROTTLE_HMAC_KEY=None)
def test_missing_throttle_secret_fails_closed_before_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def authentication_must_not_run(**_kwargs: object) -> None:
        raise AssertionError("authentication ran without throttling")

    monkeypatch.setattr("aegis_apps.identity.api.authenticate", authentication_must_not_run)
    client = Client(enforce_csrf_checks=True)
    token = _csrf_token(client)

    response = client.post(
        "/api/v1/auth/login",
        {"username": "alice", "password": PASSWORD},
        content_type="application/json",
        headers={"X-CSRFToken": token},
        REMOTE_ADDR="192.0.2.61",
    )

    assert response.status_code == 503
    assert response.json() == {
        "type": "authentication_unavailable",
        "title": "Unable to sign in",
    }
