from aegis_apps.common.views import live, proxy_attestation, ready
from aegis_apps.identity.api import CsrfView, LoginView, LogoutView, SessionView
from aegis_apps.roots.api import RootListView
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/live", live, name="health-live"),
    path(
        "health/proxy-attestation",
        proxy_attestation,
        name="health-proxy-attestation",
    ),
    path("health/ready", ready, name="health-ready"),
    path("api/v1/auth/csrf", CsrfView.as_view(), name="auth-csrf"),
    path("api/v1/auth/login", LoginView.as_view(), name="auth-login"),
    path("api/v1/auth/logout", LogoutView.as_view(), name="auth-logout"),
    path("api/v1/auth/session", SessionView.as_view(), name="auth-session"),
    path("api/v1/roots", RootListView.as_view(), name="root-list"),
]
