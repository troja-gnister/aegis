from aegis_apps.common.views import live, ready
from aegis_apps.identity.api import CsrfView, LoginView, LogoutView, SessionView
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/live", live, name="health-live"),
    path("health/ready", ready, name="health-ready"),
    path("api/v1/auth/csrf", CsrfView.as_view(), name="auth-csrf"),
    path("api/v1/auth/login", LoginView.as_view(), name="auth-login"),
    path("api/v1/auth/logout", LogoutView.as_view(), name="auth-logout"),
    path("api/v1/auth/session", SessionView.as_view(), name="auth-session"),
]
