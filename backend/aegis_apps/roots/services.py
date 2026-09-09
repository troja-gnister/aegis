from __future__ import annotations

import hashlib
import uuid
from collections.abc import Collection
from functools import partial

from aegisctl.mounts import SLOT_ID_RE
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import F, Q

from aegis_apps.audit.services import record_event
from aegis_apps.common.middleware import REQUEST_ID
from aegis_apps.identity.models import GroupIdentity, User

from .locking import AuthorizationLocks
from .manifest import ManifestError, configured_manifest
from .models import Root, RootGrant, _grant_delete_capability
from .permissions import Permission, validate_permission_mask
from .selectors import invalidate_authorization_cache


class _Unset:
    pass


_UNSET = _Unset()
_ROOT_SLOT_LOCK_DOMAIN = b"aegis.authorization.root-slot.v1\x00"


def _validate_common(*, actor: User, request_id: str) -> None:
    if not isinstance(actor, User) or actor.pk is None:
        raise ValueError("invalid authorization actor")
    if (
        not isinstance(request_id, str)
        or not 8 <= len(request_id) <= 64
        or REQUEST_ID.fullmatch(request_id) is None
    ):
        raise ValueError("invalid authorization request ID")


def _validate_uuid(value: object, *, field_name: str) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ValueError(f"invalid {field_name}")
    return value


def _validate_group_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("invalid group ID")
    return value


def _lock_root_slot(slot_id: str) -> None:
    digest = hashlib.sha256(_ROOT_SLOT_LOCK_DOMAIN + slot_id.encode("ascii")).digest()
    lock_key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_key])


def _validate_root_definition(root: Root) -> None:
    try:
        manifest = configured_manifest()
    except ManifestError:
        raise ValueError("root manifest is unavailable") from None
    if manifest is None:
        raise ValueError("root manifest is unconfigured")
    slot = manifest.get(root.slot_id)
    if slot is None:
        raise ValueError("root manifest slot is unavailable")
    if root.mode == Root.Mode.READ_WRITE and slot.mode != "read_write":
        raise ValueError("root mode exceeds manifest mode")
    try:
        root.full_clean(validate_unique=False)
    except ValidationError:
        raise ValueError("root definition is invalid") from None


def _affected_user_ids(root_id: uuid.UUID) -> frozenset[uuid.UUID]:
    return frozenset(
        User.objects.filter(
            Q(root_grants__root_id=root_id) | Q(groups__root_grants__root_id=root_id)
        )
        .values_list("pk", flat=True)
        .distinct()
    )


def _advance_epochs(
    *,
    root_ids: Collection[uuid.UUID],
    user_ids: Collection[uuid.UUID],
) -> None:
    roots = frozenset(root_ids)
    users = frozenset(user_ids)
    if not roots and not users:
        return
    if roots:
        Root.objects.filter(pk__in=roots).update(
            authorization_epoch=F("authorization_epoch") + 1
        )
    if users:
        User.objects.filter(pk__in=users).update(
            authorization_epoch=F("authorization_epoch") + 1
        )
    transaction.on_commit(
        partial(
            invalidate_authorization_cache,
            user_ids=users,
            root_ids=roots,
        )
    )


def _root_audit(
    *, event_type: str, actor: User, request_id: str, root: Root
) -> None:
    record_event(
        event_type=event_type,
        outcome="success",
        actor=actor,
        request_id=request_id,
        root_id=root.id,
        object_id=root.id,
        metadata={"root_id": str(root.id)},
    )


def create_root(
    *,
    actor: User,
    slot_id: str,
    display_name: str,
    mode: str,
    request_id: str,
    active: bool = False,
) -> Root:
    _validate_common(actor=actor, request_id=request_id)
    if not isinstance(slot_id, str) or SLOT_ID_RE.fullmatch(slot_id) is None:
        raise ValueError("invalid root slot ID")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 160:
        raise ValueError("invalid root display name")
    if mode not in Root.Mode.values:
        raise ValueError("invalid root mode")
    if not isinstance(active, bool):
        raise ValueError("invalid root active state")
    with transaction.atomic():
        _lock_root_slot(slot_id)
        root = Root(
            slot_id=slot_id,
            display_name=display_name,
            mode=mode,
            active=active,
        )
        _validate_root_definition(root)
        existing = Root.objects.filter(slot_id=slot_id).first()
        if existing is not None:
            if (
                existing.display_name == display_name
                and existing.mode == mode
                and existing.active is active
            ):
                return existing
            raise ValueError("root manifest slot is already configured")
        root.save(force_insert=True)
        _root_audit(
            event_type="authorization.root.created",
            actor=actor,
            request_id=request_id,
            root=root,
        )
        return root


