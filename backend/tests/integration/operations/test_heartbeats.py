from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
from unittest.mock import patch

import pytest
from aegis_apps.operations import heartbeats as heartbeat_service
from aegis_apps.operations.heartbeats import publish_heartbeat
from aegis_apps.operations.models import WorkerHeartbeat
from aegis_apps.operations.selectors import operations_status, worker_role_states
from django.db import close_old_connections, connection, transaction
from django.test import override_settings
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

RELEASE = "release-1"
SCHEMA = "sha256:" + "a" * 64
MANIFEST = "b" * 64


@pytest.fixture(autouse=True)
def _enable_test_heartbeat_time_boundary(settings: object) -> None:
    settings.AEGIS_ENVIRONMENT = "test"  # type: ignore[attr-defined]


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
    return heartbeat_service.publish_heartbeat_for_test(
        role=role,
        worker_id=worker_id or str(uuid.uuid4()),
        release_id=release_id,
        schema_identity=schema_identity,
        manifest_identity=manifest_identity,
        status=status,
        metrics={} if metrics is None else metrics,
        current_job_id=None,
        observed_at=heartbeat_time,
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


def test_production_heartbeat_uses_authoritative_database_time() -> None:
    worker_id = str(uuid.uuid4())
    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        lower_bound = cursor.fetchone()[0]
    fake_application_time = lower_bound + timedelta(days=30)

    with patch("aegis_apps.operations.heartbeats.timezone.now", return_value=fake_application_time):
        heartbeat = publish_heartbeat(
            role="operations",
            worker_id=worker_id,
            release_id=RELEASE,
            schema_identity=SCHEMA,
            manifest_identity=MANIFEST,
        )

    with connection.cursor() as cursor:
        cursor.execute("SELECT clock_timestamp()")
        upper_bound = cursor.fetchone()[0]
    assert lower_bound <= heartbeat.last_seen_at <= upper_bound
    assert heartbeat.last_seen_at != fake_application_time


def test_older_publication_committing_after_newer_cannot_replace_stopping_state() -> None:
    worker_id = str(uuid.uuid4())
    observed_at = timezone.now()
    _publish(worker_id=worker_id, now=observed_at)
    older_transaction_started = Event()
    newer_committed = Event()

    def publish_older_last() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                older_transaction_started.set()
                assert newer_committed.wait(timeout=5)
                _publish(
                    worker_id=worker_id,
                    status="idle",
                    metrics={"scanProgress": 0.1},
                    now=observed_at + timedelta(seconds=1),
                )
        finally:
            close_old_connections()

    def publish_newer_first() -> None:
        close_old_connections()
        try:
            assert older_transaction_started.wait(timeout=5)
            _publish(
                worker_id=worker_id,
                status="stopping",
                metrics={"scanProgress": 0.9},
                now=observed_at + timedelta(seconds=2),
            )
            newer_committed.set()
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        older = executor.submit(publish_older_last)
        newer = executor.submit(publish_newer_first)
        newer.result(timeout=10)
        older.result(timeout=10)

    heartbeat = WorkerHeartbeat.objects.get(role="operations", worker_id=worker_id)
    assert heartbeat.last_seen_at == observed_at + timedelta(seconds=2)
    assert heartbeat.status == "stopping"
    assert heartbeat.metrics == {"scanProgress": 0.9}


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


def test_future_heartbeat_cannot_attest_readiness_or_status_metrics() -> None:
    status_time = timezone.now()
    _publish(
        role="operations",
        metrics={"scanProgress": 0.75, "diskPressure": 0.85},
        now=status_time + timedelta(hours=1),
    )

    states = worker_role_states(
        roles=("operations",),
        release_id=RELEASE,
        schema_identity=SCHEMA,
        manifest_identity=MANIFEST,
        now=status_time,
        freshness_seconds=45,
    )
    with (
        override_settings(
            AEGIS_RELEASE_ID=RELEASE,
            AEGIS_REQUIRED_WORKER_ROLES=("operations",),
            AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS=45,
        ),
        patch(
            "aegis_apps.operations.selectors.current_schema_identity",
            return_value=SCHEMA,
        ),
        patch(
            "aegis_apps.operations.selectors.current_manifest_identity",
            return_value=MANIFEST,
        ),
        patch(
            "aegis_apps.operations.selectors.authoritative_database_time",
            return_value=status_time,
            create=True,
        ),
    ):
        status = operations_status()

    assert states == {"operations": "stale"}
    assert status[0]["state"] == "stale"
    assert status[0]["scanProgress"] is None
    assert status[0]["diskPressure"] == "unknown"


def test_worker_role_state_query_is_existence_oriented_not_history_counting() -> None:
    now = timezone.now()
    _publish(role="operations", now=now)
    statements: list[str] = []

    def capture(
        execute: object,
        sql: str,
        params: object,
        many: bool,
        context: object,
    ) -> object:
        statements.append(" ".join(sql.upper().split()))
        return execute(sql, params, many, context)  # type: ignore[operator]

    with connection.execute_wrapper(capture):
        states = worker_role_states(
            roles=("operations", "indexer"),
            release_id=RELEASE,
            schema_identity=SCHEMA,
            manifest_identity=MANIFEST,
            now=now,
            freshness_seconds=45,
        )

    assert states == {"operations": "healthy", "indexer": "missing"}
    assert not any("COUNT(" in statement for statement in statements)


@override_settings(
    AEGIS_ENVIRONMENT="test",
    AEGIS_WORKER_HEARTBEAT_RETENTION_SECONDS=3600,
    AEGIS_WORKER_HEARTBEAT_SLOTS_PER_ROLE=4,
)
def test_stale_heartbeat_slots_are_reused_within_role_and_indexed() -> None:
    status_time = timezone.now()
    cutoff = status_time - timedelta(hours=1)

    def row(*, last_seen_at: datetime) -> WorkerHeartbeat:
        return WorkerHeartbeat(
            role="operations",
            worker_id=str(uuid.uuid4()),
            release_id=RELEASE,
            schema_identity=SCHEMA,
            manifest_identity=MANIFEST,
            last_seen_at=last_seen_at,
            status="idle",
            metrics={},
        )

    oldest = row(last_seen_at=cutoff - timedelta(seconds=2))
    old = row(last_seen_at=cutoff - timedelta(seconds=1))
    boundary = row(last_seen_at=cutoff)
    recent = row(last_seen_at=status_time)
    WorkerHeartbeat.objects.bulk_create((boundary, old, recent, oldest))
    media = WorkerHeartbeat.objects.create(
        role="media",
        worker_id=str(uuid.uuid4()),
        release_id=RELEASE,
        schema_identity=SCHEMA,
        manifest_identity=MANIFEST,
        last_seen_at=oldest.last_seen_at,
        status="idle",
        metrics={},
    )
    replacement_worker_id = str(uuid.uuid4())

    replacement = heartbeat_service.publish_heartbeat_for_test(
        role="operations",
        worker_id=replacement_worker_id,
        release_id=RELEASE,
        schema_identity=SCHEMA,
        manifest_identity=MANIFEST,
        observed_at=status_time,
    )

    assert replacement.pk == oldest.pk
    assert replacement.worker_id == replacement_worker_id
    assert not WorkerHeartbeat.objects.filter(worker_id=oldest.worker_id).exists()
    assert WorkerHeartbeat.objects.filter(pk__in=(old.pk, boundary.pk, recent.pk)).count() == 3
    assert WorkerHeartbeat.objects.filter(pk=media.pk, worker_id=media.worker_id).exists()
    assert WorkerHeartbeat.objects.filter(role="operations").count() == 4
    assert any(
        index.fields == ["role", "last_seen_at"]
        for index in WorkerHeartbeat._meta.indexes
    )


@override_settings(
    AEGIS_ENVIRONMENT="test",
    AEGIS_WORKER_HEARTBEAT_RETENTION_SECONDS=3600,
    AEGIS_WORKER_HEARTBEAT_SLOTS_PER_ROLE=1,
)
def test_new_worker_fails_closed_when_its_role_slots_are_retained() -> None:
    status_time = timezone.now()
    retained = _publish(role="operations", now=status_time)
    media = _publish(role="media", now=status_time - timedelta(hours=2))

    with pytest.raises(heartbeat_service.HeartbeatCapacityError):
        _publish(
            role="operations",
            worker_id=str(uuid.uuid4()),
            now=status_time + timedelta(seconds=1),
        )

    retained.refresh_from_db()
    media.refresh_from_db()
    assert retained.last_seen_at == status_time
    assert media.role == "media"


@override_settings(
    AEGIS_ENVIRONMENT="test",
    AEGIS_WORKER_HEARTBEAT_RETENTION_SECONDS=3600,
    AEGIS_WORKER_HEARTBEAT_SLOTS_PER_ROLE=1,
)
def test_concurrent_new_workers_cannot_exceed_the_per_role_slot_bound() -> None:
    status_time = timezone.now()
    worker_ids = (str(uuid.uuid4()), str(uuid.uuid4()))

    def publish(worker_id: str) -> str:
        close_old_connections()
        try:
            try:
                _publish(worker_id=worker_id, now=status_time)
            except heartbeat_service.HeartbeatCapacityError:
                return "capacity"
            return "published"
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, worker_ids))

    assert sorted(outcomes) == ["capacity", "published"]
    assert WorkerHeartbeat.objects.filter(role="operations").count() == 1


@override_settings(AEGIS_ENVIRONMENT="production")
def test_test_time_injection_boundary_is_unavailable_in_production() -> None:
    with pytest.raises(PermissionError, match="test-only"):
        heartbeat_service.publish_heartbeat_for_test(
            role="operations",
            worker_id=str(uuid.uuid4()),
            release_id=RELEASE,
            schema_identity=SCHEMA,
            manifest_identity=MANIFEST,
            observed_at=timezone.now(),
        )
