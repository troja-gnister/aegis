from __future__ import annotations

import pytest
from aegis_apps.media.models import MediaAsset, MediaDerivative
from django.contrib import admin
from django.db import models
from django.test import RequestFactory

PERSONAL_ASSET_FIELDS = frozenset(
    {
        "width",
        "height",
        "orientation",
        "captured_at",
        "captured_at_source",
        "camera_make",
        "camera_model",
        "duration_ms",
        "video_codec",
        "audio_codec",
        "container",
        "has_location",
        "sort_at",
        "sort_source",
        "device",
        "inode",
        "size",
        "mtime_ns",
    }
)


@pytest.mark.parametrize("model", [MediaAsset, MediaDerivative])
def test_media_admin_is_completely_read_only(model: type[models.Model]) -> None:
    registered = admin.site._registry[model]
    request = RequestFactory().get("/")
    model_fields = {field.name for field in model._meta.fields}

    assert registered.actions is None
    shown = registered.get_fields(request)
    assert shown
    assert set(shown) <= model_fields
    assert registered.get_readonly_fields(request) == shown
    assert registered.has_add_permission(request) is False
    assert registered.has_change_permission(request) is False
    assert registered.has_delete_permission(request) is False
    for column in registered.list_display:
        assert isinstance(column, str)
        assert "__" not in column


def test_asset_admin_shows_only_operational_columns() -> None:
    registered = admin.site._registry[MediaAsset]
    request = RequestFactory().get("/")
    visible = {
        *registered.get_fields(request),
        *(column for column in registered.list_display if isinstance(column, str)),
        *(column for column in registered.list_filter if isinstance(column, str)),
        *(column for column in registered.search_fields),
    }

    assert not visible & PERSONAL_ASSET_FIELDS
    assert set(registered.get_fields(request)) == (
        {field.name for field in MediaAsset._meta.fields} - PERSONAL_ASSET_FIELDS
    )


def test_derivative_admin_shows_every_column() -> None:
    registered = admin.site._registry[MediaDerivative]
    request = RequestFactory().get("/")

    assert set(registered.get_fields(request)) == {
        field.name for field in MediaDerivative._meta.fields
    }
