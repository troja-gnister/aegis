"""Timeline sort keys: capture times keep their wall clock (offsets dropped); mtime is UTC."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from aegis_apps.media.sorting import SORT_MAX, SORT_MIN, captured_sort_at, modified_sort_at


@pytest.mark.parametrize(
    ("captured", "expected"),
    [
        # OffsetTimeOriginal present: the camera's wall clock is kept, the offset dropped.
        ("2024-05-01T10:30:00+02:00", datetime(2024, 5, 1, 10, 30, tzinfo=UTC)),
        ("2024-05-01T10:30:00-07:30", datetime(2024, 5, 1, 10, 30, tzinfo=UTC)),
        # No offset: the wall clock, encoded as if it were UTC.
        ("2024-05-01T10:30:00", datetime(2024, 5, 1, 10, 30, tzinfo=UTC)),
        ("2024-05-01T23:59:59Z", datetime(2024, 5, 1, 23, 59, 59, tzinfo=UTC)),
    ],
)
def test_exif_capture_time_is_the_wall_clock_encoded_as_utc(
    captured: str, expected: datetime,
) -> None:
    assert captured_sort_at(captured, "exif") == expected


@pytest.mark.parametrize(
    ("captured", "expected"),
    [
        # A container creation_time is normally "Z": kept as is.
        ("2024-05-01T10:30:00Z", datetime(2024, 5, 1, 10, 30, tzinfo=UTC)),
        ("2024-05-01T10:30:00", datetime(2024, 5, 1, 10, 30, tzinfo=UTC)),
        # An iPhone QuickTime creationdate is local time with an offset: the offset is
        # dropped and the local wall clock kept.
        ("2024-05-01T10:30:00-07:00", datetime(2024, 5, 1, 10, 30, tzinfo=UTC)),
        ("2024-05-01T22:00:00+05:30", datetime(2024, 5, 1, 22, 0, tzinfo=UTC)),
    ],
)
def test_container_times_keep_their_wall_clock(captured: str, expected: datetime) -> None:
    assert captured_sort_at(captured, "container") == expected


@pytest.mark.parametrize(
    "captured",
    [
        "2024-05-01T10:30:00", "2024-05-01T10:30:00Z", "2024-05-01T10:30:00+02:00",
        "2024-05-01T10:30:00-07:00",
    ],
)
def test_a_photo_and_a_video_of_the_same_moment_sort_together(captured: str) -> None:
    assert captured_sort_at(captured, "exif") == captured_sort_at(captured, "container")


@pytest.mark.parametrize(
    ("captured", "source"),
    [
        ("2024-02-30T10:30:00", "exif"),
        ("2024-05-01 10:30:00", "exif"),
        ("2024-05-01T10:30:00+02:60", "container"),
        ("2024-05-01T10:30:00+19:00", "container"),
        ("2024-05-01T10:30:00+19:00", "exif"),
        ("2024-05-01T10:30:00.5Z", "exif"),
        ("2024-05-01T10:30:00", "camera"),
        (None, "exif"),
        (1714559400, "exif"),
    ],
)
def test_invalid_capture_times_give_no_capture_key(captured: object, source: object) -> None:
    assert captured_sort_at(captured, source) is None


def test_offsets_never_move_a_value_across_the_library_bounds() -> None:
    assert captured_sort_at("1800-01-01T00:00:00+01:00", "container") == SORT_MIN
    assert captured_sort_at("2200-12-31T23:59:59-01:00", "exif") == SORT_MAX.replace(
        microsecond=0,
    )
    assert captured_sort_at("1799-12-31T23:59:59Z", "exif") == SORT_MIN
    assert captured_sort_at("2201-01-01T00:00:00Z", "container") == SORT_MAX


@pytest.mark.parametrize(
    ("mtime_ns", "expected"),
    [
        (0, datetime(1970, 1, 1, tzinfo=UTC)),
        (1_714_559_400_123_456_789, datetime(2024, 5, 1, 10, 30, 0, 123_456, tzinfo=UTC)),
        (Decimal("1714559400000000000"), datetime(2024, 5, 1, 10, 30, tzinfo=UTC)),
        (-1, datetime(1969, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)),
    ],
)
def test_the_fallback_is_the_utc_modification_time(
    mtime_ns: int | Decimal, expected: datetime,
) -> None:
    assert modified_sort_at(mtime_ns) == expected


@pytest.mark.parametrize(
    ("mtime_ns", "expected"),
    [
        (-(1 << 127), SORT_MIN),
        ((1 << 127) - 1, SORT_MAX),
        # About 1653: before the library range.
        (-(10**19), SORT_MIN),
        # About 2286: after it.
        (10**19, SORT_MAX),
    ],
)
def test_modification_times_outside_the_range_are_clamped(
    mtime_ns: int, expected: datetime,
) -> None:
    assert modified_sort_at(mtime_ns) == expected


@pytest.mark.parametrize("value", [True, 1.5, "1", Decimal("1.5"), Decimal("NaN"), None])
def test_modification_time_must_be_an_integer(value: object) -> None:
    with pytest.raises(ValueError):
        modified_sort_at(value)  # type: ignore[arg-type]
