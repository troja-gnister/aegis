from aegis_apps.audit.models import AuditEvent
from django.contrib import admin
from django.test import RequestFactory


def test_audit_admin_is_completely_read_only() -> None:
    registered = admin.site._registry[AuditEvent]
    request = RequestFactory().get("/")

    assert registered.actions is None
    assert registered.get_readonly_fields(request) == tuple(
        field.name for field in AuditEvent._meta.fields
    )
    assert registered.has_add_permission(request) is False
    assert registered.has_change_permission(request) is False
    assert registered.has_delete_permission(request) is False
