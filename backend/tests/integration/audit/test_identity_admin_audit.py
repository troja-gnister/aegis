from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.identity.admin_services import group_subject_id
from aegis_apps.identity.models import User
from django.contrib import admin
from django.contrib.auth.models import Group, Permission
from django.test import Client
from django.urls import path, reverse

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
REQUEST_ID = "admin_request-1234"
PASSWORD = "Maple-Cloud-Anchor-731!"

urlpatterns = [path("test-admin/", admin.site.urls)]


@pytest.fixture(autouse=True)
def _admin_urls(settings: Any) -> None:
    settings.ROOT_URLCONF = __name__


@pytest.fixture
def actor() -> User:
    return User.objects.create_superuser(
        username="admin-actor",
        email="actor@example.invalid",
        password=PASSWORD,
    )


@pytest.fixture
def client(actor: User) -> Iterator[Client]:
    value = Client()
    value.force_login(actor)
    yield value


def _user_change_data(user: User, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "is_active": "on" if user.is_active else "",
        "is_staff": "on" if user.is_staff else "",
        "is_superuser": "on" if user.is_superuser else "",
        "groups": [str(item) for item in user.groups.values_list("pk", flat=True)],
        "user_permissions": [
            str(item) for item in user.user_permissions.values_list("pk", flat=True)
        ],
        "date_joined_0": user.date_joined.strftime("%Y-%m-%d"),
        "date_joined_1": user.date_joined.strftime("%H:%M:%S"),
        "_save": "Save",
    }
    value.update(overrides)
    return value


def _event_types() -> list[str]:
    return list(AuditEvent.objects.order_by("occurred_at").values_list("event_type", flat=True))


def test_group_subject_id_is_stable_domain_separated_uuid5() -> None:
    first = group_subject_id(41)

    assert first == group_subject_id(41)
    assert first != group_subject_id(42)
    assert first.version == 5
    assert first != uuid.uuid5(uuid.NAMESPACE_URL, "41")


