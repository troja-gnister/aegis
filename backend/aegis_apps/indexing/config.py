from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ScanPolicy:
    interval_seconds: int
    idle_timeout_seconds: int
    batch_records: int
    readers: int

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> ScanPolicy:
        limits = (
            ("AEGIS_SCAN_INTERVAL_SECONDS", 3600, 60, 604800),
            ("AEGIS_SCAN_IDLE_TIMEOUT_SECONDS", 120, 30, 3600),
            ("AEGIS_SCAN_BATCH_RECORDS", 500, 100, 2000),
            ("AEGIS_SCAN_READERS", 2, 1, 4),
        )
        result: list[int] = []
        for name, default, lower, upper in limits:
            raw = values.get(name, str(default))
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 6:
                raise ValueError("invalid scan policy")
            value = int(raw)
            if not lower <= value <= upper:
                raise ValueError("invalid scan policy")
            result.append(value)
        return cls(*result)
