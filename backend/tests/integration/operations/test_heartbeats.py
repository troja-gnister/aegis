from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from aegis_apps.operations.heartbeats import publish_heartbeat
from aegis_apps.operations.models import WorkerHeartbeat
from aegis_apps.operations.selectors import worker_role_states
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

RELEASE = "release-1"
SCHEMA = "sha256:" + "a" * 64
MANIFEST = "b" * 64


def _publish(
    *,
    role: str = "operations",
    worker_id: str | None = None,
    status: str = "idle",
    metrics: object = None,
    release_id: str = RELEASE,
    schema_identity: str = SCHEMA,
    manifest_identity: str = MANIFEST,
    now: datetime | None = None,
) -> WorkerHeartbeat:
    heartbeat_time = timezone.now() if now is None else now
    return publish_heartbeat(
        role=role,
        worker_id=worker_id or str(uuid.uuid4()),
        release_id=release_id,
        schema_identity=schema_identity,
        manifest_identity=manifest_identity,
        status=status,
        metrics={} if metrics is None else metrics,
        current_job_id=None,
        now=heartbeat_time,
    )


def test_heartbeat_upsert_is_safe_and_does_not_derive_a_hostname() -> None:
    worker_id = str(uuid.uuid4())
    now = timezone.now()
    created = _publish(
        worker_id=worker_id,
        metrics={
            "queueAgeSeconds": 0,
            "scanProgress": 0.25,
            "diskPressure": 0.5,
            "diskCapacityBytes": 9_223_372_036_854_775_807,
        },
        now=now,
    )
    updated = _publish(
        worker_id=worker_id,
        status="running",
        metrics={"scanProgress": 0.75},
        now=now + timedelta(seconds=1),
    )

    assert updated.id == created.id
    assert WorkerHeartbeat.objects.count() == 1
    assert updated.worker_id == worker_id
    assert updated.status == "running"
    assert updated.metrics == {"scanProgress": 0.75}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("role", "unknown"),
        ("worker_id", "hostname-or-container-name"),
        ("release_id", "/sensitive/release"),
        ("schema_identity", ""),
        ("manifest_identity", "not-a-digest"),
        ("status", "unknown"),
        ("metrics", {"unknown": 1}),
        ("metrics", {"queueAgeSeconds": True}),
        ("metrics", {"queueAgeSeconds": "1"}),
        ("metrics", {"queueAgeSeconds": float("nan")}),
        ("metrics", {"scanProgress": 1.1}),
        ("metrics", {"diskPressure": -0.1}),
        ("metrics", {"diskCapacityBytes": 9_223_372_036_854_775_808}),
        ("metrics", {"scanProgress": {"nested": 1}}),
        ("now", timezone.now().replace(tzinfo=None)),
    ],
)
def test_heartbeat_rejects_unknown_unbounded_or_nested_values(field: str, value: object) -> None:
    kwargs = {field: value}
    with pytest.raises(ValueError):
        _publish(**kwargs)  # type: ignore[arg-type]
    assert WorkerHeartbeat.objects.count() == 0


def test_worker_role_states_require_fresh_nonstopping_exact_identities() -> None:
    now = timezone.now()
    _publish(role="operations", now=now)
    _publish(role="indexer", release_id="old-release", now=now)
    _publish(role="media", status="stopping", now=now)

    states = worker_role_states(
        roles=("operations", "indexer", "media"),
        release_id=RELEASE,
        schema_identity=SCHEMA,
        manifest_identity=MANIFEST,
        now=now + timedelta(seconds=45),
        freshness_seconds=45,
    )

    assert states == {
        "operations": "healthy",
        "indexer": "stale",
        "media": "stale",
    }


def test_worker_role_states_distinguish_missing_from_expired() -> None:
    now = timezone.now()
    _publish(role="operations", now=now - timedelta(seconds=46))

    states = worker_role_states(
        roles=("operations", "indexer"),
        release_id=RELEASE,
        schema_identity=SCHEMA,
        manifest_identity=MANIFEST,
        now=now,
        freshness_seconds=45,
    )

    assert states == {"operations": "stale", "indexer": "missing"}
