from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from aegis_apps.common.health import readiness


@require_GET
def live(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def ready(_request: HttpRequest) -> JsonResponse:
    is_ready, checks = readiness()
    status = "ok" if is_ready else "unavailable"
    return JsonResponse({"status": status, "checks": checks}, status=200 if is_ready else 503)
