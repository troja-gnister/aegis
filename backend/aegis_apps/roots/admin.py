from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from django import forms
from django.contrib import admin
from django.forms import BaseInlineFormSet, ModelForm
from django.http import HttpRequest

from aegis_apps.identity.models import User

from .manifest import ManifestError, configured_manifest
from .models import Root, RootGrant
from .permissions import validate_permission_mask
from .services import (
    activate_root,
    create_root,
    deactivate_root,
    remove_grant,
    set_group_grant,
    set_user_grant,
    update_root,
)


class RootAdminForm(forms.ModelForm):  # type: ignore[type-arg]
    slot_id = forms.ChoiceField(choices=())

    class Meta:
        model = Root
        fields = ("slot_id", "display_name", "mode", "active")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        choices: tuple[tuple[str, str], ...] = ()
        try:
            manifest = configured_manifest()
        except ManifestError:
            manifest = None
        if manifest is not None:
            choices = tuple((slot_id, slot_id) for slot_id in sorted(manifest.slots))
        slot_field = cast(forms.ChoiceField, self.fields["slot_id"])
        slot_field.choices = choices


class RootGrantAdminForm(forms.ModelForm):  # type: ignore[type-arg]
    permissions = forms.IntegerField(min_value=0, max_value=255)

    class Meta:
        model = RootGrant
        fields = ("root", "user", "group", "permissions")

    def clean_permissions(self) -> int:
        value = self.cleaned_data["permissions"]
        return int(validate_permission_mask(value))


def _actor(request: HttpRequest) -> User:
    if not isinstance(request.user, User):
        raise PermissionError("root administration requires an Aegis user")
    return request.user


def _request_id(request: HttpRequest) -> str:
    value = getattr(request, "request_id", None)
    if not isinstance(value, str):
        raise PermissionError("root administration requires request identity")
    return value


def _replace_state(target: Root | RootGrant, source: Root | RootGrant) -> None:
    target.__dict__.update(source.__dict__)


def _reject_inline_writes(
    formsets: Sequence[BaseInlineFormSet[Any, Any, Any]],
) -> None:
    if formsets:
        raise PermissionError("root administration inline writes are disabled")


class CanonicalAuditOnlyAdminMixin:
    def log_addition(self, request: HttpRequest, obj: Any, message: Any) -> Any:
        del request, obj, message
        return None

    def log_change(self, request: HttpRequest, obj: Any, message: Any) -> Any:
        del request, obj, message
        return None

    def log_deletion(self, request: HttpRequest, obj: Any, object_repr: str) -> Any:
        del request, obj, object_repr
        return None

    def log_deletions(self, request: HttpRequest, queryset: Any) -> Any:
        del request, queryset
        return None


@admin.register(Root)
class RootAdmin(CanonicalAuditOnlyAdminMixin, admin.ModelAdmin):  # type: ignore[type-arg]
    form = RootAdminForm
    actions = ("activate_roots", "deactivate_roots")
    list_display = ("display_name", "slot_id", "mode", "active", "authorization_epoch")
    readonly_fields = (
        "authorization_epoch",
        "capabilities",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            None,
            {"fields": ("slot_id", "display_name", "mode", "active")},
        ),
        (
            "Trusted state",
            {
                "fields": (
                    "authorization_epoch",
                    "capabilities",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    def get_actions(self, request: HttpRequest) -> dict[str, Any]:
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def has_delete_permission(self, request: HttpRequest, obj: Root | None = None) -> bool:
        del request, obj
        return False

    def save_model(
        self, request: HttpRequest, obj: Root, form: ModelForm[Any], change: bool
    ) -> None:
        if change:
            saved = update_root(
                actor=_actor(request),
                root_id=obj.id,
                display_name=obj.display_name,
                mode=obj.mode,
                active=obj.active,
                request_id=_request_id(request),
            )
        else:
            saved = create_root(
                actor=_actor(request),
                slot_id=obj.slot_id,
                display_name=obj.display_name,
                mode=obj.mode,
                active=obj.active,
                request_id=_request_id(request),
            )
        _replace_state(obj, saved)

    def save_related(
        self,
        request: HttpRequest,
        form: ModelForm[Any],
        formsets: Sequence[BaseInlineFormSet[Any, Any, Any]],
        change: bool,
    ) -> None:
        del request, form, change
        _reject_inline_writes(formsets)

    def delete_model(self, request: HttpRequest, obj: Root) -> None:
        del request, obj
        raise PermissionError("root deletion is disabled")

    def delete_queryset(self, request: HttpRequest, queryset: Any) -> None:
        del request, queryset
        raise PermissionError("root bulk deletion is disabled")

    @admin.action(description="Activate selected roots")
    def activate_roots(self, request: HttpRequest, queryset: Any) -> None:
        for root_id in queryset.values_list("pk", flat=True).iterator():
            activate_root(
                actor=_actor(request),
                root_id=root_id,
                request_id=_request_id(request),
            )

    @admin.action(description="Deactivate selected roots")
    def deactivate_roots(self, request: HttpRequest, queryset: Any) -> None:
        for root_id in queryset.values_list("pk", flat=True).iterator():
            deactivate_root(
                actor=_actor(request),
                root_id=root_id,
                request_id=_request_id(request),
            )


@admin.register(RootGrant)
class RootGrantAdmin(CanonicalAuditOnlyAdminMixin, admin.ModelAdmin):  # type: ignore[type-arg]
    form = RootGrantAdminForm
    actions = None
    list_display = ("id", "root", "permissions", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")

    def get_actions(self, request: HttpRequest) -> dict[str, Any]:
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    def get_readonly_fields(
        self, request: HttpRequest, obj: RootGrant | None = None
    ) -> tuple[str, ...]:
        del request
        if obj is None:
            return self.readonly_fields
        return (*self.readonly_fields, "root", "user", "group")

    def save_model(
        self, request: HttpRequest, obj: RootGrant, form: ModelForm[Any], change: bool
    ) -> None:
        if change:
            stored = RootGrant.objects.get(pk=obj.pk)
            root_id = stored.root_id
            user_id = stored.user_id
            group_id = stored.group_id
        else:
            root_id = obj.root_id
            user_id = obj.user_id
            group_id = obj.group_id
        if user_id is not None:
            saved = set_user_grant(
                actor=_actor(request),
                root_id=root_id,
                user_id=user_id,
                permissions=validate_permission_mask(obj.permissions),
                request_id=_request_id(request),
            )
        elif group_id is not None:
            saved = set_group_grant(
                actor=_actor(request),
                root_id=root_id,
                group_id=group_id,
                permissions=validate_permission_mask(obj.permissions),
                request_id=_request_id(request),
            )
        else:
            raise ValueError("root grant requires a principal")
        _replace_state(obj, saved)

    def save_related(
        self,
        request: HttpRequest,
        form: ModelForm[Any],
        formsets: Sequence[BaseInlineFormSet[Any, Any, Any]],
        change: bool,
    ) -> None:
        del request, form, change
        _reject_inline_writes(formsets)

    def delete_model(self, request: HttpRequest, obj: RootGrant) -> None:
        remove_grant(
            actor=_actor(request),
            grant_id=obj.id,
            request_id=_request_id(request),
        )

    def delete_queryset(self, request: HttpRequest, queryset: Any) -> None:
        del request, queryset
        raise PermissionError("root grant bulk deletion is disabled")
