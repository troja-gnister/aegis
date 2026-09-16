from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import patch

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.catalog.authorization import CatalogUnavailable
from aegis_apps.indexing.database import ScanRequestThrottled
from aegis_apps.indexing.models import IndexDeployment, RootIndexState, ScanRequest, ScanRun
from aegis_apps.operations.models import WorkerHeartbeat
from aegis_apps.operations.selectors import SchemaCompatibilityError, current_schema_identity
from django.conf import settings
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _status_path(root_id: uuid.UUID) -> str:
    return f"/api/v1/roots/{root_id}/index-status"


def _scan_path(root_id: uuid.UUID) -> str:
    return f"/api/v1/roots/{root_id}/scans"


def _csrf(api_catalog: Any) -> str:
    value = api_catalog.client.cookies["csrftoken"].value
    assert isinstance(value, str)
    return value


def test_index_status_is_browse_authorized_stored_and_precision_safe(api_catalog: Any) -> None:
    with api_catalog.database.as_django_role("aegis_web"):
        response = api_catalog.group_client.get(_status_path(api_catalog.root.pk))

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    payload = response.json()
    assert payload == {
        "state": "ready",
        "generation": "7",
        "observedEntries": str(2**60 + 3),
        "completedDirectories": str(2**60 + 4),
        "degradedDirectories": "0",
        "updatedAt": payload["updatedAt"],
        "lastCompletedAt": payload["lastCompletedAt"],
    }
    assert isinstance(payload["updatedAt"], str)
    assert isinstance(payload["lastCompletedAt"], str)
    assert set(payload) == {
        "state",
        "generation",
        "observedEntries",
        "completedDirectories",
        "degradedDirectories",
        "updatedAt",
        "lastCompletedAt",
    }


def test_index_status_mismatched_binding_and_invalid_active_run_are_unavailable(
    api_catalog: Any,
) -> None:
    RootIndexState.objects.filter(root=api_catalog.root).update(binding_epoch=2)
    binding = api_catalog.get(_status_path(api_catalog.root.pk))
    RootIndexState.objects.filter(root=api_catalog.root).update(
        binding_epoch=1,
        status="scanning",
        active_run=None,
    )
    coordination = api_catalog.get(_status_path(api_catalog.root.pk))

    assert binding.status_code == coordination.status_code == 200
    assert binding.json()["state"] == "unavailable"
    assert coordination.json()["state"] == "unavailable"
    assert binding.headers["Cache-Control"] == "private, no-store"
    assert coordination.headers["Cache-Control"] == "private, no-store"


def test_active_status_requires_a_fresh_compatible_stored_indexer(api_catalog: Any) -> None:
    deployment = IndexDeployment.objects.get(pk=1)
    schema_identity = current_schema_identity()
    run = ScanRun.objects.create(
        root=api_catalog.root,
        binding_epoch=deployment.epoch,
        policy_epoch=deployment.epoch,
        root_epoch=api_catalog.root.authorization_epoch,
        manifest_identity=deployment.manifest_identity,
        generation=7,
        start_epoch=0,
        state="running",
        started_at=timezone.now(),
    )
    RootIndexState.objects.filter(root=api_catalog.root).update(
        status="scanning",
        active_run=run,
    )
    heartbeat = WorkerHeartbeat.objects.create(
        role="indexer",
        worker_id=str(uuid.uuid4()),
        release_id=settings.AEGIS_RELEASE_ID,
        schema_identity=schema_identity,
        manifest_identity=deployment.manifest_identity,
        last_seen_at=timezone.now(),
        current_job_id=None,
        status="running",
        metrics={},
    )

    with (
        api_catalog.database.as_django_role("aegis_web"),
        CaptureQueriesContext(connection) as captured,
    ):
        healthy = api_catalog.client.get(_status_path(api_catalog.root.pk))
    heartbeat.schema_identity = "schema-mismatch"
    heartbeat.save(update_fields=("schema_identity",))
    mismatched = api_catalog.get(_status_path(api_catalog.root.pk))

    assert healthy.status_code == mismatched.status_code == 200
    assert healthy.json()["state"] == "scanning"
    assert mismatched.json()["state"] == "unavailable"
    assert len(captured) <= 16


