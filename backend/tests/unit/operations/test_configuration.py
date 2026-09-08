from __future__ import annotations

import pytest
from aegis.config import ConfigurationError
from aegis_apps.operations.config import WorkerRuntimeConfig


def test_worker_runtime_configuration_has_bounded_phase_one_defaults() -> None:
    config = WorkerRuntimeConfig.from_environ({})

    assert config.release_id == "development"
    assert config.process_role is None
    assert config.required_roles == ("operations", "indexer", "media")
    assert 0 < config.heartbeat_seconds <= 15
    assert config.heartbeat_seconds <= config.heartbeat_fresh_seconds <= 300
    assert 0 < config.poll_seconds <= 30
    assert 0 <= config.poll_jitter_seconds <= config.poll_seconds
    assert 0 < config.lease_seconds <= 300
    assert 0 < config.retry_base_seconds <= config.retry_max_seconds <= 86_400


def test_worker_runtime_configuration_preserves_valid_required_role_order() -> None:
    config = WorkerRuntimeConfig.from_environ(
        {
            "AEGIS_RELEASE_ID": "release-2026.09.08+build.1",
            "AEGIS_PROCESS_ROLE": "media",
            "AEGIS_REQUIRED_WORKER_ROLES": "media,operations",
            "AEGIS_JOB_LEASE_SECONDS": "45",
            "AEGIS_JOB_RETRY_BASE_SECONDS": "2",
            "AEGIS_JOB_RETRY_MAX_SECONDS": "120",
            "AEGIS_WORKER_HEARTBEAT_SECONDS": "12.5",
            "AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS": "45",
            "AEGIS_QUEUE_POLL_SECONDS": "1.5",
            "AEGIS_QUEUE_POLL_JITTER_SECONDS": "0.25",
        }
    )

    assert config.release_id == "release-2026.09.08+build.1"
    assert config.process_role == "media"
    assert config.required_roles == ("media", "operations")
    assert config.lease_seconds == 45
    assert config.heartbeat_seconds == 12.5


@pytest.mark.parametrize(
    "environment",
    [
        {"AEGIS_RELEASE_ID": ""},
        {"AEGIS_RELEASE_ID": "secret/path"},
        {"AEGIS_PROCESS_ROLE": "unknown"},
        {"AEGIS_PROCESS_ROLE": " operations "},
        {"AEGIS_REQUIRED_WORKER_ROLES": ""},
        {"AEGIS_REQUIRED_WORKER_ROLES": "media,media"},
        {"AEGIS_REQUIRED_WORKER_ROLES": "media,unknown"},
        {"AEGIS_JOB_LEASE_SECONDS": "0"},
        {"AEGIS_JOB_LEASE_SECONDS": "nan"},
        {"AEGIS_WORKER_HEARTBEAT_SECONDS": "16"},
        {"AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS": "5"},
        {"AEGIS_QUEUE_POLL_SECONDS": "0"},
        {"AEGIS_QUEUE_POLL_JITTER_SECONDS": "99"},
        {"AEGIS_JOB_RETRY_BASE_SECONDS": "301"},
        {"AEGIS_JOB_RETRY_MAX_SECONDS": "0"},
    ],
)
def test_worker_runtime_configuration_rejects_invalid_or_unbounded_values(
    environment: dict[str, str],
) -> None:
    with pytest.raises(ConfigurationError):
        WorkerRuntimeConfig.from_environ(environment)
