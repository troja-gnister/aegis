from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import Any, cast

from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import F
from django.forms import ModelForm

from aegis_apps.audit.services import record_event

from .models import GroupIdentity, User

_USER_AUTHORIZATION_FIELDS = frozenset(
    {"is_active", "is_staff", "is_superuser", "user_permissions"}
)


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
            stored = (
                User.objects.select_for_update()
                .only("date_joined", "authorization_epoch")
                .get(pk=user.pk)
            )
            if (
                "date_joined" in changes
                and stored.date_joined.replace(microsecond=0) == user.date_joined
            ):
                user.date_joined = stored.date_joined
                changes.remove("date_joined")
            if not changes:
                return user
            if changes & _USER_AUTHORIZATION_FIELDS:
                user.authorization_epoch = stored.authorization_epoch + 1

        user.save()
        form.save_m2m()
        if not created and "is_active" in changes:
            from aegis_apps.roots.services import advance_roots_for_user_active_change

            advance_roots_for_user_active_change(user_id=user.pk)
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

        previous_member_ids = (
            set(group.user_set.values_list("pk", flat=True)) if not created else set()
        )

        group.save()
        form.save_m2m()
        members = list(User.objects.filter(pk__in=member_ids))
        if len(members) != len(set(member_ids)):
            raise ValueError("group members are invalid")
        group.user_set.set(members)
        next_member_ids = {member.pk for member in members}
        affected_member_ids = (
            previous_member_ids & next_member_ids
            if not created and "permissions" in changes
            else set()
        )
        if affected_member_ids:
            User.objects.filter(pk__in=affected_member_ids).update(
                authorization_epoch=F("authorization_epoch") + 1
            )

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
        user.authorization_epoch += 1
        user.save(update_fields=["is_active", "authorization_epoch"])
        from aegis_apps.roots.services import advance_roots_for_user_active_change

        advance_roots_for_user_active_change(user_id=user.pk)
        record_event(
            event_type="identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=request_id,
            object_id=user.pk,
            metadata=_metadata(user.pk),
        )
        return user
