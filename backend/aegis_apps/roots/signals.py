from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.contrib.auth.models import Group
from django.db.models.signals import ModelSignal

from aegis_apps.identity.models import User

from .locking import AuthorizationLocks
from .services import advance_membership_epochs, root_ids_for_groups

_PENDING_ATTRIBUTE = "_aegis_membership_epoch_pending"
_membership_epoch_updates_suppressed: ContextVar[bool] = ContextVar(
    "aegis_membership_epoch_updates_suppressed",
    default=False,
)


@dataclass(frozen=True, slots=True)
class _MembershipChange:
    user_ids: frozenset[uuid.UUID]
    group_ids: frozenset[int]
    root_ids: frozenset[uuid.UUID] = frozenset()


@contextmanager
def suppress_membership_epoch_updates_for_identity_admin() -> Iterator[None]:
    token = _membership_epoch_updates_suppressed.set(True)
    try:
        yield
    finally:
        _membership_epoch_updates_suppressed.reset(token)


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
        return _MembershipChange(frozenset(), frozenset(), frozenset())
    value = pending.pop(_key(action=action, reverse=reverse), None)
    if not pending:
        delattr(instance, _PENDING_ATTRIBUTE)
    if isinstance(value, _MembershipChange):
        return value
    return _MembershipChange(frozenset(), frozenset(), frozenset())


def _group_ids_for_lock(
    *,
    instance: User | Group,
    reverse: bool,
    pk_set: set[object] | None,
    clear: bool,
) -> frozenset[int]:
    if reverse:
        if not isinstance(instance, Group) or type(instance.pk) is not int:
            return frozenset()
        return frozenset((instance.pk,))
    if not isinstance(instance, User) or instance.pk is None:
        return frozenset()
    if clear:
        return frozenset(instance.groups.values_list("pk", flat=True))
    return frozenset(value for value in (pk_set or ()) if type(value) is int and value > 0)


def _lock_membership_change(
    *,
    instance: User | Group,
    action: str,
    reverse: bool,
    pk_set: set[object] | None,
) -> _MembershipChange:
    clear = action == "pre_clear"
    group_ids = _group_ids_for_lock(
        instance=instance,
        reverse=reverse,
        pk_set=pk_set,
        clear=clear,
    )
    authorization_locks = AuthorizationLocks()
    locked_groups = authorization_locks.groups(group_ids)
    if len(locked_groups) != len(group_ids):
        raise ValueError("group membership principals are invalid")
    if action == "pre_add":
        change = _post_add(instance=instance, reverse=reverse, pk_set=pk_set)
    else:
        change = _pre_remove_or_clear(
            instance=instance,
            reverse=reverse,
            pk_set=pk_set,
            clear=clear,
        )
    root_ids = root_ids_for_groups(group_ids)
    locked_roots = authorization_locks.roots(root_ids)
    if len(locked_roots) != len(root_ids):
        raise ValueError("group membership roots are invalid")
    locked_users = authorization_locks.users(change.user_ids)
    if len(locked_users) != len(change.user_ids):
        raise ValueError("group membership users are invalid")
    return _MembershipChange(change.user_ids, change.group_ids, root_ids)


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
    if _membership_epoch_updates_suppressed.get():
        return
    if action in {"pre_add", "pre_remove", "pre_clear"}:
        _store(
            instance=instance,
            action=action,
            reverse=reverse,
            change=_lock_membership_change(
                instance=instance,
                action=action,
                reverse=reverse,
                pk_set=pk_set,
            ),
        )
        return
    if action == "post_add":
        change = _pop(instance=instance, action="pre_add", reverse=reverse)
    elif action == "post_remove":
        change = _pop(instance=instance, action="pre_remove", reverse=reverse)
    elif action == "post_clear":
        change = _pop(instance=instance, action="pre_clear", reverse=reverse)
    else:
        return
    advance_membership_epochs(
        user_ids=change.user_ids,
        group_ids=change.group_ids,
        root_ids=change.root_ids,
    )


def connect_membership_signals(signal: ModelSignal) -> None:
    signal.connect(
        handle_group_membership_change,
        sender=User.groups.through,
        dispatch_uid="aegis.roots.group_membership_epochs",
        weak=False,
    )
