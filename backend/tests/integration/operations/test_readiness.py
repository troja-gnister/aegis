from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from aegis_apps.common.health import readiness
from aegis_apps.operations.heartbeats import publish_heartbeat_for_test
from aegis_apps.operations.models import UNCONFIGURED_MANIFEST_IDENTITY
from aegis_apps.roots.manifest import ManifestError
from django.test import override_settings
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
RELEASE = "readiness-release"
SCHEMA = "sha256:" + "e" * 64


@pytest.fixture(autouse=True)
def _enable_test_heartbeat_time_boundary(settings: object) -> None:
    settings.AEGIS_ENVIRONMENT = "test"  # type: ignore[attr-defined]


def _heartbeat(*, role: str, status: str = "idle", age: int = 0) -> None:
    publish_heartbeat_for_test(
        role=role,
        worker_id=str(uuid.uuid4()),
        release_id=RELEASE,
        schema_identity=SCHEMA,
        manifest_identity=UNCONFIGURED_MANIFEST_IDENTITY,
        status=status,
        metrics={},
        observed_at=timezone.now() - timedelta(seconds=age),
    )


@override_settings(
    AEGIS_RELEASE_ID=RELEASE,
    AEGIS_REQUIRED_WORKER_ROLES=("operations", "indexer", "media"),
    AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS=45,
)
def test_readiness_accepts_unconfigured_manifest_only_with_every_fresh_role() -> None:
    for role in ("operations", "indexer", "media"):
        _heartbeat(role=role)

    with (
        patch("aegis_apps.common.health.database_status", return_value=(True, "ok")),
        patch("aegis_apps.operations.selectors.current_schema_identity", return_value=SCHEMA),
    ):
        ready, checks = readiness()

    assert ready is True
    assert checks == {
        "database": "ok",
        "operations": "healthy",
        "indexer": "healthy",
        "media": "healthy",
    }


@override_settings(
    AEGIS_RELEASE_ID=RELEASE,
    AEGIS_REQUIRED_WORKER_ROLES=("operations", "indexer", "media"),
    AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS=45,
)
def test_readiness_reports_only_safe_stale_and_missing_role_states() -> None:
    _heartbeat(role="operations", age=46)
    _heartbeat(role="media", status="stopping")

    with (
        patch("aegis_apps.common.health.database_status", return_value=(True, "ok")),
        patch("aegis_apps.operations.selectors.current_schema_identity", return_value=SCHEMA),
    ):
        ready, checks = readiness()

    assert ready is False
    assert checks == {
        "database": "ok",
        "operations": "stale",
        "indexer": "missing",
        "media": "stale",
    }


@override_settings(
    AEGIS_RELEASE_ID=RELEASE,
    AEGIS_REQUIRED_WORKER_ROLES=("operations",),
    AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS=45,
)
def test_readiness_rejects_a_fresh_mixed_release_even_with_a_matching_worker() -> None:
    _heartbeat(role="operations")
    publish_heartbeat_for_test(
        role="operations",
        worker_id=str(uuid.uuid4()),
        release_id="another-release",
        schema_identity=SCHEMA,
        manifest_identity=UNCONFIGURED_MANIFEST_IDENTITY,
        status="idle",
        metrics={},
        observed_at=timezone.now(),
    )

    with (
        patch("aegis_apps.common.health.database_status", return_value=(True, "ok")),
        patch("aegis_apps.operations.selectors.current_schema_identity", return_value=SCHEMA),
    ):
        ready, checks = readiness()

    assert ready is False
    assert checks == {"database": "ok", "operations": "stale"}


@override_settings(
    AEGIS_RELEASE_ID=RELEASE,
    AEGIS_REQUIRED_WORKER_ROLES=("operations", "indexer", "media"),
)
def test_readiness_fails_closed_on_invalid_manifest_without_disclosing_it() -> None:
    sentinel = "/private/mount/manifest"
    with (
        patch("aegis_apps.common.health.database_status", return_value=(True, "ok")),
        patch("aegis_apps.operations.selectors.current_schema_identity", return_value=SCHEMA),
        patch(
            "aegis_apps.operations.selectors.current_manifest_identity",
            side_effect=ManifestError(sentinel),
        ),
    ):
        ready, checks = readiness()

    assert ready is False
    assert checks == {
        "database": "ok",
        "operations": "stale",
        "indexer": "stale",
        "media": "stale",
    }
    assert sentinel not in str(checks)


def test_readiness_database_failure_short_circuits_worker_checks() -> None:
    worker_readiness = Mock(side_effect=AssertionError("worker check must not run"))
    with (
        patch(
            "aegis_apps.common.health.database_status",
            return_value=(False, "database unavailable"),
        ),
        patch("aegis_apps.common.health.worker_readiness", worker_readiness),
    ):
        ready, checks = readiness()

    assert ready is False
    assert checks == {"database": "database unavailable"}
    worker_readiness.assert_not_called()
