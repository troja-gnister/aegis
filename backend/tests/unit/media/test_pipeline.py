"""Pipeline decisions without a database: stages, limits, keys, backoff and error codes."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from aegis_apps.media.config import MediaPolicy
from aegis_apps.media.enums import (
    DerivativeExtension,
    DerivativeKind,
    MediaErrorCode,
    MediaKind,
    StageState,
)
from aegis_apps.media.keys import canonical_params, is_hex_digest
from aegis_apps.media.pipeline import (
    ENVIRONMENT_CODES,
    INTERACTIVE_RETRY_CODES,
    PROCESSOR_WIDE_CODES,
    QUARANTINE_AFTER,
    RETRY_MAX_SECONDS,
    TRANSCODE_MAX_BYTES,
    TRANSCODE_MIN_BYTES,
    TRANSCODE_UNKNOWN_BYTES,
    WORKER_ERROR_CODES,
    build_job,
    derivative_identities,
    derivative_parameters,
    is_counted,
    job_limits,
    next_operation,
    output_bytes,
    retry_delay,
    terminal,
)
from aegis_media_processor import protocol

POLICY = MediaPolicy.from_environ({})
FINGERPRINT = "f" * 64


def test_media_error_codes_equal_the_processor_codes_plus_the_worker_codes() -> None:
    processor_codes = set().union(*protocol.ERROR_CODES.values())
    assert set(MediaErrorCode.values) == processor_codes | WORKER_ERROR_CODES
    assert not processor_codes & WORKER_ERROR_CODES


def test_processor_outputs_are_derivative_kinds_and_extensions() -> None:
    for specs in protocol.OP_OUTPUTS.values():
        for spec in specs:
            assert spec.name in DerivativeKind.values
            assert spec.format in DerivativeExtension.values


def test_environment_codes_are_never_counted_and_file_codes_are() -> None:
    assert set(MediaErrorCode.values) >= ENVIRONMENT_CODES
    for code in ("source_unavailable", "tool_missing", "spawn_failed", "publish_failed",
                 "processor_unavailable"):
        assert not is_counted(code)
    for code in ("decode_failed", "crashed", "timeout", "memory_limit", "interrupted",
                 "output_invalid", "source_changed", "invalid_result"):
        assert is_counted(code)
    assert QUARANTINE_AFTER == 3


def test_processor_wide_and_interactive_retry_codes() -> None:
    assert {
        "tool_missing", "spawn_failed", "publish_failed", "processor_unavailable",
    } == PROCESSOR_WIDE_CODES
    # source_unavailable is one file's problem: it backs off that asset, never the worker.
    assert "source_unavailable" not in PROCESSOR_WIDE_CODES
    assert PROCESSOR_WIDE_CODES <= ENVIRONMENT_CODES
    # Interactive requests are re-queued for these, and fail (feeding the web backoff) only
    # for content failures.
    assert ENVIRONMENT_CODES | {"interrupted"} == INTERACTIVE_RETRY_CODES
    for code in ("decode_failed", "timeout", "memory_limit", "output_invalid", "too_large",
                 "unsupported_format", "source_changed", "crashed"):
        assert code not in INTERACTIVE_RETRY_CODES


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"kind": "photo", "probe_state": "pending", "thumbnail_state": "pending",
          "poster_state": None}, "probe"),
        ({"kind": "photo", "probe_state": "failed", "thumbnail_state": "pending",
          "poster_state": None}, "probe"),
        ({"kind": "photo", "probe_state": "ready", "thumbnail_state": "pending",
          "poster_state": None}, "thumbnail"),
        ({"kind": "photo", "probe_state": "ready", "thumbnail_state": "failed",
          "poster_state": None}, "thumbnail"),
        ({"kind": "photo", "probe_state": "ready", "thumbnail_state": "ready",
          "poster_state": None}, None),
        ({"kind": "video", "probe_state": "ready", "thumbnail_state": "pending",
          "poster_state": "pending"}, "poster"),
        ({"kind": "video", "probe_state": "ready", "thumbnail_state": "ready",
          "poster_state": "failed"}, "poster"),
        ({"kind": "video", "probe_state": "unsupported", "thumbnail_state": "unsupported",
          "poster_state": "unsupported"}, None),
        ({"kind": "video", "probe_state": "quarantined", "thumbnail_state": "quarantined",
          "poster_state": "quarantined"}, None),
    ],
)
def test_probe_comes_before_thumbnail_or_poster(row: dict[str, object], expected: str) -> None:
    assert next_operation(row) == expected


@pytest.mark.parametrize("op", ["probe", "thumbnail", "preview", "poster", "transcode"])
def test_job_limits_follow_the_policy_and_the_protocol_bounds(op: protocol.Op) -> None:
    limits = job_limits(op, POLICY, 12_345)
    stage = POLICY.limits_for(op)
    assert limits.memory_bytes == stage.memory_bytes
    if op != "transcode":
        assert limits.wall_seconds == stage.timeout_seconds
    assert limits.cpu_seconds >= limits.wall_seconds
    for name, (minimum, maximum) in protocol.LIMIT_BOUNDS.items():
        assert minimum <= getattr(limits, name) <= maximum


def test_transcode_budgets_scale_with_the_duration() -> None:
    short = job_limits("transcode", POLICY, 10_000)
    long = job_limits("transcode", POLICY, 1_800_000)
    capped = job_limits("transcode", POLICY, 3_600_000)
    unknown = job_limits("transcode", POLICY, None)
    assert short.wall_seconds == 120
    assert long.wall_seconds == 3 * 1800 + 60
    assert capped.wall_seconds == POLICY.transcode.timeout_seconds
    assert unknown.wall_seconds == POLICY.transcode.timeout_seconds
    assert long.cpu_seconds == 4 * long.wall_seconds
    assert output_bytes("transcode", 10_000) == TRANSCODE_MIN_BYTES
    assert output_bytes("transcode", 3_600_000) == 3600 * (4 << 20)
    assert output_bytes("transcode", 100 * 24 * 3600 * 1000) == TRANSCODE_MAX_BYTES
    assert output_bytes("transcode", None) == TRANSCODE_UNKNOWN_BYTES


@pytest.mark.parametrize("op", ["thumbnail", "preview", "poster", "transcode", "probe"])
def test_derivative_parameters_are_valid_key_parameters(op: protocol.Op) -> None:
    parameters = derivative_parameters(op, protocol.DEFAULT_PARAMS[op])
    assert parameters["op"] == op
    assert all(re.fullmatch(r"[a-z][A-Za-z0-9]{0,31}", name) for name in parameters)
    canonical_params(parameters)


def test_thumbnail_parameters_are_camel_cased_protocol_params() -> None:
    assert derivative_parameters("thumbnail", protocol.THUMBNAIL_PARAMS) == {
        "op": "thumbnail", "maxEdge": 400, "quality": 75,
    }
    assert derivative_parameters("poster", protocol.POSTER_PARAMS) == {
        "op": "poster", "thumbnailEdge": 400, "thumbnailQuality": 75, "posterEdge": 2048,
        "posterQuality": 82, "frameMs": 1000,
    }


def test_a_poster_job_yields_two_distinct_keys_and_others_one() -> None:
    poster = derivative_identities(FINGERPRINT, "poster", protocol.POSTER_PARAMS)
    assert [item.kind for item in poster] == ["thumbnail", "poster"]
    assert len({item.key for item in poster}) == 2
    assert all(is_hex_digest(item.key) and item.extension == "webp" for item in poster)
    (transcode,) = derivative_identities(FINGERPRINT, "transcode", protocol.TRANSCODE_PARAMS)
    assert transcode.extension == "mp4"
    assert derivative_identities(FINGERPRINT, "probe", protocol.PROBE_PARAMS) == ()


def test_keys_change_with_the_fingerprint_and_the_parameters() -> None:
    first = derivative_identities(FINGERPRINT, "thumbnail", protocol.THUMBNAIL_PARAMS)[0]
    again = derivative_identities(FINGERPRINT, "thumbnail", protocol.THUMBNAIL_PARAMS)[0]
    other_file = derivative_identities("e" * 64, "thumbnail", protocol.THUMBNAIL_PARAMS)[0]
    other_params = derivative_identities(
        FINGERPRINT, "thumbnail", protocol.ImageParams(max_edge=401, quality=75),
    )[0]
    assert first == again
    assert len({first.key, other_file.key, other_params.key}) == 3


@pytest.mark.parametrize("failures", [1, 2, 3, 10, 64])
def test_backoff_is_exponential_capped_and_jittered(failures: int) -> None:
    low = retry_delay(failures, base=60, maximum=RETRY_MAX_SECONDS, uniform=lambda: 0.0)
    high = retry_delay(failures, base=60, maximum=RETRY_MAX_SECONDS, uniform=lambda: 1.0)
    expected = min(RETRY_MAX_SECONDS, 60 * 2 ** (failures - 1))
    assert low == timedelta(seconds=expected / 2)
    assert high == timedelta(seconds=expected)


def test_backoff_grows_between_consecutive_failures() -> None:
    delays = [retry_delay(n, uniform=lambda: 1.0) for n in (1, 2, 3)]
    assert delays[0] < delays[1] < delays[2]


def test_build_job_produces_valid_jobs_with_fresh_ids() -> None:
    first = build_job(
        "thumbnail", kind=MediaKind.PHOTO, lane="background", slot="synthetic",
        path=b"album/photo.jpg", identity=(7, 1000, 4096, 1_700_000_000_000_000_000),
        policy=POLICY,
    )
    second = build_job(
        "thumbnail", kind=MediaKind.PHOTO, lane="interactive", slot="synthetic",
        path=b"album/photo.jpg", identity=(7, 1000, 4096, 1_700_000_000_000_000_000),
        policy=POLICY,
    )
    assert first.id != second.id
    assert protocol.decode_job(protocol.encode_job(first)) == first
    assert (first.lane, second.lane) == ("background", "interactive")
    assert first.params == protocol.THUMBNAIL_PARAMS


@pytest.mark.parametrize(
    ("kind", "path", "slot"),
    [
        (MediaKind.PHOTO, b"album/clip.mp4", "synthetic"),
        (MediaKind.PHOTO, b"../photo.jpg", "synthetic"),
        (MediaKind.PHOTO, b"album/photo.jpg", "Bad Slot"),
    ],
)
def test_build_job_refuses_jobs_the_processor_would_refuse(
    kind: str, path: bytes, slot: str,
) -> None:
    with pytest.raises(protocol.ProtocolError):
        build_job(
            "thumbnail", kind=kind, lane="background", slot=slot, path=path,
            identity=(7, 1000, 4096, 0), policy=POLICY,
        )


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ({"probe_state": "ready", "thumbnail_state": "ready", "poster_state": None}, False),
        ({"probe_state": "unsupported", "thumbnail_state": "unsupported",
          "poster_state": None}, True),
        ({"probe_state": "ready", "thumbnail_state": "quarantined", "poster_state": None}, True),
        ({"probe_state": "ready", "thumbnail_state": "ready",
          "poster_state": StageState.QUARANTINED}, True),
        ({"probe_state": "failed", "thumbnail_state": "pending", "poster_state": None}, False),
    ],
)
def test_terminal_assets_block_interactive_work(
    states: dict[str, object], expected: bool,
) -> None:
    assert terminal(states) is expected
