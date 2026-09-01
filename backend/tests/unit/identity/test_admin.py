from aegis_apps.identity.models import User
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from django.test import RequestFactory


def test_user_admin_exposes_authorization_epoch_as_read_only() -> None:
    registered_admin = admin.site._registry[User]
    request = RequestFactory().get("/")

    assert isinstance(registered_admin, UserAdmin)
    assert "authorization_epoch" in registered_admin.get_readonly_fields(request=request)
    assert any(
        "authorization_epoch" in fieldset[1]["fields"]
        for fieldset in registered_admin.get_fieldsets(request=request, obj=User())
    )


def test_django_group_admin_remains_registered() -> None:
    assert isinstance(admin.site._registry[Group], GroupAdmin)
