"""Allowlisted browser DTOs. Source identity is never serialized."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from aegis_apps.indexing.models import RootIndexState


def entry_summary(row: dict[str, Any], *, available: bool) -> dict[str, object]:
    state = row["source_state"]
    if not available and state == "present":
        state = "inaccessible"
    return {
        "id": str(row["id"]), "rootId": str(row["root_id"]),
        "displayName": row["display_name"], "kind": row["kind"],
        "typeHint": row["type_hint"],
        "size": None if row["size"] is None else str(row["size"]),
        "modifiedNs": None if row["mtime_ns"] is None else str(row["mtime_ns"]),
        "sourceState": state, "version": str(row["catalog_version"]),
    }


def index_status(root_id: UUID, *, available: bool) -> dict[str, object]:
    row = RootIndexState.objects.filter(root_id=root_id).values(
        "status", "next_generation", "observed_entries", "completed_directories",
        "degraded_directories", "updated_at", "last_completed_at",
    ).first()
    return {
        "state": "unavailable" if not available else row["status"] if row else "not_indexed",
        "generation": str(max(0, row["next_generation"] - 1)) if row else "0",
        "observedEntries": str(row["observed_entries"]) if row else "0",
        "completedDirectories": str(row["completed_directories"]) if row else "0",
        "degradedDirectories": str(row["degraded_directories"]) if row else "0",
        "updatedAt": row["updated_at"].isoformat() if row and row["updated_at"] else None,
        "lastCompletedAt": (
            row["last_completed_at"].isoformat() if row and row["last_completed_at"] else None
        ),
    }