def test_index_status_unknown_and_unauthorized_match_catalog_not_found(api_catalog: Any) -> None:
    unknown = api_catalog.get(_status_path(uuid.uuid4()))
    with api_catalog.database.as_django_role("aegis_web"):
        unauthorized = api_catalog.group_client.get(_status_path(uuid.uuid4()))

    assert unknown.status_code == unauthorized.status_code == 404
    assert unknown.content == unauthorized.content
    assert unknown.json()["type"] == "catalog_not_found"
    assert unknown.headers["Cache-Control"] == "private, no-store"
    assert unauthorized.headers["Cache-Control"] == "private, no-store"


def test_manual_scan_requires_csrf_and_valid_client_request_id(api_catalog: Any) -> None:
    path = _scan_path(api_catalog.root.pk)
    rejected_csrf = api_catalog.post(
        path,
        data=b"",
        content_type="application/json",
        headers={"X-Request-ID": "scan_request_001"},
    )
    rejected_id = api_catalog.post(
        path,
        data=b"",
        content_type="application/json",
        headers={"X-CSRFToken": _csrf(api_catalog), "X-Request-ID": "bad"},
    )

    assert rejected_csrf.status_code == 403
    assert rejected_csrf.json()["type"] == "csrf_failed"
    assert rejected_id.status_code == 400
    assert rejected_id.json()["type"] == "scan_request_denied"
    assert not ScanRequest.objects.exists()
    for response in (rejected_csrf, rejected_id):
        assert response.headers["Cache-Control"] == "private, no-store"


def test_manual_scan_uses_header_as_idempotency_and_audit_request_id(api_catalog: Any) -> None:
    request_id = "scan_request_002"
    response = api_catalog.post(
        _scan_path(api_catalog.root.pk),
        data=b"",
        content_type="application/json",
        headers={"X-CSRFToken": _csrf(api_catalog), "X-Request-ID": request_id},
    )

    assert response.status_code == 202
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.json() == {"scanId": str(uuid.UUID(response.json()["scanId"]))}
    stored = ScanRequest.objects.get(actor=api_catalog.user)
    event = AuditEvent.objects.get(event_type="index.scan.requested")
    assert stored.client_request_id == event.request_id == request_id
    assert str(stored.run_id) == response.json()["scanId"]
    assert "/srv/aegis" not in response.content.decode()

    repeated = api_catalog.post(
        _scan_path(api_catalog.root.pk),
        data=b"",
        content_type="application/json",
        headers={"X-CSRFToken": _csrf(api_catalog), "X-Request-ID": request_id},
    )
    assert repeated.status_code == 202
    assert repeated.json() == response.json()
    assert ScanRequest.objects.filter(actor=api_catalog.user).count() == 1


def test_browse_grant_cannot_request_scan_and_unknown_root_is_indistinguishable(
    api_catalog: Any,
) -> None:
    headers = {
        "X-CSRFToken": api_catalog.group_client.cookies["csrftoken"].value,
        "X-Request-ID": "scan_request_003",
    }
    with api_catalog.database.as_django_role("aegis_web"):
        unauthorized = api_catalog.group_client.post(
            _scan_path(api_catalog.root.pk),
            data=b"",
            content_type="application/json",
            headers=headers,
        )
        unknown = api_catalog.group_client.post(
            _scan_path(uuid.uuid4()),
            data=b"",
            content_type="application/json",
            headers={**headers, "X-Request-ID": "scan_request_004"},
        )

    assert unauthorized.status_code == unknown.status_code == 403
    assert unauthorized.content == unknown.content
    assert unauthorized.json() == {
        "type": "scan_request_denied",
        "title": "Scan request denied",
    }
    assert not ScanRequest.objects.exists()


def test_root_admin_alone_can_scan_but_cannot_read_catalog_metadata(api_catalog: Any) -> None:
    status_path = _status_path(api_catalog.root.pk)
    entries_path = f"/api/v1/roots/{api_catalog.root.pk}/entries"
    scan_path = _scan_path(api_catalog.root.pk)
    with api_catalog.database.as_django_role("aegis_web"):
        status = api_catalog.admin_client.get(status_path)
        entries = api_catalog.admin_client.get(entries_path)
        scan = api_catalog.admin_client.post(
            scan_path,
            data=b"",
            content_type="application/json",
            headers={
                "X-CSRFToken": api_catalog.admin_client.cookies["csrftoken"].value,
                "X-Request-ID": "scan_request_admin_only",
            },
        )

    assert status.status_code == entries.status_code == 404
    assert status.content == entries.content
    assert scan.status_code == 202
    assert ScanRequest.objects.filter(actor=api_catalog.admin_user).count() == 1


