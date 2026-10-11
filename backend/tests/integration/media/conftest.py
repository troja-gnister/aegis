"""A protected synthetic catalog with media assets, real sessions and web-role requests."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.indexing.models import IndexDeployment
from aegis_apps.media.keys import derivative_key, fingerprint, params_digest
from aegis_apps.media.links import fingerprint_token
from aegis_apps.media.models import MediaAsset, MediaDerivative
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.test import Client

PROCESSOR_VERSION = "aegis-media-processor/1"
SORT_AT = datetime(2024, 5, 1, 10, 30, tzinfo=UTC)
ALL_MEDIA = Permission.BROWSE | Permission.PREVIEW | Permission.EXPORT


@dataclass(slots=True)
class MediaCatalog:
    """Builders and request helpers over ``api_catalog`` (direct, group and admin users)."""

    api: Any
    browse: Any

    @property
    def root(self) -> Root:
        return Root.objects.get(pk=self.api.root.pk)

    @property
    def client(self) -> Client:
        client: Client = self.api.client
        return client

    def get(self, path: str, *, client: Client | None = None, **kwargs: Any) -> Any:
        with self.api.database.as_django_role("aegis_web"):
            return (client or self.api.client).get(path, **kwargs)

    def post(
        self, path: str, data: object = None, *, client: Client | None = None,
        csrf: bool = True, **kwargs: Any,
    ) -> Any:
        selected = client or self.api.client
        headers = dict(kwargs.pop("headers", {}))
        if csrf:
            headers["X-CSRFToken"] = selected.cookies["csrftoken"].value
        with self.api.database.as_django_role("aegis_web"):
            return selected.post(
                path, data=data, content_type="application/json", headers=headers, **kwargs,
            )

    def namespace(self, client: Client | None = None) -> str:
        response = self.get("/api/v1/auth/session", client=client)
        assert response.status_code == 200
        value = response.json()["cacheNamespace"]
        assert isinstance(value, str)
        return value

    def epoch(self) -> int:
        return self.root.authorization_epoch

    def grant(self, permissions: Permission | int, *, user: Any = None) -> None:
        """Set a direct grant without advancing epochs (sessions stay current)."""
        RootGrant.objects.filter(root_id=self.api.root.pk, user=user or self.api.user).update(
            permissions=int(permissions),
        )

    def entry(
        self, raw: bytes, *, parent: CatalogEntry | None = None, kind: str = "file",
        **kwargs: Any,
    ) -> CatalogEntry:
        values: dict[str, Any] = {"device": 7, "inode": 1000, "size": 4096,
                                  "mtime_ns": 1_700_000_000_000_000_000}
        values.update(kwargs)
        if kind == "directory":
            values = {key: None for key in values} | kwargs
        entry: CatalogEntry = self.browse.entry(raw, parent=parent, kind=kind, **values)
        return entry

    def asset(self, entry: CatalogEntry, **overrides: Any) -> MediaAsset:
        assert entry.device is not None and entry.inode is not None
        assert entry.size is not None and entry.mtime_ns is not None
        kind = overrides.get("kind", "photo")
        values: dict[str, Any] = {
            "entry": entry,
            "root_id": entry.root_id,
            "kind": kind,
            "device": entry.device,
            "inode": entry.inode,
            "size": entry.size,
            "mtime_ns": entry.mtime_ns,
            "fingerprint": fingerprint(
                entry.root_id, entry.id, entry.device, entry.inode, entry.size,
                entry.mtime_ns,
            ),
            "probe_state": "ready",
            "thumbnail_state": "ready",
            "poster_state": "ready" if kind == "video" else None,
            "width": 4032 if kind == "photo" else 1920,
            "height": 3024 if kind == "photo" else 1080,
            "sort_at": SORT_AT,
            "processor_version": PROCESSOR_VERSION,
        }
        if kind == "video":
            values.update(
                duration_ms=12_345, video_codec="h264", audio_codec="aac", container="mp4",
            )
        values.update(overrides)
        return MediaAsset.objects.create(**values)

    def derivative(
        self, asset: MediaAsset, kind: str = "thumbnail", **overrides: Any,
    ) -> MediaDerivative:
        source = str(overrides.pop("fingerprint", asset.fingerprint))
        digest = params_digest({"kind": kind, "variant": str(overrides.pop("variant", 1))})
        state = overrides.get("state", "ready")
        ready_shape: dict[str, Any] = (
            {"bytes": 2048, "width": 400, "height": 300} if state in ("ready", "stale") else {}
        )
        values: dict[str, Any] = {
            "asset": asset,
            "kind": kind,
            "fingerprint": source,
            "params_digest": digest,
            "key": derivative_key(source, PROCESSOR_VERSION, kind, digest),
            "extension": "mp4" if kind == "transcode" else "webp",
            "state": state,
            "evictable": kind in ("preview", "transcode"),
            **ready_shape,
        }
        values.update(overrides)
        return MediaDerivative.objects.create(**values)

    def derivative_path(
        self, entry: CatalogEntry, kind: str = "thumbnail", *, fingerprint: str | None = None,
        namespace: str | None = None, epoch: int | None = None,
    ) -> str:
        """A delivery URL; *fingerprint* is a source fingerprint, sent as its token."""
        source = fingerprint or MediaAsset.objects.get(pk=entry.pk).fingerprint
        return (
            f"/media/v1/derivatives/{entry.pk}/{kind}?f={fingerprint_token(source)}"
            f"&n={namespace or self.namespace()}&e={self.epoch() if epoch is None else epoch}"
        )

    def original_path(
        self, entry: CatalogEntry, disposition: str = "inline", *,
        namespace: str | None = None, epoch: int | None = None,
    ) -> str:
        return (
            f"/media/v1/originals/{entry.pk}?d={disposition}"
            f"&n={namespace or self.namespace()}&e={self.epoch() if epoch is None else epoch}"
        )


@pytest.fixture
def media_catalog(api_catalog: Any, browse_fixture: Any) -> MediaCatalog:
    catalog = MediaCatalog(api_catalog, browse_fixture)
    catalog.grant(ALL_MEDIA)
    return catalog


def other_root() -> Root:
    return Root.objects.create(
        slot_id=f"foreign-{uuid.uuid4().hex[:12]}",
        display_name="Foreign",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )


SECOND_ROOT_PASSWORD = "second-root-real-login-password"


@dataclass(slots=True)
class SecondRoot:
    """A second root that is in the manifest and bound, granted only to another user."""

    root: Root
    owner_client: Client
    entry: CatalogEntry
    asset: MediaAsset
    derivative: MediaDerivative


def _rebind_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, slot_ids: list[str],
) -> None:
    raw = json.dumps({
        "version": 1, "generatedAt": "2026-10-11T12:00:00Z", "slots": [
            {
                "slotId": slot_id,
                "containerPath": f"/srv/aegis/roots/{slot_id}",
                "mode": "read_only", "filesystemId": 100 + index, "rootInode": 200 + index,
                "expectedIdentity": f"remote:synthetic.invalid:/media-{index}",
                "mountFingerprint": f"{index + 10:x}" * 64,
            }
            for index, slot_id in enumerate(slot_ids)
        ],
    }).encode()
    path = tmp_path / "two-slot-manifest.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(path))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", digest)
    IndexDeployment.objects.filter(pk=1).update(
        manifest_identity=digest, slot_ids=sorted(slot_ids),
    )


@pytest.fixture
def second_root(
    media_catalog: MediaCatalog,
    entry_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> SecondRoot:
    root = Root.objects.create(
        slot_id=f"second-{uuid.uuid4().hex[:12]}",
        display_name="Second root",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    anchor = CatalogEntry.objects.create(
        root=root, raw_name=b"", display_name="", name_key=b"", kind="directory",
    )
    _rebind_manifest(monkeypatch, tmp_path, [media_catalog.root.slot_id, root.slot_id])
    owner = User.objects.create_user(
        username=f"second-owner-{uuid.uuid4().hex[:8]}", password=SECOND_ROOT_PASSWORD,
    )
    RootGrant.objects.create(root=root, user=owner, permissions=int(ALL_MEDIA))
    entry: CatalogEntry = entry_factory(
        root=root, raw=b"victim.jpg", parent=anchor, size=4096,
        mtime_ns=1_700_000_000_000_000_000, device=7, inode=2000,
    )
    CatalogEntry.objects.filter(pk=entry.pk).update(
        source_parent_revision=anchor.source_revision,
    )
    entry.refresh_from_db()
    asset = media_catalog.asset(entry)
    derivative = media_catalog.derivative(asset)
    client = Client(enforce_csrf_checks=True)
    with media_catalog.api.database.as_django_role("aegis_web"):
        csrf = client.get("/api/v1/auth/csrf")
        login = client.post(
            "/api/v1/auth/login",
            {"username": owner.username, "password": SECOND_ROOT_PASSWORD},
            content_type="application/json",
            headers={"X-CSRFToken": csrf.json()["csrfToken"]},
        )
    assert login.status_code == 200
    return SecondRoot(root, client, entry, asset, derivative)
