from __future__ import annotations

from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import ParseError
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from .auth_services import sign_in, sign_out
from .models import User
from .serializers import BoundedJSONParser, LoginSerializer
from .session_policy import cache_namespace

AUTHENTICATION_REQUIRED = {
    "type": "authentication_required",
    "title": "Authentication required",
}
INVALID_LOGIN_PROBLEM = {
    "type": "invalid_credentials",
    "title": "Unable to sign in",
}
LOGIN_THROTTLED_PROBLEM = {
    "type": "login_throttled",
    "title": "Unable to sign in",
}
AUTHENTICATION_UNAVAILABLE = {
    "type": "authentication_unavailable",
    "title": "Unable to sign in",
}
INVALID_REQUEST_PROBLEM = {"type": "invalid_request", "title": "Invalid request"}
AUTH_AUDIT_EVENTS = frozenset(
    {
        "auth.login.succeeded",
        "auth.login.failed",
        "auth.login.throttled",
        "auth.logout",
        "auth.session.revoked",
    }
)


def _user_payload(user: User) -> dict[str, str]:
    return {"id": str(user.pk), "username": user.username}


def _response(data: Any = None, *, status: int = 200) -> Response:
    response = Response(data, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


class JSONAPIView(APIView):
    authentication_classes = (SessionAuthentication,)
    permission_classes = ()
    parser_classes = (BoundedJSONParser,)
    renderer_classes = (JSONRenderer,)

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, ParseError):
            return _response(INVALID_REQUEST_PROBLEM, status=400)
        return super().handle_exception(exc)


class CsrfView(JSONAPIView):
    def get(self, request: Request) -> Response:
        return _response({"csrfToken": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class LoginView(JSONAPIView):
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return _response(INVALID_REQUEST_PROBLEM, status=400)
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]
        result = sign_in(request, username=username, password=password)
        if result.user is not None:
            return _response({"user": _user_payload(result.user)})
        problem = {
            401: INVALID_LOGIN_PROBLEM,
            429: LOGIN_THROTTLED_PROBLEM,
            503: AUTHENTICATION_UNAVAILABLE,
        }[result.status]
        response = _response(problem, status=result.status)
        if result.status == 429:
            response["Retry-After"] = str(result.retry_after_seconds)
        return response


@method_decorator(csrf_protect, name="dispatch")
class LogoutView(JSONAPIView):
    def post(self, request: Request) -> Response:
        sign_out(request)
        return _response(status=204)


class SessionView(JSONAPIView):
    def get(self, request: Request) -> Response:
        user = request.user
        if not isinstance(user, User) or not user.is_authenticated:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        return _response(
            {
                "user": _user_payload(user),
                "cacheNamespace": cache_namespace(session=request.session, user=user),
            }
        )


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    del reason
    if request.path.startswith("/api/"):
        response = JsonResponse(
            {"type": "csrf_failed", "title": "Request verification failed"},
            status=403,
        )
        response["Cache-Control"] = "private, no-store"
        return response
    return HttpResponse(
        "Request verification failed",
        status=403,
        content_type="text/plain; charset=utf-8",
    )
