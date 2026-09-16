from aegis_apps.catalog.api import (
    DirectoryListView,
    EntryDetailView,
    IndexStatusView,
    RootScanView,
)
from aegis_apps.common.views import live, proxy_attestation, ready
from aegis_apps.identity.api import CsrfView, LoginView, LogoutView, SessionView
from aegis_apps.operations.api import OperationsStatusView
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
    path(
        "api/v1/roots/<uuid:root_id>/entries",
        DirectoryListView.as_view(),
        name="entry-list",
    ),
    path(
        "api/v1/entries/<uuid:entry_id>",
        EntryDetailView.as_view(),
        name="entry-detail",
    ),
    path(
        "api/v1/roots/<uuid:root_id>/index-status",
        IndexStatusView.as_view(),
        name="index-status",
    ),
    path(
        "api/v1/roots/<uuid:root_id>/scans",
        RootScanView.as_view(),
        name="root-scan",
    ),
    path(
        "api/v1/admin/operations/status",
        OperationsStatusView.as_view(),
        name="operations-status",
    ),
]
