from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from .models import User


@admin.register(User)
class AegisUserAdmin(UserAdmin):  # type: ignore[type-arg]
    readonly_fields = (*tuple(UserAdmin.readonly_fields), "authorization_epoch")
    fieldsets = (
        *tuple(UserAdmin.fieldsets or ()),
        ("Aegis", {"fields": ("authorization_epoch",)}),
    )
