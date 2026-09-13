from __future__ import annotations

from typing import Any, NoReturn, cast

from django import forms
from django.contrib.admin import AdminSite
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.views import LoginView, LogoutView
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache

from .auth_services import LoginResult, sign_in, sign_out
from .serializers import LoginSerializer


class AegisAdminAuthenticationForm(AuthenticationForm):
    result: LoginResult | None = None

    def clean(self) -> dict[str, Any]:
        serializer = LoginSerializer(
            data={
                "username": self.cleaned_data.get("username"),
                "password": self.cleaned_data.get("password"),
            }
        )
        if self.request is None or not serializer.is_valid():
            raise forms.ValidationError("Unable to sign in", code="invalid_login")
        self.result = sign_in(self.request, require_staff=True, **serializer.validated_data)
        if self.result.user is None:
            raise forms.ValidationError("Unable to sign in", code="invalid_login")
        self.user_cache = self.result.user
        return self.cleaned_data


class AegisAdminLoginView(LoginView):
    authentication_form = AegisAdminAuthenticationForm

    def form_valid(self, form: Any) -> HttpResponse:
        # The shared service already established and audited the session inside
        # the admission transaction. LoginView supplies safe next-URL handling.
        return HttpResponseRedirect(self.get_success_url())

    def form_invalid(self, form: Any) -> HttpResponse:
        response = super().form_invalid(form)
        result = form.result
        if result is not None and result.status in (429, 503):
            response.status_code = result.status
            if result.status == 429:
                response["Retry-After"] = str(result.retry_after_seconds)
        return response


class AegisAdminLogoutView(LogoutView):
    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        sign_out(request)
        redirect_to = self.get_success_url()
        if redirect_to != request.get_full_path():
            return HttpResponseRedirect(redirect_to)
        return self.render_to_response(self.get_context_data(**kwargs))


class AegisAdminSite(AdminSite):
    @method_decorator(never_cache)
    def login(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        index = reverse("admin:index", current_app=self.name)
        if request.method == "GET" and self.has_permission(request):
            return HttpResponseRedirect(index)
        context = {
            **self.each_context(request),
            "title": "Log in",
            "subtitle": None,
            "app_path": request.get_full_path(),
            "username": request.user.get_username(),
            **(extra_context or {}),
        }
        request.current_app = self.name
        response = AegisAdminLoginView.as_view(
            extra_context=context,
            template_name=self.login_template or "admin/login.html",
            next_page=index,
        )(request)
        return cast(HttpResponse, response)

    def logout(  # type: ignore[override]  # Django also returns redirects here.
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> HttpResponse:
        request.current_app = self.name
        response = AegisAdminLogoutView.as_view(
            extra_context={
                **self.each_context(request),
                "has_permission": False,
                **(extra_context or {}),
            },
            template_name=self.logout_template or "registration/logged_out.html",
        )(request)
        return cast(HttpResponse, response)

    def password_change(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> NoReturn:
        raise Http404

    def password_change_done(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> NoReturn:
        raise Http404
