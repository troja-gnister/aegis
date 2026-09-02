from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.sessions.backends.base import SessionBase
from django.utils.dateparse import parse_datetime

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