def update_root(
    *,
    actor: User,
    root_id: uuid.UUID,
    request_id: str,
    slot_id: str | _Unset = _UNSET,
    display_name: str | _Unset = _UNSET,
    mode: str | _Unset = _UNSET,
    active: bool | _Unset = _UNSET,
) -> Root:
    _validate_common(actor=actor, request_id=request_id)
    root_uuid = _validate_uuid(root_id, field_name="root ID")
    with transaction.atomic():
        authorization_locks = AuthorizationLocks()
        root = authorization_locks.roots((root_uuid,)).get(root_uuid)
        if root is None:
            raise Root.DoesNotExist
        original_slot_id = root.slot_id
        original_display_name = root.display_name
        original_mode = root.mode
        original_active = root.active
        if not isinstance(slot_id, _Unset):
            if not isinstance(slot_id, str) or SLOT_ID_RE.fullmatch(slot_id) is None:
                raise ValueError("invalid root slot ID")
            root.slot_id = slot_id
        if not isinstance(display_name, _Unset):
            if (
                not isinstance(display_name, str)
                or not display_name.strip()
                or len(display_name) > 160
            ):
                raise ValueError("invalid root display name")
            root.display_name = display_name
        if not isinstance(mode, _Unset):
            if mode not in Root.Mode.values:
                raise ValueError("invalid root mode")
            root.mode = mode
        if not isinstance(active, _Unset):
            if not isinstance(active, bool):
                raise ValueError("invalid root active state")
            root.active = active
        _validate_root_definition(root)
        if root.slot_id != original_slot_id:
            _lock_root_slot(root.slot_id)
            if Root.objects.filter(slot_id=root.slot_id).exclude(pk=root.id).exists():
                raise ValueError("root manifest slot is already configured")
        changed_fields = {
            field_name
            for field_name, previous, current in (
                ("slot_id", original_slot_id, root.slot_id),
                ("display_name", original_display_name, root.display_name),
                ("mode", original_mode, root.mode),
                ("active", original_active, root.active),
            )
            if previous != current
        }
        if not changed_fields:
            return root
        authorization_changed = bool(changed_fields & {"slot_id", "mode", "active"})
        affected_users: frozenset[uuid.UUID] = frozenset()
        if authorization_changed:
            affected_users = frozenset(
                authorization_locks.users(_affected_user_ids(root.id))
            )
        root.save(update_fields=(*sorted(changed_fields), "updated_at"))
        if authorization_changed:
            _advance_epochs(root_ids=(root.id,), user_ids=affected_users)
            root.refresh_from_db(fields=("authorization_epoch",))
        _root_audit(
            event_type="authorization.root.changed",
            actor=actor,
            request_id=request_id,
            root=root,
        )
        return root


def activate_root(*, actor: User, root_id: uuid.UUID, request_id: str) -> Root:
    return update_root(
        actor=actor,
        root_id=root_id,
        active=True,
        request_id=request_id,
    )


def deactivate_root(*, actor: User, root_id: uuid.UUID, request_id: str) -> Root:
    return update_root(
        actor=actor,
        root_id=root_id,
        active=False,
        request_id=request_id,
    )


def _grant_audit(
    *,
    event_type: str,
    actor: User,
    request_id: str,
    root_id: uuid.UUID,
    subject_id: uuid.UUID,
    permissions: Permission,
) -> None:
    record_event(
        event_type=event_type,
        outcome="success",
        actor=actor,
        request_id=request_id,
        root_id=root_id,
        object_id=subject_id,
        metadata={"subject_id": str(subject_id), "permissions": int(permissions)},
    )


def set_user_grant(
    *,
    actor: User,
    root_id: uuid.UUID,
    user_id: uuid.UUID,
    permissions: Permission,
    request_id: str,
) -> RootGrant:
    _validate_common(actor=actor, request_id=request_id)
    root_uuid = _validate_uuid(root_id, field_name="root ID")
    user_uuid = _validate_uuid(user_id, field_name="user ID")
    mask = validate_permission_mask(permissions)
    with transaction.atomic():
        authorization_locks = AuthorizationLocks()
        root = authorization_locks.roots((root_uuid,)).get(root_uuid)
        if root is None:
            raise Root.DoesNotExist
        _validate_root_definition(root)
        subject = authorization_locks.users((user_uuid,)).get(user_uuid)
        if subject is None:
            raise User.DoesNotExist
        grant = (
            RootGrant.objects.select_for_update()
            .filter(root=root, user=subject)
            .first()
        )
        if grant is not None and grant.permissions == int(mask):
            return grant
        if grant is None:
            grant = RootGrant.objects.create(
                root=root,
                user=subject,
                permissions=int(mask),
            )
        else:
            grant.permissions = int(mask)
            grant.save(update_fields=("permissions", "updated_at"))
        _advance_epochs(root_ids=(root.id,), user_ids=(subject.id,))
        _grant_audit(
            event_type="authorization.user.grant.changed",
            actor=actor,
            request_id=request_id,
            root_id=root.id,
            subject_id=subject.id,
            permissions=mask,
        )
        return grant


