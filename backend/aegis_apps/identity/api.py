from __future__ import annotations

from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.authentication import SessionAuthentication
from rest_framework.parsers import JSONParser
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from aegis_apps.audit.services import record_event

from .models import User
from .serializers import LoginSerializer
from .session_policy import cache_namespace, initialize_session

AUTHENTICATION_REQUIRED = {
    "type": "authentication_required",
    "title": "Authentication required",
}
INVALID_LOGIN_PROBLEM = {
    "type": "invalid_credentials",
    "title": "Unable to sign in",
}


def _request_id(request: Request) -> str:
    value = getattr(request, "request_id", None)
    if not isinstance(value, str):
        raise PermissionError("authentication requires request identity")
    return value


def _user_payload(user: User) -> dict[str, str]:
    return {"id": str(user.pk), "username": user.username}


def _response(data: Any = None, *, status: int = 200) -> Response:
    response = Response(data, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


class JSONAPIView(APIView):
    authentication_classes = (SessionAuthentication,)
    permission_classes = ()
    parser_classes = (JSONParser,)
    renderer_classes = (JSONRenderer,)


class CsrfView(JSONAPIView):
    def get(self, request: Request) -> Response:
        return _response({"csrfToken": get_token(request)})


@method_decorator(csrf_protect, name="dispatch")
class LoginView(JSONAPIView):
    def post(self, request: Request) -> Response:
        serializer = LoginSerializer(data=request.data)
        if not serializer.is_valid():
            return _response(
                {"type": "invalid_request", "title": "Invalid request"},
                status=400,
            )
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]
        user = authenticate(request=request, username=username, password=password)
        if not isinstance(user, User):
            record_event(
                event_type="auth.login.failed",
                outcome="failure",
                actor=None,
                request_id=_request_id(request),
                metadata={"bucket_type": "account", "request_id": _request_id(request)},
            )
            return _response(INVALID_LOGIN_PROBLEM, status=401)

        login(request, user)
        initialize_session(session=request.session, user=user, now=timezone.now())
        record_event(
            event_type="auth.login.succeeded",
            outcome="success",
            actor=user,
            request_id=_request_id(request),
        )
        return _response({"user": _user_payload(user)})


@method_decorator(csrf_protect, name="dispatch")
class LogoutView(JSONAPIView):
    def post(self, request: Request) -> Response:
        user = request.user
        if isinstance(user, User) and user.is_authenticated:
            record_event(
                event_type="auth.logout",
                outcome="success",
                actor=user,
                request_id=_request_id(request),
            )
        logout(request)
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
