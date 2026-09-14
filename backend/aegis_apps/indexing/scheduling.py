from uuid import UUID

from .database import _DIGEST, _call, _uuid


def schedule_root_scan(root_id: UUID, worker_id: str, manifest_identity: str) -> UUID | None:
    if not isinstance(root_id, UUID):
        raise ValueError("invalid scan identifier")
    _uuid(worker_id)
    if not isinstance(manifest_identity, str) or not _DIGEST.fullmatch(manifest_identity):
        raise ValueError("invalid scan manifest")
    result = _call("schedule", [root_id, worker_id, manifest_identity])
    return None if result is None else UUID(str(result))
