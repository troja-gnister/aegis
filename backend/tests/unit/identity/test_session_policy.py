from datetime import UTC, datetime, timedelta

import pytest
from aegis_apps.identity.models import User
from aegis_apps.identity.session_policy import (
    AUTH_STARTED_AT,
    AUTHORIZATION_EPOCH,
    LAST_SEEN_AT,
    session_is_current,
)
from django.contrib.sessions.backends.signed_cookies import SessionStore
from django.test import override_settings

NOW = datetime(2026, 9, 1, 12, tzinfo=UTC)


def _session(**overrides: object) -> SessionStore:
    session = SessionStore()
    session[AUTH_STARTED_AT] = (NOW - timedelta(hours=1)).isoformat()
    session[LAST_SEEN_AT] = (NOW - timedelta(minutes=5)).isoformat()
    session[AUTHORIZATION_EPOCH] = 7
    session.update(overrides)
    return session


@override_settings(
    AEGIS_SESSION_IDLE_AGE=timedelta(minutes=30),
    AEGIS_SESSION_ABSOLUTE_AGE=timedelta(hours=12),
)
def test_session_policy_accepts_current_aware_bounded_session() -> None:
    user = User(is_active=True, authorization_epoch=7)

    assert session_is_current(session=_session(), user=user, now=NOW) is True


@override_settings(
    AEGIS_SESSION_IDLE_AGE=timedelta(minutes=30),
    AEGIS_SESSION_ABSOLUTE_AGE=timedelta(hours=12),
)
@pytest.mark.parametrize(
    "session,user",
    [
        (
            _session(auth_started_at=NOW.isoformat(), last_seen_at=None),
            User(is_active=True, authorization_epoch=7),
        ),
        (_session(auth_started_at="not-a-time"), User(is_active=True, authorization_epoch=7)),
        (
            _session(auth_started_at=NOW.replace(tzinfo=None).isoformat()),
            User(is_active=True, authorization_epoch=7),
        ),
        (
            _session(auth_started_at=(NOW - timedelta(hours=12, microseconds=1)).isoformat()),
            User(is_active=True, authorization_epoch=7),
        ),
        (
            _session(last_seen_at=(NOW - timedelta(minutes=30, microseconds=1)).isoformat()),
            User(is_active=True, authorization_epoch=7),
        ),
        (_session(), User(is_active=False, authorization_epoch=7)),
        (_session(), User(is_active=True, authorization_epoch=8)),
    ],
)
def test_session_policy_rejects_malformed_expired_inactive_or_stale_sessions(
    session: SessionStore, user: User
) -> None:
    assert session_is_current(session=session, user=user, now=NOW) is False


@override_settings(
    AEGIS_SESSION_IDLE_AGE=timedelta(minutes=30),
    AEGIS_SESSION_ABSOLUTE_AGE=timedelta(hours=12),
)
def test_session_policy_includes_exact_idle_and_absolute_boundaries() -> None:
    user = User(is_active=True, authorization_epoch=7)
    session = _session(
        auth_started_at=(NOW - timedelta(hours=12)).isoformat(),
        last_seen_at=(NOW - timedelta(minutes=30)).isoformat(),
    )

    assert session_is_current(session=session, user=user, now=NOW) is True
