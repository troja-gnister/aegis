from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.contrib.auth.models import Group
from django.db.models.signals import ModelSignal

from aegis_apps.identity.models import User

from .services import advance_membership_epochs

_PENDING_ATTRIBUTE = "_aegis_membership_epoch_pending"


@dataclass(frozen=True, slots=True)
class _MembershipChange:
    user_ids: frozenset[uuid.UUID]
    group_ids: frozenset[int]


def _key(*, action: str, reverse: bool) -> tuple[str, bool]:
    return action, reverse


def _store(
    *, instance: User | Group, action: str, reverse: bool, change: _MembershipChange
) -> None:
    pending = getattr(instance, _PENDING_ATTRIBUTE, None)
    if not isinstance(pending, dict):
        pending = {}
        setattr(instance, _PENDING_ATTRIBUTE, pending)
    pending[_key(action=action, reverse=reverse)] = change


def _pop(*, instance: User | Group, action: str, reverse: bool) -> _MembershipChange:
    pending = getattr(instance, _PENDING_ATTRIBUTE, None)
    if not isinstance(pending, dict):
        return _MembershipChange(frozenset(), frozenset())
    value = pending.pop(_key(action=action, reverse=reverse), None)
    if not pending:
        delattr(instance, _PENDING_ATTRIBUTE)
    if isinstance(value, _MembershipChange):
        return value
    return _MembershipChange(frozenset(), frozenset())


def _pre_remove_or_clear(
    *,
    instance: User | Group,
    reverse: bool,
    pk_set: set[object] | None,
    clear: bool,
) -> _MembershipChange:
    if reverse:
        if not isinstance(instance, Group) or instance.pk is None:
            return _MembershipChange(frozenset(), frozenset())
        members = instance.user_set.all()
        if not clear:
            members = members.filter(pk__in=pk_set or ())
        return _MembershipChange(
            frozenset(members.values_list("pk", flat=True)),
            frozenset((instance.pk,)),
        )
    if not isinstance(instance, User) or instance.pk is None:
        return _MembershipChange(frozenset(), frozenset())
    groups = instance.groups.all()
    if not clear:
        groups = groups.filter(pk__in=pk_set or ())
    return _MembershipChange(
        frozenset((instance.pk,)),
        frozenset(groups.values_list("pk", flat=True)),
    )


def _post_add(
    *, instance: User | Group, reverse: bool, pk_set: set[object] | None
) -> _MembershipChange:
    values = pk_set or set()
    if reverse:
        if not isinstance(instance, Group) or instance.pk is None:
            return _MembershipChange(frozenset(), frozenset())
        user_ids = frozenset(value for value in values if isinstance(value, uuid.UUID))
        return _MembershipChange(user_ids, frozenset((instance.pk,)))
    if not isinstance(instance, User) or instance.pk is None:
        return _MembershipChange(frozenset(), frozenset())
    group_ids = frozenset(
        value for value in values if type(value) is int and value > 0
    )
    return _MembershipChange(frozenset((instance.pk,)), group_ids)


def handle_group_membership_change(
    sender: type[object],
    instance: User | Group,
    action: str,
    reverse: bool,
    model: type[object],
    pk_set: set[object] | None,
    using: str,
    **_kwargs: object,
) -> None:
    del sender, model, using
    if action in {"pre_remove", "pre_clear"}:
        _store(
            instance=instance,
            action=action,
            reverse=reverse,
            change=_pre_remove_or_clear(
                instance=instance,
                reverse=reverse,
                pk_set=pk_set,
                clear=action == "pre_clear",
            ),
        )
        return
    if action == "post_add":
        change = _post_add(instance=instance, reverse=reverse, pk_set=pk_set)
    elif action == "post_remove":
        change = _pop(instance=instance, action="pre_remove", reverse=reverse)
    elif action == "post_clear":
        change = _pop(instance=instance, action="pre_clear", reverse=reverse)
    else:
        return
    advance_membership_epochs(
        user_ids=change.user_ids,
        group_ids=change.group_ids,
    )


def connect_membership_signals(signal: ModelSignal) -> None:
    signal.connect(
        handle_group_membership_change,
        sender=User.groups.through,
        dispatch_uid="aegis.roots.group_membership_epochs",
        weak=False,
    )
