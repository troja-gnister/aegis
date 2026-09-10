from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Final, Literal, cast

from aegisctl.mounts import attest_mounts
from django.conf import settings
from django.db import connection

from aegis_apps.common.database_privileges import current_database_login
from aegis_apps.operations.models import UNCONFIGURED_MANIFEST_IDENTITY
from aegis_apps.operations.selectors import (
    authoritative_database_time,
    current_schema_identity,
    worker_role_states,
)
from aegis_apps.roots.manifest import configured_manifest

RuntimeRole = Literal["web", "operations", "indexer", "media"]
RUNTIME_ROLES: Final = ("web", "operations", "indexer", "media")
EXPECTED_DATABASE_LOGINS: Final = {
    "web": "aegis_web",
    "operations": "aegis_operations",
    "indexer": "aegis_indexer",
    "media": "aegis_media",
}
MAX_DEPLOYMENT_METADATA_BYTES: Final = 4096


class RuntimeBoundaryError(RuntimeError):
    """The running process does not match its deployed security boundary."""


def _runtime_role(value: object) -> RuntimeRole:
    if isinstance(value, str) and value in RUNTIME_ROLES:
        return cast(RuntimeRole, value)
    raise RuntimeBoundaryError("runtime role is invalid")


def deployed_database_metadata() -> dict[str, str]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_catalog.obj_description(
                'public'::pg_catalog.regnamespace,
                'pg_namespace'
            )
            """
        )
        row = cursor.fetchone()
    if (
        row is None
        or not isinstance(row[0], str)
        or not 1 <= len(row[0].encode("utf-8")) <= MAX_DEPLOYMENT_METADATA_BYTES
    ):
        raise RuntimeBoundaryError("database deployment metadata is unavailable")
    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        raise RuntimeBoundaryError("database deployment metadata is invalid") from None
    if (
        not isinstance(payload, dict)
        or set(payload) != {"application", "releaseId", "schemaIdentity"}
        or any(not isinstance(value, str) for value in payload.values())
    ):
        raise RuntimeBoundaryError("database deployment metadata is invalid")
    return cast(dict[str, str], payload)


def probe_local_web() -> None:
    public_url = urllib.parse.urlsplit(settings.AEGIS_PUBLIC_URL)
    if not public_url.netloc:
        raise RuntimeBoundaryError("web runtime authority is invalid")
    request = urllib.request.Request(
        "http://127.0.0.1:8000/health/live",
        headers={"Host": public_url.netloc, "X-Forwarded-Proto": "https"},
    )
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            if response.status != 200:
                raise RuntimeBoundaryError("web runtime is unavailable")
    except (OSError, urllib.error.URLError):
        raise RuntimeBoundaryError("web runtime is unavailable") from None


def check_runtime_boundary(role: object, *, require_http: bool = False) -> None:
    runtime_role = _runtime_role(role)
    if require_http and runtime_role != "web":
        raise RuntimeBoundaryError("HTTP probe is invalid for this runtime role")

    expected_process_role = None if runtime_role == "web" else runtime_role
    if expected_process_role != settings.AEGIS_PROCESS_ROLE:
        raise RuntimeBoundaryError("configured process role does not match")
    if current_database_login() != EXPECTED_DATABASE_LOGINS[runtime_role]:
        raise RuntimeBoundaryError("database login does not match runtime role")

    schema_identity = current_schema_identity()
    expected_metadata = {
        "application": "aegis",
        "releaseId": settings.AEGIS_RELEASE_ID,
        "schemaIdentity": schema_identity,
    }
    if deployed_database_metadata() != expected_metadata:
        raise RuntimeBoundaryError("database deployment metadata does not match")

    manifest = configured_manifest()
    manifest_identity = UNCONFIGURED_MANIFEST_IDENTITY if manifest is None else manifest.digest
    if runtime_role != "web":
        if manifest is not None:
            attest_mounts(manifest, runtime_role)
        states = worker_role_states(
            roles=(runtime_role,),
            release_id=settings.AEGIS_RELEASE_ID,
            schema_identity=schema_identity,
            manifest_identity=manifest_identity,
            now=authoritative_database_time(),
            freshness_seconds=settings.AEGIS_WORKER_HEARTBEAT_FRESH_SECONDS,
        )
        if states != {runtime_role: "healthy"}:
            raise RuntimeBoundaryError("worker heartbeat is incompatible")

    if require_http:
        probe_local_web()
