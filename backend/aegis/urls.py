from aegis_apps.common.views import live
from django.urls import path

urlpatterns = [
    path("health/live", live, name="health-live"),
]
