from __future__ import annotations

import uuid

import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.audit.services import record_event
from aegis_apps.identity.models import User
from django.db.models.deletion import ProtectedError
from django.db.models.signals import ModelSignal, post_save, pre_save

pytestmark = pytest.mark.integration
REQUEST_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def actor() -> User:
    return User.objects.create_user(username="auditor", password="test-password")


@pytest.mark.django_db(transaction=True)
def test_record_event_persists_redacted_bounded_metadata(actor: User) -> None:
    subject_id = uuid.uuid4()

    event = record_event(
        event_type="identity.admin.created",
        outcome="success",
        actor=actor,
        request_id=REQUEST_ID,
        object_id=subject_id,
        metadata={"subject_id": str(subject_id), "api-key": "private"},
    )

    event.refresh_from_db()
    assert isinstance(event.id, uuid.UUID)
    assert event.actor == actor
    assert event.object_id == subject_id
    assert event.metadata == {"subject_id": str(subject_id), "api-key": "[REDACTED]"}


@pytest.mark.django_db(transaction=True)
def test_audit_event_instance_cannot_be_updated_or_deleted(actor: User) -> None:
    event = record_event(
        event_type="identity.user.created",
        outcome="success",
        actor=actor,
        request_id=REQUEST_ID,
    )
    event.outcome = "failure"

    with pytest.raises(PermissionError):
        event.save()
    with pytest.raises(PermissionError):
        event.delete()


@pytest.mark.django_db(transaction=True)
def test_audit_queryset_mutation_and_bulk_paths_are_blocked(actor: User) -> None:
    event = record_event(
        event_type="identity.user.created",
        outcome="success",
        actor=actor,
        request_id=REQUEST_ID,
    )
    unsaved = AuditEvent(
        event_type="identity.user.changed",
        outcome="success",
        actor=actor,
        request_id=REQUEST_ID,
    )

    blocked = (
        lambda: AuditEvent.objects.create(
            event_type="identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=REQUEST_ID,
        ),
        lambda: AuditEvent.objects.bulk_create([unsaved]),
        lambda: AuditEvent.objects.get_or_create(id=uuid.uuid4(), defaults={}),
        lambda: AuditEvent.objects.update_or_create(id=event.id, defaults={"outcome": "failure"}),
        lambda: AuditEvent.objects.filter(pk=event.pk).update(outcome="failure"),
        lambda: AuditEvent.objects.filter(pk=event.pk).delete(),
        lambda: AuditEvent.objects.filter(pk=event.pk)._raw_delete("default"),
        lambda: AuditEvent.objects.bulk_update([event], ["outcome"]),
    )

    for mutation in blocked:
        with pytest.raises(PermissionError):
            mutation()

    event.refresh_from_db()
    assert event.outcome == "success"


@pytest.mark.django_db(transaction=True)
def test_audit_write_gate_resets_after_success_and_failure(actor: User) -> None:
    record_event(
        event_type="identity.user.created",
        outcome="success",
        actor=actor,
        request_id=REQUEST_ID,
    )
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic

    with pytest.raises(ValueError):
        record_event(
            event_type="identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=REQUEST_ID,
            metadata=cyclic,
        )
    with pytest.raises(PermissionError):
        AuditEvent.objects.create(
            event_type="identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=REQUEST_ID,
        )


