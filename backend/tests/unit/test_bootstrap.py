from django.conf import settings


def test_platform_uses_uuid_custom_user() -> None:
    from aegis_apps.identity.models import User

    assert settings.AUTH_USER_MODEL == "identity.User"
    assert User._meta.pk.name == "id"
    assert User._meta.get_field("authorization_epoch").default == 0
