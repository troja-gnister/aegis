"""Bounded, fail-closed media worker configuration (spec M4)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from aegis.config import ConfigurationError

BYTES_PER_GIB: Final = 1 << 30
_MAX_RAW_DIGITS: Final = 9

# (environment name, default, minimum, maximum)
Limit = tuple[str, int, int, int]
CACHE_BUDGET_GB: Final[Limit] = ("AEGIS_DERIVATIVES_CACHE_BUDGET_GB", 20, 1, 10_000)
# Read by the worker and, with the same default and bounds, by the processor
# (aegis_media_processor.loop.concurrency_from_environment).
PROCESSOR_CONCURRENCY: Final[Limit] = ("AEGIS_MEDIA_PROCESSOR_CONCURRENCY", 4, 1, 16)
DISCOVERY_BATCH: Final[Limit] = ("AEGIS_MEDIA_DISCOVERY_BATCH", 500, 50, 5_000)
# A discovery pass reads every catalog row of every active root, so passes are an hour apart by
# default; the worker re-reads the active roots every minute. Test stacks shorten both (the e2e
# harness, through compose.test.yaml) so a fresh synthetic library is found within seconds.
DISCOVERY_INTERVAL: Final[Limit] = ("AEGIS_MEDIA_DISCOVERY_INTERVAL_SECONDS", 3_600, 5, 86_400)
ROOTS_REFRESH: Final[Limit] = ("AEGIS_MEDIA_ROOTS_REFRESH_SECONDS", 60, 5, 3_600)
STAGE_LIMITS: Final[Mapping[str, tuple[Limit, Limit]]] = MappingProxyType({
    "probe": (
        ("AEGIS_MEDIA_PROBE_TIMEOUT_SECONDS", 30, 1, 600),
        ("AEGIS_MEDIA_PROBE_MEMORY_MB", 1_024, 64, 4_096),
    ),
    "thumbnail": (
        ("AEGIS_MEDIA_THUMBNAIL_TIMEOUT_SECONDS", 60, 1, 900),
        ("AEGIS_MEDIA_THUMBNAIL_MEMORY_MB", 1_536, 128, 8_192),
    ),
    "preview": (
        ("AEGIS_MEDIA_PREVIEW_TIMEOUT_SECONDS", 120, 1, 1_800),
        ("AEGIS_MEDIA_PREVIEW_MEMORY_MB", 1_536, 128, 8_192),
    ),
    # The transcode timeout is a cap; the worker scales it with the probed duration.
    "transcode": (
        ("AEGIS_MEDIA_TRANSCODE_TIMEOUT_SECONDS", 7_200, 60, 43_200),
        ("AEGIS_MEDIA_TRANSCODE_MEMORY_MB", 3_072, 256, 16_384),
    ),
})
# A poster is a frame extraction plus a resize, so it runs under the thumbnail limits. A
# backfill job (probe plus thumbnail or poster) combines two stages: pipeline.job_limits.
_STAGE_FOR_OPERATION: Final[Mapping[str, str]] = MappingProxyType({
    "probe": "probe",
    "thumbnail": "thumbnail",
    "poster": "thumbnail",
    "preview": "preview",
    "transcode": "transcode",
})


def _bounded_integer(environ: Mapping[str, str], limit: Limit) -> int:
    name, default, minimum, maximum = limit
    raw = environ.get(name, str(default))
    if (
        not isinstance(raw, str)
        or not 1 <= len(raw) <= _MAX_RAW_DIGITS
        or not raw.isascii()
        or not raw.isdecimal()
    ):
        raise ConfigurationError(f"{name} must be an integer")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} is outside its safe bounds")
    return value


@dataclass(frozen=True, slots=True)
class StageLimits:
    timeout_seconds: int
    memory_mb: int

    @property
    def memory_bytes(self) -> int:
        return self.memory_mb * (1 << 20)


@dataclass(frozen=True, slots=True)
class MediaPolicy:
    cache_budget_gb: int
    processor_concurrency: int
    discovery_batch: int
    discovery_interval_seconds: int
    roots_refresh_seconds: int
    probe: StageLimits
    thumbnail: StageLimits
    preview: StageLimits
    transcode: StageLimits

    @property
    def cache_budget_bytes(self) -> int:
        """The eviction budget; ``GB`` in the setting's name means GiB (2**30 bytes)."""
        return self.cache_budget_gb * BYTES_PER_GIB

    def limits_for(self, operation: str) -> StageLimits:
        stage = _STAGE_FOR_OPERATION.get(operation)
        if stage is None:
            raise ValueError("invalid media operation")
        limits: StageLimits = getattr(self, stage)
        return limits

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> MediaPolicy:
        stages = {
            stage: StageLimits(
                timeout_seconds=_bounded_integer(environ, timeout),
                memory_mb=_bounded_integer(environ, memory),
            )
            for stage, (timeout, memory) in STAGE_LIMITS.items()
        }
        return cls(
            cache_budget_gb=_bounded_integer(environ, CACHE_BUDGET_GB),
            processor_concurrency=_bounded_integer(environ, PROCESSOR_CONCURRENCY),
            discovery_batch=_bounded_integer(environ, DISCOVERY_BATCH),
            discovery_interval_seconds=_bounded_integer(environ, DISCOVERY_INTERVAL),
            roots_refresh_seconds=_bounded_integer(environ, ROOTS_REFRESH),
            probe=stages["probe"],
            thumbnail=stages["thumbnail"],
            preview=stages["preview"],
            transcode=stages["transcode"],
        )
