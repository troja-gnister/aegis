from __future__ import annotations

import pytest
from aegis.config import ConfigurationError
from aegis_apps.media.request_policy import MediaRequestPolicy
from django.conf import settings


def test_defaults_cap_previews_and_transcodes_and_double_the_backoff() -> None:
    policy = MediaRequestPolicy.from_environ({})

    assert policy == MediaRequestPolicy(16, 2, 60, 86_400)
    assert policy == settings.AEGIS_MEDIA_REQUEST_POLICY
    assert policy.active_limit("preview") == 16
    assert policy.active_limit("transcode") == 2
    assert [policy.backoff_for(n) for n in (1, 2, 3, 4)] == [60, 120, 240, 480]
    assert policy.backoff_for(12) == 86_400
    assert policy.backoff_for(10_000) == 86_400


@pytest.mark.parametrize(
    ("name", "minimum", "maximum"),
    [
        ("AEGIS_MEDIA_ACTIVE_PREVIEW_REQUESTS", 1, 256),
        ("AEGIS_MEDIA_ACTIVE_TRANSCODE_REQUESTS", 1, 16),
        ("AEGIS_MEDIA_REQUEST_BACKOFF_SECONDS", 1, 3_600),
        ("AEGIS_MEDIA_REQUEST_BACKOFF_MAX_SECONDS", 60, 604_800),
    ],
)
def test_bounds_are_inclusive_and_fail_closed(name: str, minimum: int, maximum: int) -> None:
    MediaRequestPolicy.from_environ({name: str(minimum)})
    MediaRequestPolicy.from_environ({name: str(maximum)})
    for value in (str(minimum - 1), str(maximum + 1), "", " 5", "+5", "5.0", "0x10", "\u0661"):
        with pytest.raises(ConfigurationError, match=name):
            MediaRequestPolicy.from_environ({name: value})


def test_the_maximum_backoff_may_not_be_below_the_base() -> None:
    with pytest.raises(ConfigurationError, match="AEGIS_MEDIA_REQUEST_BACKOFF_MAX_SECONDS"):
        MediaRequestPolicy.from_environ({
            "AEGIS_MEDIA_REQUEST_BACKOFF_SECONDS": "3600",
            "AEGIS_MEDIA_REQUEST_BACKOFF_MAX_SECONDS": "600",
        })


@pytest.mark.parametrize("failures", [0, -1, True])
def test_backoff_needs_at_least_one_failure(failures: int) -> None:
    with pytest.raises(ValueError):
        MediaRequestPolicy(16, 2, 60, 86_400).backoff_for(failures)


def test_only_requestable_kinds_have_a_limit() -> None:
    with pytest.raises(ValueError):
        MediaRequestPolicy(16, 2, 60, 86_400).active_limit("thumbnail")
