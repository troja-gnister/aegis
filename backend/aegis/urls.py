from aegis_apps.common.views import live, ready
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("health/live", live, name="health-live"),
    path("health/ready", ready, name="health-ready"),
]
