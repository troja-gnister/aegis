"""Bounded, fail-closed limits for interactive derivative requests (review I1).

Per actor, at most a few open preview and transcode requests may exist at once, so one
user cannot fill the interactive lane, which always runs before the background backfill.
After a failed request for the same entry, kind and fingerprint, a new request is refused
for a backoff that doubles with every failure, so a poison file cannot be re-queued in a
loop.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from aegis.config import ConfigurationError

from .config import Limit, _bounded_integer

ACTIVE_PREVIEWS: Final[Limit] = ("AEGIS_MEDIA_ACTIVE_PREVIEW_REQUESTS", 16, 1, 256)
ACTIVE_TRANSCODES: Final[Limit] = ("AEGIS_MEDIA_ACTIVE_TRANSCODE_REQUESTS", 2, 1, 16)
BACKOFF_SECONDS: Final[Limit] = ("AEGIS_MEDIA_REQUEST_BACKOFF_SECONDS", 60, 1, 3_600)
BACKOFF_MAX_SECONDS: Final[Limit] = (
    "AEGIS_MEDIA_REQUEST_BACKOFF_MAX_SECONDS", 86_400, 60, 604_800,
)
# A 429 asks the client to wait this long before asking again.
CAP_RETRY_AFTER_SECONDS: Final = 30
# Doublings stop here; the maximum bounds the wait anyway.
_MAX_DOUBLINGS: Final = 20


@dataclass(frozen=True, slots=True)
class MediaRequestPolicy:
    active_previews: int
    active_transcodes: int
    backoff_seconds: int
    backoff_max_seconds: int

    def active_limit(self, kind: str) -> int:
        if kind == "preview":
            return self.active_previews
        if kind == "transcode":
            return self.active_transcodes
        raise ValueError("invalid media request kind")

    def backoff_for(self, failures: int) -> int:
        """Seconds to refuse a new request after *failures* failed ones (at least 1)."""
        if type(failures) is not int or failures < 1:
            raise ValueError("invalid failure count")
        doublings = min(failures - 1, _MAX_DOUBLINGS)
        return min(self.backoff_seconds << doublings, self.backoff_max_seconds)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> MediaRequestPolicy:
        policy = cls(
            active_previews=_bounded_integer(environ, ACTIVE_PREVIEWS),
            active_transcodes=_bounded_integer(environ, ACTIVE_TRANSCODES),
            backoff_seconds=_bounded_integer(environ, BACKOFF_SECONDS),
            backoff_max_seconds=_bounded_integer(environ, BACKOFF_MAX_SECONDS),
        )
        if policy.backoff_max_seconds < policy.backoff_seconds:
            raise ConfigurationError(f"{BACKOFF_MAX_SECONDS[0]} is outside its safe bounds")
        return policy
