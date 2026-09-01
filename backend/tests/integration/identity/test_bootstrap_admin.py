from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from aegis.config import ConfigurationError
from aegis_apps.audit.models import AuditEvent
from aegis_apps.identity.services import BootstrapResult, bootstrap_admin
from django.contrib.auth import get_user_model
from django.db import close_old_connections

pytestmark = pytest.mark.integration

VALID_REQUEST_ID = "0123456789abcdef0123456789abcdef"
VALID_PASSWORD = "Pine-River-Copper-Lantern-731!"


@pytest.mark.django_db(transaction=True)
def test_bootstrap_admin_creates_active_argon2_superuser() -> None:
    result = bootstrap_admin(
        username="\uff21dmin",
        email="admin@EXAMPLE.INVALID",
        password=VALID_PASSWORD,
        request_id=VALID_REQUEST_ID,
    )

    user = get_user_model().objects.get(pk=result.user_id)
    assert result.created is True
    assert user.username == "Admin"
    assert user.email == "admin@example.invalid"
    assert user.is_active is True
    assert user.is_staff is True
    assert user.is_superuser is True
    assert user.password.startswith("argon2$")


@pytest.mark.django_db(transaction=True)
def test_bootstrap_admin_is_idempotent_and_does_not_rotate_password() -> None:
    first = bootstrap_admin(
        username="admin",
        email="admin@example.invalid",
        password=VALID_PASSWORD,
        request_id=VALID_REQUEST_ID,
    )
    user = get_user_model().objects.get(pk=first.user_id)
    original_hash = user.password

    second = bootstrap_admin(
        username="admin",
        email="admin@example.invalid",
        password="Quartz-Harbor-Orbit-984!",
        request_id="abcdef0123456789abcdef0123456789",
    )
    user.refresh_from_db()

    assert first.created is True
    assert second == BootstrapResult(user_id=first.user_id, created=False)
    assert user.password == original_hash


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("email", "other@example.invalid"),
        ("is_active", False),
        ("is_staff", False),
        ("is_superuser", False),
    ],
)
def test_bootstrap_admin_refuses_to_mutate_mismatched_existing_user(
    field: str, value: str | bool
) -> None:
    user = get_user_model().objects.create_superuser(
        username="admin", email="admin@example.invalid", password=VALID_PASSWORD
    )
    setattr(user, field, value)
    user.save(update_fields=[field])
    original_state = (
        user.pk,
        user.email,
        user.password,
        user.is_active,
        user.is_staff,
        user.is_superuser,
    )

    with pytest.raises(
        ConfigurationError, match=r"^administrator bootstrap configuration is invalid$"
    ):
        bootstrap_admin(
            username="admin",
            email="admin@example.invalid",
            password="Quartz-Harbor-Orbit-984!",
            request_id=VALID_REQUEST_ID,
        )

    user.refresh_from_db()
    assert (
        user.pk,
        user.email,
        user.password,
        user.is_active,
        user.is_staff,
        user.is_superuser,
    ) == original_state


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("username", "email", "password", "request_id"),
    [
        ("", "admin@example.invalid", VALID_PASSWORD, VALID_REQUEST_ID),
        ("a" * 151, "admin@example.invalid", VALID_PASSWORD, VALID_REQUEST_ID),
        ("not valid", "admin@example.invalid", VALID_PASSWORD, VALID_REQUEST_ID),
        ("admin", "", VALID_PASSWORD, VALID_REQUEST_ID),
        ("admin", f"{'a' * 240}@example.invalid", VALID_PASSWORD, VALID_REQUEST_ID),
        ("admin", "not-an-email", VALID_PASSWORD, VALID_REQUEST_ID),
        ("admin", "admin@example.invalid", "", VALID_REQUEST_ID),
        ("admin", "admin@example.invalid", "short", VALID_REQUEST_ID),
        ("admin", "admin@example.invalid", "x" * 4097, VALID_REQUEST_ID),
        ("admin", "admin@example.invalid", "\u00e9" * 2049, VALID_REQUEST_ID),
        ("admin", "admin@example.invalid", VALID_PASSWORD, "abc"),
        ("admin", "admin@example.invalid", VALID_PASSWORD, "A" * 32),
        ("admin", "admin@example.invalid", VALID_PASSWORD, "g" * 32),
    ],
)
def test_bootstrap_admin_rejects_invalid_inputs_without_persisting(
    username: str, email: str, password: str, request_id: str
) -> None:
    with pytest.raises(
        ConfigurationError, match=r"^administrator bootstrap configuration is invalid$"
    ):
        bootstrap_admin(
            username=username,
            email=email,
            password=password,
            request_id=request_id,
        )

    assert get_user_model().objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_bootstrap_admin_concurrent_calls_create_once_without_rotating_password() -> None:
    barrier = Barrier(2)
    passwords = (VALID_PASSWORD, "Quartz-Harbor-Orbit-984!")

    def invoke(password: str) -> BootstrapResult:
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            return bootstrap_admin(
                username="admin",
                email="admin@example.invalid",
                password=password,
                request_id=VALID_REQUEST_ID,
            )
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, passwords))

    assert sorted(result.created for result in results) == [False, True]
    assert results[0].user_id == results[1].user_id
    user = get_user_model().objects.get(pk=results[0].user_id)
    created_result = next(result for result in results if result.created)
    winning_password = passwords[results.index(created_result)]
    assert user.check_password(winning_password)


@pytest.mark.django_db(transaction=True)
def test_bootstrap_admin_records_exact_created_and_existing_events() -> None:
    first = bootstrap_admin(
        username="admin",
        email="admin@example.invalid",
        password=VALID_PASSWORD,
        request_id=VALID_REQUEST_ID,
    )
    second = bootstrap_admin(
        username="admin",
        email="admin@example.invalid",
        password="Quartz-Harbor-Orbit-984!",
        request_id="abcdef0123456789abcdef0123456789",
    )

    events = list(AuditEvent.objects.order_by("occurred_at"))
    assert [event.event_type for event in events] == [
        "identity.bootstrap.created",
        "identity.bootstrap.existing",
    ]
    assert [event.request_id for event in events] == [
        VALID_REQUEST_ID,
        "abcdef0123456789abcdef0123456789",
    ]
    assert all(event.outcome == "success" for event in events)
    assert all(event.actor_id == first.user_id for event in events)
    assert all(event.object_id == first.user_id for event in events)
    assert all(event.metadata == {"subject_id": str(first.user_id)} for event in events)
    assert second.user_id == first.user_id


@pytest.mark.django_db(transaction=True)
def test_bootstrap_admin_audit_failure_rolls_back_new_user(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_audit(**_values: object) -> None:
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr("aegis_apps.identity.services.record_event", fail_audit)

    with pytest.raises(RuntimeError, match="simulated audit failure"):
        bootstrap_admin(
            username="admin",
            email="admin@example.invalid",
            password=VALID_PASSWORD,
            request_id=VALID_REQUEST_ID,
        )

    assert get_user_model().objects.count() == 0
    assert AuditEvent.objects.count() == 0