def test_anonymous_scan_is_401_after_valid_csrf_admission(api_catalog: Any) -> None:
    client = Client(enforce_csrf_checks=True)
    csrf = client.get("/api/v1/auth/csrf").json()["csrfToken"]
    with api_catalog.database.as_django_role("aegis_web"):
        response = client.post(
            _scan_path(api_catalog.root.pk),
            data=b"",
            content_type="application/json",
            headers={"X-CSRFToken": csrf, "X-Request-ID": "scan_request_anonymous"},
        )

    assert response.status_code == 401
    assert response.json() == {
        "type": "authentication_required",
        "title": "Authentication required",
    }
    assert response.headers["Cache-Control"] == "private, no-store"
    assert not ScanRequest.objects.exists()


def test_manual_scan_rejects_body_query_and_wrong_methods(api_catalog: Any) -> None:
    path = _scan_path(api_catalog.root.pk)
    headers = {"X-CSRFToken": _csrf(api_catalog), "X-Request-ID": "scan_request_005"}
    body = api_catalog.post(path, data={"path": "/srv/aegis/private"}, headers=headers)
    query = api_catalog.post(
        f"{path}?content=true",
        data=b"",
        content_type="application/json",
        headers=headers,
    )
    with api_catalog.database.as_django_role("aegis_web"):
        wrong_method = api_catalog.client.delete(path, headers=headers)

    assert body.status_code == query.status_code == 400
    assert body.json()["type"] == query.json()["type"] == "scan_request_denied"
    assert wrong_method.status_code == 405
    assert not ScanRequest.objects.exists()
    for response in (body, query, wrong_method):
        assert response.headers["Cache-Control"] == "private, no-store"


def test_manual_scan_rate_limit_has_bounded_retry_after(api_catalog: Any) -> None:
    with patch(
        "aegis_apps.indexing.services.request_root_scan",
        side_effect=ScanRequestThrottled(),
    ):
        response = api_catalog.post(
            _scan_path(api_catalog.root.pk),
            data=b"",
            content_type="application/json",
            headers={
                "X-CSRFToken": _csrf(api_catalog),
                "X-Request-ID": "scan_request_006",
            },
        )

    assert response.status_code == 429
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    assert response.json() == {
        "type": "scan_request_denied",
        "title": "Scan request denied",
    }
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("path", (_status_path(uuid.uuid4()), _scan_path(uuid.uuid4())))
def test_status_and_scan_resolver_paths_are_private(path: str) -> None:
    malformed = path.replace(str(uuid.UUID(path.split("/")[4])), "not-a-uuid")
    response = Client().get(malformed)

    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "private, no-store"


def test_status_schema_database_outage_is_private_and_nondisclosing(api_catalog: Any) -> None:
    with patch(
        "aegis_apps.catalog.authorization.browse_context",
        side_effect=RuntimeError("schema /srv/aegis/private unavailable"),
    ):
        api_catalog.client.raise_request_exception = False
        generic = api_catalog.get(_status_path(api_catalog.root.pk))
    with patch(
        "aegis_apps.indexing.selectors.index_status",
        side_effect=SchemaCompatibilityError("schema mismatch"),
    ):
        schema_unavailable = api_catalog.get(_status_path(api_catalog.root.pk))
    with patch(
        "aegis_apps.catalog.authorization.browse_context",
        side_effect=CatalogUnavailable(),
    ):
        database_unavailable = api_catalog.get(_status_path(api_catalog.root.pk))

    assert generic.status_code == 500
    assert b"/srv/aegis/private" not in generic.content
    assert schema_unavailable.status_code == database_unavailable.status_code == 503
    assert schema_unavailable.json()["type"] == "catalog_unavailable"
    assert database_unavailable.json()["type"] == "catalog_unavailable"
    assert generic.headers["Cache-Control"] == "private, no-store"
    assert schema_unavailable.headers["Cache-Control"] == "private, no-store"
    assert database_unavailable.headers["Cache-Control"] == "private, no-store"
