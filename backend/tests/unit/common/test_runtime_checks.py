from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aegis_apps.common import runtime_checks
from django.test import override_settings

SCHEMA_IDENTITY = "sha256:" + "a" * 64
MANIFEST_IDENTITY = "b" * 64


def deployed_metadata() -> dict[str, str]:
    return {
        "application": "aegis",
        "releaseId": "release-11",
        "schemaIdentity": SCHEMA_IDENTITY,
    }


@override_settings(
    AEGIS_PROCESS_ROLE=None,
    AEGIS_RELEASE_ID="release-11",
    AEGIS_PUBLIC_URL="https://files.example.com",
)
def test_web_runtime_check_verifies_login_schema_release_and_local_http() -> None:
    with (
        patch.object(runtime_checks, "current_database_login", return_value="aegis_web"),
        patch.object(
            runtime_checks,
            "current_schema_identity",
            return_value=SCHEMA_IDENTITY,
        ),
        patch.object(
            runtime_checks,
            "deployed_database_metadata",
            return_value=deployed_metadata(),
        ),
        patch.object(runtime_checks, "configured_manifest", return_value=None),
        patch.object(runtime_checks, "probe_local_web", return_value=None) as probe,
    ):
        runtime_checks.check_runtime_boundary("web", require_http=True)

    probe.assert_called_once_with()


@override_settings(
    AEGIS_PROCESS_ROLE="operations",
    AEGIS_RELEASE_ID="release-11",
    AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS=45,
)
def test_worker_runtime_check_attests_mounts_and_compatible_heartbeat() -> None:
    manifest = SimpleNamespace(digest=MANIFEST_IDENTITY)
    with (
        patch.object(
            runtime_checks,
            "current_database_login",
            return_value="aegis_operations",
        ),
        patch.object(
            runtime_checks,
            "current_schema_identity",
            return_value=SCHEMA_IDENTITY,
        ),
        patch.object(
            runtime_checks,
            "deployed_database_metadata",
            return_value=deployed_metadata(),
        ),
        patch.object(runtime_checks, "configured_manifest", return_value=manifest),
        patch.object(runtime_checks, "attest_mounts") as attest,
        patch.object(
            runtime_checks,
            "worker_role_states",
            return_value={"operations": "healthy"},
        ) as states,
        patch.object(runtime_checks, "authoritative_database_time", return_value=object()),
    ):
        runtime_checks.check_runtime_boundary("operations")

    attest.assert_called_once_with(manifest, "operations")
    assert states.call_args.kwargs["release_id"] == "release-11"
    assert states.call_args.kwargs["schema_identity"] == SCHEMA_IDENTITY
    assert states.call_args.kwargs["manifest_identity"] == MANIFEST_IDENTITY


@pytest.mark.parametrize(
    ("role", "process_role", "database_login"),
    [
        ("operations", "indexer", "aegis_operations"),
        ("operations", "operations", "aegis_indexer"),
        ("web", "operations", "aegis_web"),
    ],
)
def test_runtime_check_rejects_process_or_database_role_mismatch(
    role: str,
    process_role: str | None,
    database_login: str,
) -> None:
    with (
        override_settings(
            AEGIS_PROCESS_ROLE=process_role,
            AEGIS_RELEASE_ID="release-11",
        ),
        patch.object(
            runtime_checks,
            "current_database_login",
            return_value=database_login,
        ),
        pytest.raises(runtime_checks.RuntimeBoundaryError),
    ):
        runtime_checks.check_runtime_boundary(role)


@override_settings(AEGIS_PROCESS_ROLE=None, AEGIS_RELEASE_ID="release-11")
def test_runtime_check_rejects_missing_or_mismatched_deployment_metadata() -> None:
    with (
        patch.object(runtime_checks, "current_database_login", return_value="aegis_web"),
        patch.object(
            runtime_checks,
            "current_schema_identity",
            return_value=SCHEMA_IDENTITY,
        ),
        patch.object(
            runtime_checks,
            "deployed_database_metadata",
            return_value={**deployed_metadata(), "releaseId": "other-release"},
        ),
        pytest.raises(runtime_checks.RuntimeBoundaryError),
    ):
        runtime_checks.check_runtime_boundary("web")


def test_runtime_check_rejects_unknown_roles_and_worker_http_probe() -> None:
    with pytest.raises(runtime_checks.RuntimeBoundaryError):
        runtime_checks.check_runtime_boundary("frontier")
    with pytest.raises(runtime_checks.RuntimeBoundaryError):
        runtime_checks.check_runtime_boundary("media", require_http=True)
