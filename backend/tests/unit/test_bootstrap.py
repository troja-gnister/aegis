import pytest
from django.conf import settings
from django.test import Client


def test_platform_uses_uuid_custom_user() -> None:
    from aegis_apps.identity.models import User

    assert settings.AUTH_USER_MODEL == "identity.User"
    assert User._meta.pk.name == "id"
    assert User._meta.get_field("authorization_epoch").default == 0


def test_live_health_endpoint_is_unauthenticated_and_get_only() -> None:
    client = Client()

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert client.post("/health/live").status_code == 405


def test_proxy_attestation_is_empty_no_store_get_only_and_has_no_database_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aegis_apps.common.health.database_status",
        lambda: (_ for _ in ()).throw(AssertionError("database queried")),
    )
    client = Client()

    response = client.get(
        "/health/proxy-attestation",
        REMOTE_ADDR="192.0.2.254",
        HTTP_X_AEGIS_PROXY_ATTESTATION="startup-v1",
    )

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["Cache-Control"] == "private, no-store"
    assert client.post("/health/proxy-attestation").status_code == 405


def test_proxy_attestation_failures_are_indistinguishable() -> None:
    client = Client()

    wrong_header = client.get(
        "/health/proxy-attestation",
        REMOTE_ADDR="192.0.2.254",
        HTTP_X_AEGIS_PROXY_ATTESTATION="wrong",
    )
    wrong_remote = client.get(
        "/health/proxy-attestation",
        REMOTE_ADDR="192.0.2.253",
        HTTP_X_AEGIS_PROXY_ATTESTATION="startup-v1",
    )

    assert wrong_header.status_code == wrong_remote.status_code == 404
    assert wrong_header.content == wrong_remote.content == b""
    assert wrong_header.headers["Cache-Control"] == "private, no-store"
    assert wrong_remote.headers["Cache-Control"] == "private, no-store"
