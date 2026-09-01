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