@pytest.mark.django_db(transaction=True)
def test_audit_instance_save_base_is_blocked_outside_service(actor: User) -> None:
    event = AuditEvent(
        event_type="identity.user.created",
        outcome="success",
        actor=actor,
        request_id=REQUEST_ID,
    )

    with pytest.raises(PermissionError):
        event.save_base(raw=True)

    assert AuditEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_audit_service_fails_closed_on_invalid_or_oversize_boundaries(actor: User) -> None:
    class OversizedOutcome(str):
        def __hash__(self) -> int:
            raise AssertionError("oversized outcome reached allowlist lookup")

    invalid_calls = (
        {"event_type": "Identity BAD"},
        {"outcome": "unknown"},
        {"outcome": OversizedOutcome("x" * 1_000_000)},
        {"request_id": "short"},
        {"object_id": "not-a-uuid"},
        {"metadata": ["not", "an", "object"]},
        {"metadata": {"ok": "x" * (17 * 1024)}},
    )

    for overrides in invalid_calls:
        values = {
            "event_type": "identity.user.changed",
            "outcome": "success",
            "actor": actor,
            "request_id": REQUEST_ID,
            **overrides,
        }
        with pytest.raises((TypeError, ValueError)):
            record_event(**values)  # type: ignore[arg-type]

    assert AuditEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_audit_actor_is_protected(actor: User) -> None:
    record_event(
        event_type="identity.user.created",
        outcome="success",
        actor=actor,
        request_id=REQUEST_ID,
    )

    with pytest.raises(ProtectedError):
        actor.delete()


@pytest.mark.django_db(transaction=True)
def test_guarded_manager_is_both_base_and_default_manager(actor: User) -> None:
    event = record_event(
        event_type="identity.user.created",
        outcome="success",
        actor=actor,
        request_id=REQUEST_ID,
    )
    managers = (AuditEvent._base_manager, AuditEvent._default_manager)

    for manager in managers:
        with pytest.raises(PermissionError):
            manager.filter(pk=event.pk).update(outcome="failure")
        with pytest.raises(PermissionError):
            manager.filter(pk=event.pk).delete()
        with pytest.raises(PermissionError):
            manager.bulk_create(
                [
                    AuditEvent(
                        event_type="identity.user.changed",
                        outcome="success",
                        actor=actor,
                        request_id=REQUEST_ID,
                    )
                ]
            )
        with pytest.raises(PermissionError):
            manager.bulk_update([event], ["outcome"])

    event.refresh_from_db()
    assert event.outcome == "success"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("signal", [pre_save, post_save])
def test_audit_capability_cannot_be_borrowed_by_signal_insert(
    signal: ModelSignal, actor: User
) -> None:
    rejected: list[PermissionError] = []

    def attempt_nested_insert(*, instance: AuditEvent, **_kwargs: object) -> None:
        if instance.event_type != "identity.user.created":
            return
        nested = AuditEvent(
            event_type="identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=REQUEST_ID,
        )
        try:
            nested.save()
        except PermissionError as error:
            rejected.append(error)

    signal.connect(attempt_nested_insert, sender=AuditEvent, weak=False)
    try:
        intended = record_event(
            event_type="identity.user.created",
            outcome="success",
            actor=actor,
            request_id=REQUEST_ID,
        )
    finally:
        signal.disconnect(attempt_nested_insert, sender=AuditEvent)

    assert len(rejected) == 1
    assert list(AuditEvent.objects.values_list("pk", flat=True)) == [intended.pk]


@pytest.mark.django_db(transaction=True)
def test_uncaught_nested_signal_insert_rolls_back_intended_event(actor: User) -> None:
    def attempt_nested_insert(*, instance: AuditEvent, **_kwargs: object) -> None:
        if instance.event_type != "identity.user.created":
            return
        AuditEvent(
            event_type="identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=REQUEST_ID,
        ).save()

    pre_save.connect(attempt_nested_insert, sender=AuditEvent, weak=False)
    try:
        with pytest.raises(PermissionError):
            record_event(
                event_type="identity.user.created",
                outcome="success",
                actor=actor,
                request_id=REQUEST_ID,
            )
    finally:
        pre_save.disconnect(attempt_nested_insert, sender=AuditEvent)

    assert AuditEvent.objects.count() == 0


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "event_type",
    ["identity." + "a" * 88, "identity." + "a" * 1_000_000],
)
def test_event_type_over_model_bound_fails_cleanly(actor: User, event_type: str) -> None:
    assert len(event_type) > 96

    with pytest.raises(ValueError, match="invalid audit event type"):
        record_event(
            event_type=event_type,
            outcome="success",
            actor=actor,
            request_id=REQUEST_ID,
        )

    assert AuditEvent.objects.count() == 0
