from __future__ import annotations

from typing import Any

from django.contrib.auth import authenticate, login, logout
from django.db import transaction
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.authentication import SessionAuthentication
from rest_framework.exceptions import ParseError
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from aegis_apps.audit.services import record_event

from .models import User
from .serializers import BoundedJSONParser, LoginSerializer
from .session_policy import cache_namespace, initialize_session
from .throttling import FailureRecord, LoginThrottle

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


def _request_id(request: Request) -> str:
    value = getattr(request, "request_id", None)
    if not isinstance(value, str):
        raise PermissionError("authentication requires request identity")
    return value


def _user_payload(user: User) -> dict[str, str]:
    return {"id": str(user.pk), "username": user.username}


def _client_ip(request: Request) -> str:
    value = request.META.get("REMOTE_ADDR", "")
    return value if isinstance(value, str) else ""


def _audit_failure(request: Request, failure: FailureRecord) -> None:
    if failure.audit_event_type is None or failure.audit_bucket_type is None:
        return
    request_id = _request_id(request)
    record_event(
        event_type=failure.audit_event_type,
        outcome="denied" if failure.audit_event_type == "auth.login.throttled" else "failure",
        actor=None,
        request_id=request_id,
        metadata={
            "bucket_type": failure.audit_bucket_type,
            "request_id": request_id,
        },
    )


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
        throttle = LoginThrottle()
        try:
            decision = throttle.check(
                username=username,
                client_ip=_client_ip(request),
            )
        except RuntimeError:
            return _response(AUTHENTICATION_UNAVAILABLE, status=503)
        if not decision.allowed:
            response = _response(LOGIN_THROTTLED_PROBLEM, status=429)
            response["Retry-After"] = str(decision.retry_after_seconds)
            return response

        user = authenticate(request=request, username=username, password=password)
        if not isinstance(user, User):
            with transaction.atomic():
                failure = throttle.record_failure(
                    username=username,
                    client_ip=_client_ip(request),
                )
                _audit_failure(request, failure)
            return _response(INVALID_LOGIN_PROBLEM, status=401)

        with transaction.atomic():
            throttle.record_success(username=username)
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
