from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.identity.models import User
from aegis_apps.roots.admin import RootAdminForm
from aegis_apps.roots.models import Root, RootGrant
from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.forms import ChoiceField, Select
from django.test import Client, RequestFactory
from django.urls import reverse

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
REQUEST_ID = "root_admin_request_1234"
PASSWORD = "Admin-Root-Test-731!"


def _configure_manifest(
    *, path: Path, monkeypatch: pytest.MonkeyPatch, mode: str = "read_write"
) -> None:
    payload = {
        "version": 1,
        "generatedAt": "2026-09-02T12:34:56Z",
        "slots": [
            {
                "slotId": "photos",
                "containerPath": "/srv/aegis/roots/photos",
                "mode": mode,
                "filesystemId": 123,
                "rootInode": 456,
                "expectedIdentity": "remote:secret-host.invalid:/private/photos",
                "mountFingerprint": "a" * 64,
            }
        ],
    }
    raw = (json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    os.chown(path, os.geteuid(), os.getegid())
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(path))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", hashlib.sha256(raw).hexdigest())


@pytest.fixture
def actor() -> User:
    return User.objects.create_superuser(
        username="root-admin-actor",
        email="actor@example.invalid",
        password=PASSWORD,
    )


@pytest.fixture
def client(actor: User) -> Iterator[Client]:
    value = Client()
    value.force_login(actor)
    yield value


def test_root_admin_exposes_internal_fields_read_only_and_disables_unsafe_actions(
    actor: User,
) -> None:
    root_admin = admin.site._registry[Root]
    grant_admin = admin.site._registry[RootGrant]
    request = RequestFactory().get("/admin/roots/root/")
    request.user = actor

    assert {"authorization_epoch", "capabilities"} <= set(
        root_admin.get_readonly_fields(request)
    )
    assert root_admin.has_delete_permission(request) is False
    assert "delete_selected" not in root_admin.get_actions(request)
    assert "delete_selected" not in grant_admin.get_actions(request)
    assert {"root", "user", "group"} <= set(
        grant_admin.get_readonly_fields(request, RootGrant(id=None))
    )


def test_root_admin_form_offers_only_live_manifest_slots_and_no_text_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)

    valid = RootAdminForm()
    valid_slot = cast(ChoiceField, valid.fields["slot_id"])
    valid_select = cast(Select, valid_slot.widget)

    assert list(valid_select.choices) == [("photos", "photos")]

    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", "b" * 64)
    invalid = RootAdminForm()
    invalid_slot = cast(ChoiceField, invalid.fields["slot_id"])
    invalid_select = cast(Select, invalid_slot.widget)
    assert list(invalid_select.choices) == []
    assert invalid_slot.widget.input_type == "select"


def test_root_admin_create_and_change_route_through_one_audited_service_each(
    client: Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    created_response = client.post(
        reverse("admin:roots_root_add"),
        {
            "slot_id": "photos",
            "display_name": "Photos",
            "mode": "read_only",
            "active": "",
            "_save": "Save",
        },
        headers={"X-Request-ID": REQUEST_ID},
    )

    root = Root.objects.get()
    assert created_response.status_code == 302
    assert root.capabilities == {}
    assert root.authorization_epoch == 0
    assert list(AuditEvent.objects.values_list("event_type", flat=True)) == [
        "authorization.root.created"
    ]
    assert AuditEvent.objects.get().request_id == REQUEST_ID
    assert LogEntry.objects.count() == 0

    changed_response = client.post(
        reverse("admin:roots_root_change", args=[root.pk]),
        {
            "slot_id": "photos",
            "display_name": "Family Photos",
            "mode": "read_only",
            "active": "on",
            "_save": "Save",
        },
        headers={"X-Request-ID": REQUEST_ID},
    )

    root.refresh_from_db()
    assert changed_response.status_code == 302
    assert root.display_name == "Family Photos"
    assert root.active is True
    assert root.authorization_epoch == 1
    assert AuditEvent.objects.count() == 2
    assert set(AuditEvent.objects.values_list("event_type", flat=True)) == {
        "authorization.root.created",
        "authorization.root.changed",
    }
    assert LogEntry.objects.count() == 0


def test_grant_admin_create_and_delete_use_audited_services_without_bulk_bypass(
    client: Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_manifest(path=tmp_path / "manifest.json", monkeypatch=monkeypatch)
    root = Root.objects.create(
        slot_id="photos",
        display_name="Photos",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    subject = User.objects.create_user(username="grant-admin-subject")

    created = client.post(
        reverse("admin:roots_rootgrant_add"),
        {
            "root": str(root.pk),
            "user": str(subject.pk),
            "group": "",
            "permissions": "1",
            "_save": "Save",
        },
        headers={"X-Request-ID": REQUEST_ID},
    )

    grant = RootGrant.objects.get()
    assert created.status_code == 302
    assert grant.permissions == 1
    assert list(AuditEvent.objects.values_list("event_type", flat=True)) == [
        "authorization.user.grant.changed"
    ]
    assert LogEntry.objects.count() == 0

    deleted = client.post(
        reverse("admin:roots_rootgrant_delete", args=[grant.pk]),
        {"post": "yes"},
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert deleted.status_code == 302
    assert not RootGrant.objects.filter(pk=grant.pk).exists()
    assert list(
        AuditEvent.objects.order_by("occurred_at").values_list("event_type", flat=True)
    ) == [
        "authorization.user.grant.changed",
        "authorization.grant.removed",
    ]
    assert LogEntry.objects.count() == 0


def test_invalid_manifest_admin_page_has_no_path_or_manifest_detail(
    client: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = "/private/secret/catalog.json"
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", secret_path)
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", "0" * 64)

    response = client.get(reverse("admin:roots_root_add"))

    assert response.status_code == 200
    body = response.content.decode()
    assert secret_path not in body
    assert "0" * 64 not in body
    assert 'name="slot_id"' in body
