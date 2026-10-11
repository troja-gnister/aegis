"""Media heartbeat metrics: Python validation and the database function agree (actual login)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

COUNTERS = (
    "mediaInteractiveQueued", "mediaBackgroundPending", "mediaInFlight", "mediaAssets",
    "mediaAssetsComplete", "mediaProcessorFailures",
)


def _parity(role_database: Any, metrics: dict[str, object], *, allowed: bool) -> None:
    from aegis_apps.operations.serializers import validate_heartbeat_metrics

    from tests.deployment.test_database_roles import (
        _heartbeat_parameters,
        _heartbeat_statement,
        _publish_heartbeat,
    )

    if allowed:
        assert validate_heartbeat_metrics(metrics) == metrics
    else:
        with pytest.raises(ValueError):
            validate_heartbeat_metrics(metrics)
    with role_database.connect("aegis_media") as caller:
        if allowed:
            heartbeat = _publish_heartbeat(
                caller, function_role="media", parameters=_heartbeat_parameters(metrics=metrics),
            )
            stored = caller.execute(
                "SELECT metrics FROM public.operations_workerheartbeat WHERE id=%s",
                [heartbeat],
            ).fetchone()
            assert stored == (metrics,)
        else:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                caller.execute(
                    _heartbeat_statement("media"), _heartbeat_parameters(metrics=metrics),
                )


@pytest.mark.parametrize("key", COUNTERS)
@pytest.mark.parametrize("value", [0, 9223372036854775807, -1, 2**63, 1.5, True, "1", None])
def test_media_counter_parity(role_database: Any, key: str, value: object) -> None:
    allowed = type(value) is int and 0 <= value <= 9223372036854775807
    _parity(role_database, {key: value}, allowed=allowed)


@pytest.mark.parametrize(
    ("value", "allowed"),
    [
        ("decode_failed", True),
        ("processor_unavailable", True),
        ("photo.jpg", False),
        ("/srv/aegis/roots/x", False),
        ("Decode", False),
        ("", False),
        ("x" * 33, False),
        (1, False),
        (None, False),
    ],
)
def test_media_last_error_code_parity(role_database: Any, value: object, allowed: bool) -> None:
    _parity(role_database, {"mediaLastErrorCode": value}, allowed=allowed)


def test_a_full_media_heartbeat_is_stored(role_database: Any) -> None:
    _parity(role_database, {
        "queueAgeSeconds": 3, "scanProgress": 0.25, "mediaInteractiveQueued": 1,
        "mediaBackgroundPending": 7, "mediaInFlight": 3, "mediaAssets": 12,
        "mediaAssetsComplete": 5, "mediaProcessorFailures": 1, "mediaLastErrorCode": "timeout",
    }, allowed=True)
