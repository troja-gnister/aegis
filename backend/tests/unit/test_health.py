import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from aegis_apps.common.health import database_status
from aegis_apps.common.views import ready
from django.test import Client, RequestFactory


def test_readiness_fails_closed_when_database_is_unavailable() -> None:
    request = RequestFactory().get("/health/ready")
    with patch(
        "aegis_apps.common.health.database_status",
        return_value=(False, "database unavailable"),
    ):
        response = ready(request)

    assert response.status_code == 503
    assert json.loads(response.content) == {
        "status": "unavailable",
        "checks": {"database": "database unavailable"},
    }


def test_readiness_succeeds_when_database_is_ready() -> None:
    request = RequestFactory().get("/health/ready")
    with patch(
        "aegis_apps.common.health.database_status",
        return_value=(True, "ok"),
    ):
        response = ready(request)

    assert response.status_code == 200
    assert json.loads(response.content) == {"status": "ok", "checks": {"database": "ok"}}


def test_database_status_reports_pending_migrations() -> None:
    executor = SimpleNamespace(
        loader=SimpleNamespace(graph=SimpleNamespace(leaf_nodes=lambda: [("app", "0001")])),
        migration_plan=lambda _targets: [("pending", False)],
    )
    with (
        patch("aegis_apps.common.health.connection.ensure_connection"),
        patch("aegis_apps.common.health.MigrationExecutor", return_value=executor),
    ):
        assert database_status() == (False, "migrations pending")


def test_database_status_suppresses_exception_details() -> None:
    sensitive_detail = "postgres://user:password@secret-host/private/path"
    with patch(
        "aegis_apps.common.health.connection.ensure_connection",
        side_effect=RuntimeError(sensitive_detail),
    ):
        result = database_status()

    assert result == (False, "database unavailable")
    assert sensitive_detail not in result[1]


def test_ready_route_is_registered() -> None:
    with patch(
        "aegis_apps.common.health.database_status",
        return_value=(True, "ok"),
    ):
        response = Client().get("/health/ready")

    assert response.status_code == 200


def test_liveness_never_queries_dependencies() -> None:
    database_status = Mock(side_effect=AssertionError("database queried by liveness"))
    with patch("aegis_apps.common.health.database_status", database_status):
        response = Client().get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    database_status.assert_not_called()