def test_real_user_add_post_hashes_password_once_and_records_one_event(
    client: Client, actor: User
) -> None:
    response = client.post(
        reverse("admin:identity_user_add"),
        {
            "username": "new-administrator",
            "usable_password": "true",
            "password1": PASSWORD,
            "password2": PASSWORD,
            "_save": "Save",
        },
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert response.status_code == 302
    user = User.objects.get(username="new-administrator")
    assert user.check_password(PASSWORD)
    assert not user.check_password(user.password)
    event = AuditEvent.objects.get()
    assert event.event_type == "identity.user.created"
    assert event.actor == actor
    assert event.object_id == user.id
    assert event.metadata == {"subject_id": str(user.id)}


def test_invalid_user_add_and_noop_user_change_emit_no_events(
    client: Client,
) -> None:
    invalid = client.post(
        reverse("admin:identity_user_add"),
        {
            "username": "invalid-user",
            "usable_password": "true",
            "password1": PASSWORD,
            "password2": "different-password",
            "_save": "Save",
        },
        headers={"X-Request-ID": REQUEST_ID},
    )
    user = User.objects.create_user(username="no-op-user", password=PASSWORD)
    unchanged = client.post(
        reverse("admin:identity_user_change", args=[user.pk]),
        _user_change_data(user),
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert invalid.status_code == 200
    assert unchanged.status_code == 302
    assert not User.objects.filter(username="invalid-user").exists()
    assert AuditEvent.objects.count() == 0


def test_real_user_permission_change_records_exactly_one_event(
    client: Client,
) -> None:
    user = User.objects.create_user(username="permission-user", password=PASSWORD)
    permission = Permission.objects.order_by("pk").first()
    assert permission is not None

    response = client.post(
        reverse("admin:identity_user_change", args=[user.pk]),
        _user_change_data(user, user_permissions=[str(permission.pk)]),
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert response.status_code == 302
    assert list(user.user_permissions.values_list("pk", flat=True)) == [permission.pk]
    assert _event_types() == ["identity.user.changed"]


def test_default_password_change_bypass_is_not_routable_or_linked(client: Client) -> None:
    user = User.objects.create_user(username="password-route-user", password=PASSWORD)
    original_hash = user.password
    change_page = client.get(reverse("admin:identity_user_change", args=[user.pk]))

    response = client.post(
        f"/test-admin/identity/user/{user.pk}/password/",
        {
            "password1": "Ocean-Quartz-Bridge-884!",
            "password2": "Ocean-Quartz-Bridge-884!",
            "usable_password": "true",
        },
        headers={"X-Request-ID": REQUEST_ID},
    )

    user.refresh_from_db()
    assert change_page.status_code == 200
    assert b"../password/" not in change_page.content
    assert response.status_code == 404
    assert user.password == original_hash
    assert AuditEvent.objects.count() == 0


def test_real_activation_action_changes_through_service_and_noop_is_silent(
    client: Client,
) -> None:
    user = User.objects.create_user(username="active-user", password=PASSWORD, is_active=True)
    action_data = {
        "action": "deactivate_users",
        "_selected_action": [str(user.pk)],
        "index": "0",
    }

    first = client.post(
        reverse("admin:identity_user_changelist"),
        action_data,
        headers={"X-Request-ID": REQUEST_ID},
    )
    second = client.post(
        reverse("admin:identity_user_changelist"),
        action_data,
        headers={"X-Request-ID": REQUEST_ID},
    )

    user.refresh_from_db()
    assert first.status_code == second.status_code == 302
    assert user.is_active is False
    assert _event_types() == ["identity.user.changed"]


def test_group_create_and_combined_change_use_stable_subject_and_one_event(
    client: Client,
) -> None:
    member = User.objects.create_user(username="member", password=PASSWORD)
    second_member = User.objects.create_user(username="second-member", password=PASSWORD)
    permission = Permission.objects.order_by("pk").first()
    assert permission is not None

    created = client.post(
        reverse("admin:auth_group_add"),
        {"name": "Reviewers", "permissions": [], "members": [str(member.pk)], "_save": "Save"},
        headers={"X-Request-ID": REQUEST_ID},
    )
    group = Group.objects.get(name="Reviewers")
    create_event = AuditEvent.objects.get()
    changed = client.post(
        reverse("admin:auth_group_change", args=[group.pk]),
        {
            "name": "Senior Reviewers",
            "permissions": [str(permission.pk)],
            "members": [str(member.pk), str(second_member.pk)],
            "_save": "Save",
        },
        headers={"X-Request-ID": REQUEST_ID},
    )

    group.refresh_from_db()
    events = list(AuditEvent.objects.order_by("occurred_at"))
    assert created.status_code == changed.status_code == 302
    assert create_event.event_type == "identity.group.created"
    assert all(event.object_id == group_subject_id(group.pk) for event in events)
    assert all(
        event.metadata == {"subject_id": str(group_subject_id(group.pk))} for event in events
    )
    assert [event.event_type for event in events] == [
        "identity.group.created",
        "identity.group.changed",
    ]
    assert set(group.user_set.values_list("pk", flat=True)) == {member.pk, second_member.pk}
    assert list(group.permissions.values_list("pk", flat=True)) == [permission.pk]


def test_group_membership_only_change_is_one_event_and_noop_is_silent(client: Client) -> None:
    member = User.objects.create_user(username="member-only", password=PASSWORD)
    group = Group.objects.create(name="Operators")
    url = reverse("admin:auth_group_change", args=[group.pk])

    changed = client.post(
        url,
        {"name": group.name, "permissions": [], "members": [str(member.pk)], "_save": "Save"},
        headers={"X-Request-ID": REQUEST_ID},
    )
    unchanged = client.post(
        url,
        {"name": group.name, "permissions": [], "members": [str(member.pk)], "_save": "Save"},
        headers={"X-Request-ID": REQUEST_ID},
    )

    assert changed.status_code == unchanged.status_code == 302
    assert _event_types() == ["identity.group.membership.changed"]


def test_admin_audit_failure_rolls_back_model_m2m_and_event(
    client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    member = User.objects.create_user(username="rollback-member", password=PASSWORD)

    def fail_audit(**_values: object) -> None:
        raise RuntimeError("simulated admin audit failure")

    monkeypatch.setattr("aegis_apps.identity.admin_services.record_event", fail_audit)

    with pytest.raises(RuntimeError, match="simulated admin audit failure"):
        client.post(
            reverse("admin:auth_group_add"),
            {
                "name": "Must Roll Back",
                "permissions": [],
                "members": [str(member.pk)],
                "_save": "Save",
            },
            headers={"X-Request-ID": REQUEST_ID},
        )

    assert not Group.objects.filter(name="Must Roll Back").exists()
    assert AuditEvent.objects.count() == 0
