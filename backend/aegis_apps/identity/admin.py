from __future__ import annotations

from collections.abc import Sequence
from typing import Any, NoReturn, cast

from django import forms
from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashWidget, UserChangeForm
from django.contrib.auth.models import Group
from django.forms import BaseInlineFormSet, ModelForm
from django.http import Http404, HttpRequest
from django.urls import URLPattern, path
from django.utils.html import format_html
from django.utils.safestring import SafeString

from .admin_services import save_group_from_admin, save_user_from_admin, set_user_active
from .models import User


class DisabledPasswordHashWidget(ReadOnlyPasswordHashWidget):
    def render(
        self,
        name: str,
        value: object,
        attrs: dict[str, Any] | None = None,
        renderer: Any = None,
    ) -> SafeString:
        del name, value, attrs, renderer
        return format_html(
            "<p>{}</p>", "Password changes are unavailable in this administration view."
        )


class AegisUserChangeForm(UserChangeForm):  # type: ignore[type-arg]
    class Meta:
        model = User
        fields = "__all__"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["password"].widget = DisabledPasswordHashWidget()


class AegisGroupAdminForm(forms.ModelForm):  # type: ignore[type-arg]
    members = forms.ModelMultipleChoiceField(queryset=User.objects.none(), required=False)

    class Meta:
        model = Group
        fields = ("name", "permissions", "members")

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        members = cast("forms.ModelMultipleChoiceField[User]", self.fields["members"])
        members.queryset = User.objects.order_by("username")
        if self.instance.pk is not None:
            self.initial["members"] = self.instance.user_set.values_list("pk", flat=True)


def _actor(request: HttpRequest) -> User:
    if not isinstance(request.user, User):
        raise PermissionError("identity admin requires an Aegis user")
    return request.user


def _request_id(request: HttpRequest) -> str:
    value = getattr(request, "request_id", None)
    if not isinstance(value, str):
        raise PermissionError("identity admin requires request identity")
    return value


def _reject_inline_writes(
    formsets: Sequence[BaseInlineFormSet[Any, Any, Any]],
) -> None:
    if formsets:
        raise PermissionError("identity admin inline writes are disabled")


class CanonicalAuditOnlyAdminMixin:
    """Keep AuditEvent as the sole change log for audited identity admins."""

    def log_addition(self, request: HttpRequest, obj: Any, message: Any) -> Any:
        del request, obj, message
        return None

    def log_change(self, request: HttpRequest, obj: Any, message: Any) -> Any:
        del request, obj, message
        return None


@admin.register(User)
class AegisUserAdmin(CanonicalAuditOnlyAdminMixin, UserAdmin):  # type: ignore[type-arg]
    actions = ("activate_users", "deactivate_users")
    form = AegisUserChangeForm
    readonly_fields = (*tuple(UserAdmin.readonly_fields), "authorization_epoch")
    fieldsets = (
        *tuple(UserAdmin.fieldsets or ()),
        ("Aegis", {"fields": ("authorization_epoch",)}),
    )

    def get_urls(self) -> list[URLPattern]:
        return [
            path(
                "<path:object_id>/password/",
                self.admin_site.admin_view(self.password_change_disabled),
            ),
            *admin.ModelAdmin.get_urls(self),
        ]

    def password_change_disabled(
        self, request: HttpRequest, object_id: str
    ) -> NoReturn:
        del request, object_id
        raise Http404

    def has_delete_permission(self, request: HttpRequest, obj: User | None = None) -> bool:
        del request, obj
        return False

    def save_model(
        self, request: HttpRequest, obj: User, form: ModelForm[Any], change: bool
    ) -> None:
        del obj, change
        save_user_from_admin(actor=_actor(request), form=form, request_id=_request_id(request))

    def save_related(
        self,
        request: HttpRequest,
        form: ModelForm[Any],
        formsets: Sequence[BaseInlineFormSet[Any, Any, Any]],
        change: bool,
    ) -> None:
        del request, form, change
        _reject_inline_writes(formsets)

    @admin.action(description="Activate selected users")
    def activate_users(self, request: HttpRequest, queryset: Any) -> None:
        for user_id in queryset.values_list("pk", flat=True).iterator():
            set_user_active(
                actor=_actor(request),
                user_id=user_id,
                active=True,
                request_id=_request_id(request),
            )

    @admin.action(description="Deactivate selected users")
    def deactivate_users(self, request: HttpRequest, queryset: Any) -> None:
        for user_id in queryset.values_list("pk", flat=True).iterator():
            set_user_active(
                actor=_actor(request),
                user_id=user_id,
                active=False,
                request_id=_request_id(request),
            )


admin.site.unregister(Group)


@admin.register(Group)
class AegisGroupAdmin(CanonicalAuditOnlyAdminMixin, GroupAdmin):
    form = AegisGroupAdminForm
    actions = None
    fieldsets = ((None, {"fields": ("name", "permissions", "members")}),)
    filter_horizontal = ("permissions",)

    def has_delete_permission(self, request: HttpRequest, obj: Group | None = None) -> bool:
        del request, obj
        return False

    def save_model(
        self, request: HttpRequest, obj: Group, form: ModelForm[Any], change: bool
    ) -> None:
        del obj, change
        members = cast(Sequence[User], form.cleaned_data["members"])
        save_group_from_admin(
            actor=_actor(request),
            form=form,
            member_ids=[member.pk for member in members],
            request_id=_request_id(request),
        )

    def save_related(
        self,
        request: HttpRequest,
        form: ModelForm[Any],
        formsets: Sequence[BaseInlineFormSet[Any, Any, Any]],
        change: bool,
    ) -> None:
        del request, form, change
        _reject_inline_writes(formsets)
