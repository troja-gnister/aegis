from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.backends.base import SessionBase
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.dateparse import parse_datetime

from aegis_apps.audit.services import record_event

from .models import User

AUTH_STARTED_AT = "auth_started_at"
LAST_SEEN_AT = "last_seen_at"
AUTHORIZATION_EPOCH = "authorization_epoch"


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = parse_datetime(value)
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def session_is_current(*, session: SessionBase, user: User, now: datetime) -> bool:
    if now.tzinfo is None or now.utcoffset() is None or not user.is_active:
        return False
    started_at = _aware_datetime(session.get(AUTH_STARTED_AT))
    last_seen_at = _aware_datetime(session.get(LAST_SEEN_AT))
    if started_at is None or last_seen_at is None:
        return False
    try:
        session_epoch = int(session[AUTHORIZATION_EPOCH])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    absolute_age = now - started_at
    idle_age = now - last_seen_at
    return (
        session_epoch == user.authorization_epoch
        and absolute_age >= timedelta(0)
        and idle_age >= timedelta(0)
        and absolute_age <= settings.AEGIS_SESSION_ABSOLUTE_AGE
        and idle_age <= settings.AEGIS_SESSION_IDLE_AGE
    )


def initialize_session(*, session: SessionBase, user: User, now: datetime) -> None:
    timestamp = now.astimezone(UTC).isoformat()
    session[AUTH_STARTED_AT] = timestamp
    session[LAST_SEEN_AT] = timestamp
    session[AUTHORIZATION_EPOCH] = user.authorization_epoch


def initialize_logged_in_session(
    sender: type[object],
    request: HttpRequest,
    user: object,
    **_kwargs: object,
) -> None:
    del sender
    if not isinstance(user, User):
        raise TypeError("login requires an Aegis user")
    initialize_session(session=request.session, user=user, now=timezone.now())


def cache_namespace(*, session: SessionBase, user: User) -> str:
    session_key = session.session_key
    if not isinstance(session_key, str) or not session_key:
        raise ValueError("authenticated session has no key")
    value = f"{session_key}:{user.pk}:{user.authorization_epoch}"
    return salted_hmac("aegis.auth.cache-namespace", value).hexdigest()


class SessionPolicyMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            now = timezone.now()
            user = request.user
            current = isinstance(user, User) and session_is_current(
                session=request.session,
                user=user,
                now=now,
            )
            if not current:
                request.session.flush()
                request.user = AnonymousUser()
                request_id = getattr(request, "request_id", "")
                record_event(
                    event_type="auth.session.revoked",
                    outcome="denied",
                    actor=None,
                    request_id=request_id,
                    metadata={"request_id": request_id},
                )
            elif isinstance(user, User):
                last_seen_at = _aware_datetime(request.session.get(LAST_SEEN_AT))
                if (
                    last_seen_at is not None
                    and now - last_seen_at >= settings.AEGIS_SESSION_ACTIVITY_WRITE_INTERVAL
                ):
                    request.session[LAST_SEEN_AT] = now.astimezone(UTC).isoformat()
        elif SESSION_KEY in request.session:
            request.session.flush()
            request_id = getattr(request, "request_id", "")
            record_event(
                event_type="auth.session.revoked",
                outcome="denied",
                actor=None,
                request_id=request_id,
                metadata={"request_id": request_id},
            )
        return self.get_response(request)
