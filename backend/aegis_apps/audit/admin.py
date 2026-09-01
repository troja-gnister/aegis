from django.contrib import admin
from django.http import HttpRequest

from .models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None
    list_display = ("occurred_at", "event_type", "outcome", "actor", "request_id")
    list_filter = ("event_type", "outcome")
    search_fields = ("request_id", "actor__id", "object_id", "root_id")

    def get_readonly_fields(
        self, request: HttpRequest, obj: AuditEvent | None = None
    ) -> tuple[str, ...]:
        del request, obj
        return tuple(field.name for field in self.model._meta.fields)

    def has_add_permission(self, request: HttpRequest) -> bool:
        del request
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: AuditEvent | None = None
    ) -> bool:
        del request, obj
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: AuditEvent | None = None
    ) -> bool:
        del request, obj
        return False
