from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, cast

from django.contrib.auth.models import Group
from django.db import transaction
from django.forms import ModelForm

from aegis_apps.audit.services import record_event

from .models import GroupIdentity, User


def _metadata(subject_id: uuid.UUID) -> dict[str, str]:
    return {"subject_id": str(subject_id)}


def save_user_from_admin(
    *, actor: User, form: ModelForm[Any], request_id: str
) -> User:
    with transaction.atomic():
        user = cast(User, form.instance)
        created = user._state.adding
        changes = set(form.changed_data)
        if not created:
            stored = User.objects.select_for_update().only("date_joined").get(pk=user.pk)
            if (
                "date_joined" in changes
                and stored.date_joined.replace(microsecond=0) == user.date_joined
            ):
                user.date_joined = stored.date_joined
                changes.remove("date_joined")
            if not changes:
                return user

        user.save()
        form.save_m2m()
        record_event(
            event_type="identity.user.created" if created else "identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=request_id,
            object_id=user.pk,
            metadata=_metadata(user.pk),
        )
        return user


def save_group_from_admin(
    *,
    actor: User,
    form: ModelForm[Any],
    member_ids: Collection[uuid.UUID],
    request_id: str,
) -> Group:
    with transaction.atomic():
        group = cast(Group, form.instance)
        created = group._state.adding
        changes = set(form.changed_data)
        if not created and not changes:
            return group

        group.save()
        form.save_m2m()
        members = list(User.objects.filter(pk__in=member_ids))
        if len(members) != len(set(member_ids)):
            raise ValueError("group members are invalid")
        group.user_set.set(members)

        identity, _ = GroupIdentity.objects.select_for_update().get_or_create(group=group)
        subject_id = identity.pk
        if created:
            event_type = "identity.group.created"
        elif changes - {"members"}:
            event_type = "identity.group.changed"
        else:
            event_type = "identity.group.membership.changed"
        record_event(
            event_type=event_type,
            outcome="success",
            actor=actor,
            request_id=request_id,
            object_id=subject_id,
            metadata=_metadata(subject_id),
        )
        return group


def set_user_active(
    *, actor: User, user_id: uuid.UUID, active: bool, request_id: str
) -> User:
    if not isinstance(active, bool):
        raise TypeError("active must be a boolean")
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.is_active == active:
            return user
        user.is_active = active
        user.save(update_fields=["is_active"])
        record_event(
            event_type="identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=request_id,
            object_id=user.pk,
            metadata=_metadata(user.pk),
        )
        return user
