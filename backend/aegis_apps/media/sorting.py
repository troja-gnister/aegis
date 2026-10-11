"""Timeline sort keys for media assets (spec M5; controller decisions of October 11, 2026).

``MediaAsset.sort_at`` orders the timeline, and ``captured_at`` is reported as ``capturedAt``.
Both are timezone-aware datetimes stored in UTC, and the API writes them with ``Z``; the
frontend formats every date in UTC. What the stored value means depends on its source:

- **A capture time** (``captured_at_source`` ``exif`` or ``container``) is the camera's wall
  clock, stored exactly as recorded and *encoded as if it were UTC*:

  - an explicit offset is dropped and the wall-clock time kept, for every source: EXIF
    ``DateTimeOriginal`` with ``OffsetTimeOriginal`` ``2024-05-01T10:30:00+02:00``, and an
    iPhone QuickTime ``creationdate`` ``2024-05-01T10:30:00-07:00``, are both stored as
    ``2024-05-01T10:30:00Z``;
  - a value with ``Z`` or without an offset is kept as is (a container ``creation_time`` is
    normally ``Z``).

  So a photo and a video taken at the same moment on the same phone sort together, and the
  viewer shows the time the camera showed, never a conversion to the viewer's zone.
- **The modification time** (``sort_source = modified``, the fallback) is the UTC time of the
  file's ``mtime_ns``. It is never stored in ``captured_at`` and never labeled a capture date.

Every value is clamped to :data:`SORT_MIN` .. :data:`SORT_MAX` (the processor's capture-year
bounds), so a corrupt timestamp can neither overflow Python's datetime nor sort far outside
the library.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from .enums import CapturedAtSource

SORT_MIN: Final = datetime(1800, 1, 1, tzinfo=UTC)
SORT_MAX: Final = datetime(2200, 12, 31, 23, 59, 59, 999_999, tzinfo=UTC)
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NANOSECONDS: Final = 1_000_000_000
# The processor's captured_at grammar (protocol.Metadata): wall-clock time plus an optional
# "Z" or "+HH:MM" suffix.
_CAPTURED_RE: Final = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(Z|[+-]\d{2}:\d{2})?", re.ASCII,
)
_MAX_OFFSET: Final = timedelta(hours=18)


def _clamped(value: datetime) -> datetime:
    return min(max(value, SORT_MIN), SORT_MAX)


def modified_sort_at(mtime_ns: int | Decimal) -> datetime:
    """The fallback sort key: the UTC time of a modification time in nanoseconds."""
    if isinstance(mtime_ns, bool):
        raise ValueError("invalid modification time")
    if isinstance(mtime_ns, Decimal):
        if not mtime_ns.is_finite() or mtime_ns != mtime_ns.to_integral_value():
            raise ValueError("invalid modification time")
        nanoseconds = int(mtime_ns)
    elif isinstance(mtime_ns, int):
        nanoseconds = mtime_ns
    else:
        raise ValueError("invalid modification time")
    seconds, remainder = divmod(nanoseconds, _NANOSECONDS)
    minimum = (SORT_MIN - _EPOCH).total_seconds()
    maximum = (SORT_MAX - _EPOCH).total_seconds()
    if seconds < minimum:
        return SORT_MIN
    if seconds > maximum:
        return SORT_MAX
    return _clamped(_EPOCH + timedelta(seconds=seconds, microseconds=remainder // 1000))


def captured_sort_at(captured_at: object, source: object) -> datetime | None:
    """The stored capture time for a probed ``captured_at`` string, or ``None`` if invalid.

    See the module docstring: the wall clock is kept, encoded as UTC; any offset is dropped.
    """
    if not isinstance(captured_at, str) or source not in CapturedAtSource.values:
        return None
    match = _CAPTURED_RE.fullmatch(captured_at)
    if match is None:
        return None
    try:
        wall = datetime(
            int(match[1]), int(match[2]), int(match[3]),
            int(match[4]), int(match[5]), int(match[6]), tzinfo=UTC,
        )
    except ValueError:
        return None
    suffix = match[7]
    if suffix not in (None, "Z"):
        hours, minutes = int(suffix[1:3]), int(suffix[4:6])
        if minutes >= 60 or timedelta(hours=hours, minutes=minutes) > _MAX_OFFSET:
            return None
    return _clamped(wall)
