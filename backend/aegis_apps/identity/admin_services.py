from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable
from typing import Any, cast

from django.contrib.auth.models import Group
from django.db import transaction
from django.forms import ModelForm

from aegis_apps.audit.services import record_event

from .models import GroupIdentity, User

_USER_AUTHORIZATION_FIELDS = frozenset(
    {"is_active", "is_staff", "is_superuser", "user_permissions"}
)


def _submitted_group_ids(form: ModelForm[Any]) -> set[int]:
    submitted = form.cleaned_data.get("groups", ())
    if not isinstance(submitted, Iterable):
        raise ValueError("user groups are invalid")
    result: set[int] = set()
    for value in submitted:
        if not isinstance(value, Group) or type(value.pk) is not int:
            raise ValueError("user groups are invalid")
        result.add(value.pk)
    return result


def _metadata(subject_id: uuid.UUID) -> dict[str, str]:
    return {"subject_id": str(subject_id)}


def save_user_from_admin(
    *, actor: User, form: ModelForm[Any], request_id: str
) -> User:
    with transaction.atomic():
        from aegis_apps.roots.locking import AuthorizationLocks
        from aegis_apps.roots.services import (
            advance_identity_admin_epochs,
            root_ids_for_groups,
            root_ids_for_user,
        )
        from aegis_apps.roots.signals import (
            suppress_membership_epoch_updates_for_identity_admin,
        )

        user = cast(User, form.instance)
        created = user._state.adding
        changes = set(form.changed_data)
        if not created and not changes:
            return user
        previous_group_ids = (
            set() if created else set(user.groups.values_list("pk", flat=True))
        )
        next_group_ids = (
            _submitted_group_ids(form)
            if "groups" in form.cleaned_data
            else set(previous_group_ids)
        )
        authorization_locks = AuthorizationLocks()
        group_scope = previous_group_ids | next_group_ids
        locked_groups = authorization_locks.groups(group_scope)
        if len(locked_groups) != len(group_scope):
            raise ValueError("user groups are invalid")
        if not created:
            previous_group_ids = set(user.groups.values_list("pk", flat=True))
        changed_group_ids = previous_group_ids ^ next_group_ids
        affected_root_ids = set(root_ids_for_groups(changed_group_ids))
        if not created and "is_active" in changes:
            affected_root_ids.update(root_ids_for_user(user.pk))
        locked_roots = authorization_locks.roots(affected_root_ids)
        if len(locked_roots) != len(affected_root_ids):
            raise ValueError("user authorization roots are invalid")
        locked_users = authorization_locks.users(() if created else (user.pk,))
        if not created:
            stored = locked_users.get(user.pk)
            if stored is None:
                raise User.DoesNotExist
            if (
                "date_joined" in changes
                and stored.date_joined.replace(microsecond=0) == user.date_joined
            ):
                user.date_joined = stored.date_joined
                changes.remove("date_joined")
            user.authorization_epoch = stored.authorization_epoch
            if not changes and not changed_group_ids:
                return user

        user.save()
        with suppress_membership_epoch_updates_for_identity_admin():
            form.save_m2m()
        persisted_group_ids = set(user.groups.values_list("pk", flat=True))
        changed_group_ids = previous_group_ids ^ persisted_group_ids
        authorization_changed = not created and bool(
            changes & _USER_AUTHORIZATION_FIELDS
        )
        if authorization_changed or changed_group_ids:
            advance_identity_admin_epochs(
                root_ids=affected_root_ids,
                user_ids=(user.pk,),
            )
        record_event(
            event_type="identity.user.created" if created else "identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=request_id,
            object_id=user.pk,
            metadata=_metadata(user.pk),
        )
        user.refresh_from_db(fields=("authorization_epoch",))
        return user


def save_group_from_admin(
    *,
    actor: User,
    form: ModelForm[Any],
    member_ids: Collection[uuid.UUID],
    request_id: str,
) -> Group:
    with transaction.atomic():
        from aegis_apps.roots.locking import AuthorizationLocks
        from aegis_apps.roots.services import (
            advance_identity_admin_epochs,
            root_ids_for_groups,
        )
        from aegis_apps.roots.signals import (
            suppress_membership_epoch_updates_for_identity_admin,
        )

        group = cast(Group, form.instance)
        created = group._state.adding
        changes = set(form.changed_data)
        if not created and not changes:
            return group

        next_member_ids = set(member_ids)
        if len(next_member_ids) != len(member_ids) or any(
            not isinstance(member_id, uuid.UUID) for member_id in next_member_ids
        ):
            raise ValueError("group members are invalid")
        authorization_locks = AuthorizationLocks()
        if created:
            group.save()
            if type(group.pk) is not int:
                raise ValueError("group is invalid")
            locked_groups = authorization_locks.groups((group.pk,))
            if group.pk not in locked_groups:
                raise ValueError("group is invalid")
            previous_member_ids: set[uuid.UUID] = set()
        else:
            if type(group.pk) is not int:
                raise ValueError("group is invalid")
            locked_group = authorization_locks.groups((group.pk,)).get(group.pk)
            if locked_group is None:
                raise Group.DoesNotExist
            previous_member_ids = set(
                locked_group.user_set.values_list("pk", flat=True)
            )

        changed_member_ids = previous_member_ids ^ next_member_ids
        affected_member_ids = set(changed_member_ids)
        if "permissions" in changes:
            affected_member_ids.update(previous_member_ids & next_member_ids)
        affected_root_ids = (
            set(root_ids_for_groups((group.pk,))) if changed_member_ids else set()
        )
        locked_roots = authorization_locks.roots(affected_root_ids)
        if len(locked_roots) != len(affected_root_ids):
            raise ValueError("group authorization roots are invalid")
        locked_users = authorization_locks.users(affected_member_ids)
        if len(locked_users) != len(affected_member_ids):
            raise ValueError("group members are invalid")
        members = list(User.objects.filter(pk__in=next_member_ids).order_by("pk"))
        if len(members) != len(next_member_ids):
            raise ValueError("group members are invalid")
        if not created:
            group.save()
        with suppress_membership_epoch_updates_for_identity_admin():
            form.save_m2m()
            group.user_set.set(members)
        if affected_member_ids:
            advance_identity_admin_epochs(
                root_ids=affected_root_ids,
                user_ids=affected_member_ids,
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
        from aegis_apps.roots.locking import AuthorizationLocks
        from aegis_apps.roots.services import (
            advance_identity_admin_epochs,
            root_ids_for_user,
        )

        affected_root_ids = root_ids_for_user(user_id)
        authorization_locks = AuthorizationLocks()
        locked_roots = authorization_locks.roots(affected_root_ids)
        if len(locked_roots) != len(affected_root_ids):
            raise ValueError("user authorization roots are invalid")
        user = authorization_locks.users((user_id,)).get(user_id)
        if user is None:
            raise User.DoesNotExist
        if user.is_active == active:
            return user
        user.is_active = active
        user.save(update_fields=["is_active"])
        advance_identity_admin_epochs(
            root_ids=affected_root_ids,
            user_ids=(user.pk,),
        )
        user.refresh_from_db(fields=("authorization_epoch",))
        record_event(
            event_type="identity.user.changed",
            outcome="success",
            actor=actor,
            request_id=request_id,
            object_id=user.pk,
            metadata=_metadata(user.pk),
        )
        return user
