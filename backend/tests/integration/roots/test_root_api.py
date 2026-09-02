from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from aegis_apps.identity.models import User
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.contrib.auth.models import Group
from django.test import Client

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
CAPABILITY_SECRET = "capability-secret-must-never-escape"
IDENTITY_SECRET = "remote:secret-host.invalid:/private/library"


def _configure_manifest(
    *,
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slot_ids: tuple[str, ...] = ("photos",),
) -> str:
    slots = [
        {
            "slotId": slot_id,
            "containerPath": f"/srv/aegis/roots/{slot_id}",
            "mode": "read_write",
            "filesystemId": 100 + index,
            "rootInode": 200 + index,
            "expectedIdentity": (
                IDENTITY_SECRET if index == 0 else f"remote:other.invalid:/opaque/{index}"
            ),
            "mountFingerprint": f"{index + 1:x}" * 64,
        }
        for index, slot_id in enumerate(slot_ids)
    ]
    payload = {
        "version": 1,
        "generatedAt": "2026-09-02T12:34:56Z",
        "slots": slots,
    }
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    os.chown(path, os.geteuid(), os.getegid())
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(path))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", digest)
    return digest


def _root(
    *,
    slot_id: str,
    display_name: str,
    active: bool = True,
    epoch: int = 0,
    capabilities: dict[str, object] | None = None,
) -> Root:
    return Root.objects.create(
        slot_id=slot_id,
        display_name=display_name,
        mode=Root.Mode.READ_ONLY,
        active=active,
        authorization_epoch=epoch,
        capabilities=capabilities or {},
    )


def test_root_list_requires_authentication_and_never_caches() -> None:
    response = Client().get("/api/v1/roots")

    assert response.status_code == 401
    assert response.json() == {
        "type": "authentication_required",
        "title": "Authentication required",
    }
    assert response.headers["Cache-Control"] == "private, no-store"


def test_root_list_returns_empty_for_intentionally_unconfigured_mounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AEGIS_MOUNT_MANIFEST", raising=False)
    monkeypatch.delenv("AEGIS_MOUNT_MANIFEST_SHA256", raising=False)
    user = User.objects.create_user(username="unconfigured-user")
    root = _root(slot_id="database-only", display_name="Database only")
    RootGrant.objects.create(root=root, user=user, permissions=Permission.BROWSE)
    client = Client()
    client.force_login(user)

    response = client.get("/api/v1/roots")

    assert response.status_code == 200
    assert response.json() == {"roots": []}
    assert response.headers["Cache-Control"] == "private, no-store"


def test_invalid_configured_manifest_returns_generic_nondisclosing_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "/private/secret/mount-catalog.json"
    secret_digest = "d" * 64
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", secret_path)
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", secret_digest)
    user = User.objects.create_user(username="invalid-manifest-user")
    client = Client()
    client.force_login(user)

    response = client.get("/api/v1/roots")

    assert response.status_code == 503
    assert response.json() == {
        "type": "root_catalog_unavailable",
        "title": "Root catalog unavailable",
    }
    assert response.headers["Cache-Control"] == "private, no-store"
    body = response.content.decode()
    assert secret_path not in body
    assert secret_digest not in body


def test_root_list_returns_only_manifest_backed_browsable_exact_shells(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_manifest(
        path=tmp_path / "manifest.json",
        monkeypatch=monkeypatch,
        slot_ids=("photos", "alpha", "private-secret-slot", "inactive", "preview"),
    )
    user = User.objects.create_user(username="root-api-user")
    group = Group.objects.create(name="private-group-name")
    user.groups.add(group)
    photos = _root(
        slot_id="photos",
        display_name="Photos",
        epoch=7,
        capabilities={"secret": CAPABILITY_SECRET, "path": "/srv/private"},
    )
    alpha = _root(slot_id="alpha", display_name="alpha")
    secret_slot = _root(slot_id="private-secret-slot", display_name="Catalog")
    outside = _root(slot_id="outside-manifest", display_name="Outside")
    inactive = _root(slot_id="inactive", display_name="Inactive", active=False)
    preview = _root(slot_id="preview", display_name="Preview only")
    RootGrant.objects.create(root=photos, user=user, permissions=255)
    RootGrant.objects.create(root=alpha, group=group, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=secret_slot, user=user, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=outside, user=user, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=inactive, user=user, permissions=Permission.BROWSE)
    RootGrant.objects.create(root=preview, user=user, permissions=Permission.PREVIEW)
    client = Client()
    user.refresh_from_db()
    client.force_login(user)

    response = client.get("/api/v1/roots")

    assert response.status_code == 200
    assert response.json() == {
        "roots": [
            {
                "id": str(alpha.id),
                "displayName": "alpha",
                "mode": "read_only",
                "permissions": ["browse"],
                "authorizationEpoch": 0,
            },
            {
                "id": str(secret_slot.id),
                "displayName": "Catalog",
                "mode": "read_only",
                "permissions": ["browse"],
                "authorizationEpoch": 0,
            },
            {
                "id": str(photos.id),
                "displayName": "Photos",
                "mode": "read_only",
                "permissions": [
                    "browse",
                    "preview",
                    "export",
                    "create",
                    "organize",
                    "copy",
                    "delete_restore",
                    "root_admin",
                ],
                "authorizationEpoch": 7,
            },
        ]
    }
    assert response.headers["Cache-Control"] == "private, no-store"
    assert set(response.json()["roots"][0]) == {
        "id",
        "displayName",
        "mode",
        "permissions",
        "authorizationEpoch",
    }
    body = response.content.decode()
    for secret in (
        CAPABILITY_SECRET,
        IDENTITY_SECRET,
        "private-secret-slot",
        "private-group-name",
        "root-api-user",
        "/srv/private",
        "/srv/aegis/roots",
    ):
        assert secret not in body


def test_superuser_without_product_grant_has_no_root_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    superuser = User.objects.create_superuser(username="no-bypass-superuser")
    _root(slot_id="photos", display_name="Photos")
    client = Client()
    client.force_login(superuser)

    response = client.get("/api/v1/roots")

    assert response.status_code == 200
    assert response.json() == {"roots": []}
    assert response.headers["Cache-Control"] == "private, no-store"
