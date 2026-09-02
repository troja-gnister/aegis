from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from aegis_apps.common.health import readiness

PROXY_ATTESTATION_HEADER = "startup-v1"
PROXY_ATTESTATION_REMOTE_ADDR = "192.0.2.254"


@require_GET
def live(_request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


@require_GET
def proxy_attestation(request: HttpRequest) -> HttpResponse:
    valid = (
        request.headers.get("X-Aegis-Proxy-Attestation") == PROXY_ATTESTATION_HEADER
        and request.META.get("REMOTE_ADDR") == PROXY_ATTESTATION_REMOTE_ADDR
    )
    response = HttpResponse(status=204 if valid else 404)
    response["Cache-Control"] = "private, no-store"
    return response


@require_GET
def ready(_request: HttpRequest) -> JsonResponse:
    is_ready, checks = readiness()
    status = "ok" if is_ready else "unavailable"
    return JsonResponse({"status": status, "checks": checks}, status=200 if is_ready else 503)
