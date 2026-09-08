from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from aegis_apps.identity.models import User
from aegis_apps.operations.heartbeats import publish_heartbeat_for_test
from aegis_apps.operations.services import create_operation, enqueue_job
from aegis_apps.roots.models import Root, RootGrant
from django.test import Client, override_settings
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

STATUS_PATH = "/api/v1/admin/operations/status"
RELEASE = "release-status"
SCHEMA = "sha256:" + "c" * 64
MANIFEST = "d" * 64


@pytest.fixture(autouse=True)
def _enable_test_heartbeat_time_boundary(settings: object) -> None:
    settings.AEGIS_ENVIRONMENT = "test"  # type: ignore[attr-defined]


def _staff_client(*, staff: bool, superuser: bool = False) -> tuple[Client, User]:
    user = User.objects.create_user(
        username=f"status-user-{uuid.uuid4()}",
        is_staff=staff,
        is_superuser=superuser,
    )
    client = Client()
    client.force_login(user)
    return client, user


def test_status_api_is_staff_only_get_only_and_always_no_store() -> None:
    unauthenticated = Client().get(STATUS_PATH)
    assert unauthenticated.status_code == 401
    assert unauthenticated.json() == {
        "type": "authentication_required",
        "title": "Authentication required",
    }
    assert unauthenticated.headers["Cache-Control"] == "private, no-store"

    ordinary_client, ordinary = _staff_client(staff=False)
    root = Root.objects.create(
        slot_id="status-private-root",
        display_name="Sentinel private root",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    RootGrant.objects.create(root=root, user=ordinary, permissions=255)
    hidden = ordinary_client.get(STATUS_PATH)
    assert hidden.status_code == 404
    assert hidden.headers["Cache-Control"] == "private, no-store"
    assert str(root.id).encode() not in hidden.content
    assert root.slot_id.encode() not in hidden.content

    staff_client, _ = _staff_client(staff=True)
    method = staff_client.post(STATUS_PATH, data={}, content_type="application/json")
    assert method.status_code == 405
    assert method.headers["Cache-Control"] == "private, no-store"


@override_settings(
    AEGIS_RELEASE_ID=RELEASE,
    AEGIS_REQUIRED_WORKER_ROLES=("media", "operations", "indexer"),
    AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS=45,
)
def test_status_api_returns_exact_ordered_safe_aggregates() -> None:
    client, actor = _staff_client(staff=True, superuser=True)
    operation = create_operation(
        actor=actor,
        request_id="task10_status_request",
        kind="foundation.probe",
        intent={"roots": []},
    )
    enqueue_job(operation=operation, target_role="media")
    worker_id = str(uuid.uuid4())
    now = timezone.now()
    publish_heartbeat_for_test(
        role="media",
        worker_id=worker_id,
        release_id=RELEASE,
        schema_identity=SCHEMA,
        manifest_identity=MANIFEST,
        status="idle",
        metrics={"scanProgress": 0.4, "diskPressure": 0.85},
        observed_at=now,
    )

    with (
        patch("aegis_apps.operations.selectors.current_schema_identity", return_value=SCHEMA),
        patch(
            "aegis_apps.operations.selectors.current_manifest_identity",
            return_value=MANIFEST,
        ),
    ):
        response = client.get(STATUS_PATH)

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    roles = response.json()["roles"]
    assert [record["role"] for record in roles] == ["media", "operations", "indexer"]
    assert list(roles[0]) == [
        "role",
        "state",
        "jobCount",
        "oldestQueueAgeSeconds",
        "scanProgress",
        "diskPressure",
    ]
    assert roles[0] == {
        "role": "media",
        "state": "healthy",
        "jobCount": 1,
        "oldestQueueAgeSeconds": roles[0]["oldestQueueAgeSeconds"],
        "scanProgress": 0.4,
        "diskPressure": "warning",
    }
    assert isinstance(roles[0]["oldestQueueAgeSeconds"], int)
    assert roles[1]["role"] == "operations"
    assert roles[1]["state"] == "missing"
    assert roles[1]["jobCount"] == 1
    assert roles[2] == {
        "role": "indexer",
        "state": "missing",
        "jobCount": 0,
        "oldestQueueAgeSeconds": None,
        "scanProgress": None,
        "diskPressure": "unknown",
    }

    serialized = json.dumps(response.json(), sort_keys=True)
    for sentinel in (
        worker_id,
        RELEASE,
        SCHEMA,
        MANIFEST,
        str(actor.id),
        actor.username,
        "payload",
        "intent",
        "result",
        "safe_error_detail",
    ):
        assert sentinel not in serialized


def test_framework_generated_status_500_is_exactly_no_store_and_nondisclosing() -> None:
    client, _ = _staff_client(staff=True)
    client.raise_request_exception = False
    sentinel = "/private/status-secret token=credential"

    with patch(
        "aegis_apps.operations.api.operations_status",
        side_effect=RuntimeError(sentinel),
    ):
        response = client.get(STATUS_PATH)

    assert response.status_code == 500
    assert response.headers["Cache-Control"] == "private, no-store"
    assert sentinel.encode() not in response.content
