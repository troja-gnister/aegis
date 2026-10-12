"""Read-only admin views; only the media worker writes media records."""

from __future__ import annotations

from django.contrib import admin
from django.db import models
from django.http import HttpRequest

from .models import MediaAsset, MediaDerivative


class ReadOnlyMediaAdmin(admin.ModelAdmin):  # type: ignore[type-arg]
    actions = None
    show_full_result_count = False
    # Operational columns only. Admin views are not filtered by root grants, so personal
    # metadata (capture time, camera, location flag) is not shown to staff.
    detail_fields: tuple[str, ...] = ()

    def get_fields(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> tuple[str, ...]:
        del request, obj
        return self.detail_fields

    def get_readonly_fields(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> tuple[str, ...]:
        del request, obj
        return self.detail_fields

    def has_add_permission(self, request: HttpRequest) -> bool:
        del request
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> bool:
        del request, obj
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: models.Model | None = None
    ) -> bool:
        del request, obj
        return False


@admin.register(MediaAsset)
class MediaAssetAdmin(ReadOnlyMediaAdmin):
    detail_fields = (
        "entry",
        "root",
        "kind",
        "fingerprint",
        "probe_state",
        "thumbnail_state",
        "poster_state",
        "lane",
        "attempts",
        "attempt_token",
        "environment_failures",
        "next_attempt_at",
        "lease_owner",
        "lease_expires_at",
        "processor_version",
        "safe_error_code",
        "created_at",
        "updated_at",
    )
    list_display = (
        "entry_id",
        "kind",
        "probe_state",
        "thumbnail_state",
        "poster_state",
        "lane",
        "attempts",
        "safe_error_code",
        "updated_at",
    )
    list_filter = ("kind", "probe_state", "thumbnail_state", "poster_state", "lane")
    search_fields = ("fingerprint",)


@admin.register(MediaDerivative)
class MediaDerivativeAdmin(ReadOnlyMediaAdmin):
    detail_fields = (
        "id",
        "asset",
        "kind",
        "fingerprint",
        "params_digest",
        "key",
        "extension",
        "bytes",
        "width",
        "height",
        "state",
        "evictable",
        "created_at",
        "last_access_at",
    )
    list_display = (
        "key",
        "asset_id",
        "kind",
        "state",
        "extension",
        "bytes",
        "evictable",
        "last_access_at",
    )
    list_filter = ("kind", "state", "evictable")
    search_fields = ("key",)