def set_group_grant(
    *,
    actor: User,
    root_id: uuid.UUID,
    group_id: int,
    permissions: Permission,
    request_id: str,
) -> RootGrant:
    _validate_common(actor=actor, request_id=request_id)
    root_uuid = _validate_uuid(root_id, field_name="root ID")
    internal_group_id = _validate_group_id(group_id)
    mask = validate_permission_mask(permissions)
    with transaction.atomic():
        authorization_locks = AuthorizationLocks()
        group = authorization_locks.groups((internal_group_id,)).get(internal_group_id)
        if group is None:
            raise Group.DoesNotExist
        root = authorization_locks.roots((root_uuid,)).get(root_uuid)
        if root is None:
            raise Root.DoesNotExist
        _validate_root_definition(root)
        existing = RootGrant.objects.filter(root=root, group=group).first()
        if existing is not None and existing.permissions == int(mask):
            return existing
        members = frozenset(
            authorization_locks.users(
                User.objects.filter(groups=group).values_list("pk", flat=True)
            )
        )
        grant = (
            RootGrant.objects.select_for_update()
            .filter(root=root, group=group)
            .first()
        )
        if grant is None:
            grant = RootGrant.objects.create(
                root=root,
                group=group,
                permissions=int(mask),
            )
        else:
            grant.permissions = int(mask)
            grant.save(update_fields=("permissions", "updated_at"))
        identity, _ = GroupIdentity.objects.get_or_create(group=group)
        _advance_epochs(root_ids=(root.id,), user_ids=members)
        _grant_audit(
            event_type="authorization.group.grant.changed",
            actor=actor,
            request_id=request_id,
            root_id=root.id,
            subject_id=identity.id,
            permissions=mask,
        )
        return grant


def remove_grant(
    *, actor: User, grant_id: uuid.UUID, request_id: str
) -> RootGrant | None:
    _validate_common(actor=actor, request_id=request_id)
    grant_uuid = _validate_uuid(grant_id, field_name="grant ID")
    with transaction.atomic():
        grant_identity = (
            RootGrant.objects.filter(pk=grant_uuid)
            .values("root_id", "user_id", "group_id")
            .first()
        )
        if grant_identity is None:
            return None
        root_id = grant_identity["root_id"]
        user_id = grant_identity["user_id"]
        group_id = grant_identity["group_id"]
        if not isinstance(root_id, uuid.UUID):
            raise RuntimeError("invalid persisted root grant")
        authorization_locks = AuthorizationLocks()
        group: Group | None = None
        if type(group_id) is int:
            group = authorization_locks.groups((group_id,)).get(group_id)
            if group is None:
                raise RuntimeError("invalid persisted root grant")
        root = authorization_locks.roots((root_id,)).get(root_id)
        if root is None:
            raise RuntimeError("invalid persisted root grant")
        if isinstance(user_id, uuid.UUID):
            affected_users = frozenset(authorization_locks.users((user_id,)))
        elif group is not None:
            affected_users = frozenset(
                authorization_locks.users(
                    User.objects.filter(groups=group).values_list("pk", flat=True)
                )
            )
        else:
            raise RuntimeError("invalid persisted root grant")
        grant = (
            RootGrant.objects.select_for_update()
            .filter(
                pk=grant_uuid,
                root_id=root_id,
                user_id=user_id,
                group_id=group_id,
            )
            .first()
        )
        if grant is None:
            return None
        _validate_root_definition(root)
        mask = validate_permission_mask(grant.permissions)
        if grant.user_id is not None:
            subject_id = grant.user_id
        else:
            if grant.group_id is None or group is None:
                raise RuntimeError("invalid persisted root grant")
            group_identity, _ = GroupIdentity.objects.get_or_create(
                group=group
            )
            subject_id = group_identity.id
        token = _grant_delete_capability.set(grant)
        try:
            grant.delete()
        finally:
            _grant_delete_capability.reset(token)
        grant.id = grant_uuid
        _advance_epochs(root_ids=(root.id,), user_ids=affected_users)
        _grant_audit(
            event_type="authorization.grant.removed",
            actor=actor,
            request_id=request_id,
            root_id=root.id,
            subject_id=subject_id,
            permissions=mask,
        )
        return grant


def root_ids_for_user(user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    return frozenset(
        Root.objects.filter(
            Q(grants__user_id=user_id) | Q(grants__group__user=user_id)
        )
        .values_list("pk", flat=True)
        .distinct()
    )


def root_ids_for_groups(group_ids: Collection[int]) -> frozenset[uuid.UUID]:
    groups = frozenset(group_ids)
    if not groups:
        return frozenset()
    return frozenset(
        RootGrant.objects.filter(group_id__in=groups)
        .values_list("root_id", flat=True)
        .distinct()
    )


def advance_identity_admin_epochs(
    *, root_ids: Collection[uuid.UUID], user_ids: Collection[uuid.UUID]
) -> None:
    _advance_epochs(root_ids=root_ids, user_ids=user_ids)


def advance_membership_epochs(
    *,
    user_ids: Collection[uuid.UUID],
    group_ids: Collection[int],
    root_ids: Collection[uuid.UUID],
) -> None:
    users = frozenset(user_ids)
    groups = frozenset(group_ids)
    roots = frozenset(root_ids)
    if not users or not groups:
        return
    _advance_epochs(root_ids=roots, user_ids=users)
