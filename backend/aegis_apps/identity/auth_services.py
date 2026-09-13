from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth import authenticate, login, logout
from django.http import HttpRequest
from django.utils import timezone

from aegis_apps.audit.services import record_event

from .models import User
from .session_policy import initialize_session
from .throttling import FailureRecord, LoginThrottle, ThrottleUnavailable


@dataclass(frozen=True)
class LoginResult:
    status: int
    user: User | None = None
    retry_after_seconds: int = 0


def _request_id(request: HttpRequest) -> str:
    value = getattr(request, "request_id", None)
    if not isinstance(value, str):
        raise PermissionError("authentication requires request identity")
    return value


def _audit_failure(request: HttpRequest, failure: FailureRecord) -> None:
    if failure.audit_event_type is None or failure.audit_bucket_type is None:
        return
    request_id = _request_id(request)
    record_event(
        event_type=failure.audit_event_type,
        outcome="denied" if failure.audit_event_type == "auth.login.throttled" else "failure",
        actor=None,
        request_id=request_id,
        metadata={"bucket_type": failure.audit_bucket_type, "request_id": request_id},
    )


def sign_in(
    request: HttpRequest,
    *,
    username: str,
    password: str,
    require_staff: bool = False,
) -> LoginResult:
    """Apply shared admission, session policy, and audit in the throttle transaction."""
    client_ip = request.META.get("REMOTE_ADDR", "")
    try:
        with LoginThrottle().admission(
            username=username,
            client_ip=client_ip if isinstance(client_ip, str) else "",
        ) as admission:
            if not admission.decision.allowed:
                return LoginResult(429, retry_after_seconds=admission.decision.retry_after_seconds)
            user = authenticate(request=request, username=username, password=password)
            if not isinstance(user, User) or (require_staff and not user.is_staff):
                _audit_failure(request, admission.record_failure())
                return LoginResult(401)
            admission.record_success()
            login(request, user)
            request.session.cycle_key()
            initialize_session(session=request.session, user=user, now=timezone.now())
            record_event(
                event_type="auth.login.succeeded",
                outcome="success",
                actor=user,
                request_id=_request_id(request),
            )
            return LoginResult(200, user=user)
    except ThrottleUnavailable:
        return LoginResult(503)


def sign_out(request: HttpRequest) -> None:
    user = request.user
    actor = user if isinstance(user, User) and user.is_authenticated else None
    request_id = _request_id(request) if actor is not None else None
    try:
        if actor is not None and request_id is not None:
            record_event(
                event_type="auth.logout",
                outcome="success",
                actor=actor,
                request_id=request_id,
            )
    finally:
        logout(request)
