"""Safe stored index-status projection for catalog clients."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime


def _timestamp(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError("invalid index status")
    return value.isoformat()


def index_status_payload(
    row: Mapping[str, object] | None,
    *,
    state: str,
) -> dict[str, object]:
    if row is None:
        return {
            "state": state,
            "generation": "0",
            "observedEntries": "0",
            "completedDirectories": "0",
            "degradedDirectories": "0",
            "updatedAt": None,
            "lastCompletedAt": None,
        }
    next_generation = row["next_generation"]
    if type(next_generation) is not int:
        raise ValueError("invalid index status")
    updated_at = row["updated_at"]
    completed_at = row["last_completed_at"]
    return {
        "state": state,
        "generation": str(max(0, next_generation - 1)),
        "observedEntries": str(row["observed_entries"]),
        "completedDirectories": str(row["completed_directories"]),
        "degradedDirectories": str(row["degraded_directories"]),
        "updatedAt": _timestamp(updated_at),
        "lastCompletedAt": _timestamp(completed_at),
    }
