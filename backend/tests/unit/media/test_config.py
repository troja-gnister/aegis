from __future__ import annotations

import pytest
from aegis.config import ConfigurationError
from aegis_apps.media.config import MediaPolicy, StageLimits
from django.conf import settings

BOUNDS = {
    "AEGIS_DERIVATIVES_CACHE_BUDGET_GB": (1, 10_000),
    "AEGIS_MEDIA_PROCESSOR_CONCURRENCY": (1, 16),
    "AEGIS_MEDIA_DISCOVERY_BATCH": (50, 5_000),
    "AEGIS_MEDIA_DISCOVERY_INTERVAL_SECONDS": (5, 86_400),
    "AEGIS_MEDIA_ROOTS_REFRESH_SECONDS": (5, 3_600),
    "AEGIS_MEDIA_PROBE_TIMEOUT_SECONDS": (1, 600),
    "AEGIS_MEDIA_PROBE_MEMORY_MB": (64, 4_096),
    "AEGIS_MEDIA_THUMBNAIL_TIMEOUT_SECONDS": (1, 900),
    "AEGIS_MEDIA_THUMBNAIL_MEMORY_MB": (128, 8_192),
    "AEGIS_MEDIA_PREVIEW_TIMEOUT_SECONDS": (1, 1_800),
    "AEGIS_MEDIA_PREVIEW_MEMORY_MB": (128, 8_192),
    "AEGIS_MEDIA_TRANSCODE_TIMEOUT_SECONDS": (60, 43_200),
    "AEGIS_MEDIA_TRANSCODE_MEMORY_MB": (256, 16_384),
}


def test_media_policy_defaults() -> None:
    policy = MediaPolicy.from_environ({})

    assert policy == MediaPolicy(
        cache_budget_gb=20,
        processor_concurrency=2,
        discovery_batch=500,
        discovery_interval_seconds=3_600,
        roots_refresh_seconds=60,
        probe=StageLimits(timeout_seconds=30, memory_mb=1_024),
        thumbnail=StageLimits(timeout_seconds=60, memory_mb=1_536),
        preview=StageLimits(timeout_seconds=120, memory_mb=1_536),
        transcode=StageLimits(timeout_seconds=7_200, memory_mb=3_072),
    )
    assert policy.cache_budget_bytes == 20 * (1 << 30)
    assert policy.probe.memory_bytes == 1_024 * (1 << 20)
    # Posters run under the thumbnail limits: 1536 MiB of address space for ffmpeg and Pillow.
    assert policy.limits_for("poster").memory_bytes == 1_536 * (1 << 20)


def test_default_intervals_are_the_discovery_and_worker_defaults() -> None:
    from aegis_apps.media import worker
    from aegis_apps.media.discovery import DISCOVERY_INTERVAL_SECONDS

    policy = MediaPolicy.from_environ({})

    # One definition (config.py); the module constants are views of it, unchanged in value.
    assert policy.discovery_interval_seconds == DISCOVERY_INTERVAL_SECONDS == 3_600
    assert policy.roots_refresh_seconds == worker.ROOTS_REFRESH_SECONDS == 60


def test_media_policy_is_loaded_into_settings() -> None:
    assert isinstance(settings.AEGIS_MEDIA_POLICY, MediaPolicy)


def test_poster_operations_use_thumbnail_limits() -> None:
    policy = MediaPolicy.from_environ({})

    assert policy.limits_for("poster") is policy.thumbnail
    assert policy.limits_for("thumbnail") is policy.thumbnail
    assert policy.limits_for("probe") is policy.probe
    assert policy.limits_for("preview") is policy.preview
    assert policy.limits_for("transcode") is policy.transcode
    for operation in ("", "original", "Thumbnail", "transcode "):
        with pytest.raises(ValueError, match="invalid media operation"):
            policy.limits_for(operation)


@pytest.mark.parametrize(("name", "bounds"), BOUNDS.items())
def test_media_policy_accepts_both_inclusive_bounds(
    name: str, bounds: tuple[int, int]
) -> None:
    for value in bounds:
        MediaPolicy.from_environ({name: str(value)})


@pytest.mark.parametrize(("name", "bounds"), BOUNDS.items())
def test_media_policy_rejects_values_outside_bounds(
    name: str, bounds: tuple[int, int]
) -> None:
    minimum, maximum = bounds
    for value in (minimum - 1, maximum + 1):
        with pytest.raises(ConfigurationError, match=name):
            MediaPolicy.from_environ({name: str(value)})


def test_media_policy_reads_explicit_values() -> None:
    policy = MediaPolicy.from_environ(
        {
            "AEGIS_DERIVATIVES_CACHE_BUDGET_GB": "250",
            "AEGIS_MEDIA_PROCESSOR_CONCURRENCY": "4",
            "AEGIS_MEDIA_DISCOVERY_BATCH": "1000",
            "AEGIS_MEDIA_DISCOVERY_INTERVAL_SECONDS": "5",
            "AEGIS_MEDIA_ROOTS_REFRESH_SECONDS": "5",
            "AEGIS_MEDIA_TRANSCODE_TIMEOUT_SECONDS": "3600",
            "AEGIS_MEDIA_TRANSCODE_MEMORY_MB": "4096",
        }
    )

    assert policy.cache_budget_gb == 250
    assert policy.processor_concurrency == 4
    assert policy.discovery_batch == 1000
    assert (policy.discovery_interval_seconds, policy.roots_refresh_seconds) == (5, 5)
    assert policy.transcode == StageLimits(timeout_seconds=3600, memory_mb=4096)


@pytest.mark.parametrize(
    "value",
    [
        "",
        " 2",
        "2 ",
        "+2",
        "-2",
        "2.0",
        "1e1",
        "0x2",
        "1_0",
        "٢",
        "nan",
        "9" * 10,
        "1" * 5000,
    ],
)
def test_media_policy_fails_closed_on_non_canonical_integers(value: str) -> None:
    with pytest.raises(ConfigurationError, match="AEGIS_MEDIA_PROCESSOR_CONCURRENCY"):
        MediaPolicy.from_environ({"AEGIS_MEDIA_PROCESSOR_CONCURRENCY": value})
