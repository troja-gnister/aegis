"""The media worker's heartbeat metrics: counts and one safe code, never a name or a path."""

from __future__ import annotations

import pytest
from aegis_apps.media.enums import MediaErrorCode
from aegis_apps.operations.serializers import validate_heartbeat_metrics

MEDIA_COUNTERS = (
    "mediaInteractiveQueued", "mediaBackgroundPending", "mediaInFlight", "mediaAssets",
    "mediaAssetsComplete", "mediaProcessorFailures",
)


def test_a_full_media_heartbeat_is_accepted() -> None:
    metrics = {
        "queueAgeSeconds": 12, "scanProgress": 0.5, "mediaInteractiveQueued": 1,
        "mediaBackgroundPending": 10, "mediaInFlight": 3, "mediaAssets": 20,
        "mediaAssetsComplete": 10, "mediaProcessorFailures": 2,
        "mediaLastErrorCode": "decode_failed",
    }
    assert validate_heartbeat_metrics(metrics) == metrics


@pytest.mark.parametrize("key", MEDIA_COUNTERS)
@pytest.mark.parametrize("value", [0, 9_223_372_036_854_775_807])
def test_media_counters_accept_bounded_integers(key: str, value: int) -> None:
    assert validate_heartbeat_metrics({key: value}) == {key: value}


@pytest.mark.parametrize("key", MEDIA_COUNTERS)
@pytest.mark.parametrize("value", [-1, 2**63, 1.0, True, "1", None, "photo.jpg"])
def test_media_counters_refuse_anything_else(key: str, value: object) -> None:
    with pytest.raises(ValueError):
        validate_heartbeat_metrics({key: value})


@pytest.mark.parametrize("code", sorted(MediaErrorCode.values))
def test_every_media_error_code_is_a_valid_last_error(code: str) -> None:
    assert validate_heartbeat_metrics({"mediaLastErrorCode": code}) == {
        "mediaLastErrorCode": code,
    }


@pytest.mark.parametrize(
    "value",
    [
        "/srv/aegis/roots/slot/photo.jpg", "photo.jpg", "Decode_Failed", "", "x" * 33,
        "decode failed", "d\u00e9cod\u00e9", 1, None, ["decode_failed"],
    ],
)
def test_the_last_error_can_never_carry_a_name_or_a_path(value: object) -> None:
    with pytest.raises(ValueError):
        validate_heartbeat_metrics({"mediaLastErrorCode": value})


def test_unknown_media_keys_are_refused() -> None:
    with pytest.raises(ValueError):
        validate_heartbeat_metrics({"mediaLastFile": "photo"})
