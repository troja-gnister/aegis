from aegis_apps.common.views import live, ready
from django.urls import path

urlpatterns = [
    path("health/live", live, name="health-live"),
    path("health/ready", ready, name="health-ready"),
]
