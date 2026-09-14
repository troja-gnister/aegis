from __future__ import annotations

import uuid
from functools import partial

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from aegis_apps.common.database_privileges import (
    PrivilegeSynchronizationError,
    current_database_login,
)
from aegis_apps.identity.models import User
from aegis_apps.operations.models import UNCONFIGURED_MANIFEST_IDENTITY
from aegis_apps.roots.locking import AuthorizationLocks, MembershipDiscoveryChanged
from aegis_apps.roots.manifest import MountManifest
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.selectors import invalidate_authorization_cache

from .config import ScanPolicy
from .models import IndexDeployment, RootIndexState, ScanRun


def _configuration(
    manifest: MountManifest | None, policy: ScanPolicy
) -> tuple[str, list[str], dict[str, int]]:
    manifest_identity = (
        UNCONFIGURED_MANIFEST_IDENTITY if manifest is None else manifest.digest
    )
    slot_ids = [] if manifest is None else sorted(manifest.slots)
    values = {
        "interval_seconds": policy.interval_seconds,
        "idle_timeout_seconds": policy.idle_timeout_seconds,
        "batch_records": policy.batch_records,
        "readers": policy.readers,
    }
    return manifest_identity, slot_ids, values


def _lock_affected_authorization(
    slot_ids: frozenset[str],
) -> tuple[frozenset[uuid.UUID], frozenset[uuid.UUID]]:
    root_ids = frozenset(
        Root.objects.filter(slot_id__in=slot_ids).values_list("pk", flat=True)
    )
    if not root_ids:
        return frozenset(), frozenset()
    group_ids = frozenset(
        group_id
        for group_id in RootGrant.objects.filter(root_id__in=root_ids).values_list(
            "group_id", flat=True
        )
        if type(group_id) is int
    )
    discovered_members = frozenset(
        User.objects.filter(groups__id__in=group_ids).values_list("pk", flat=True)
    )
    locks = AuthorizationLocks()
    locks.membership_users(discovered_members)
    locked_groups = locks.groups(group_ids)
    if len(locked_groups) != len(group_ids):
        raise MembershipDiscoveryChanged("binding authorization groups changed")
    locked_roots = locks.roots(root_ids)
    if len(locked_roots) != len(root_ids):
        raise MembershipDiscoveryChanged("binding authorization roots changed")
    current_group_ids = frozenset(
        group_id
        for group_id in RootGrant.objects.filter(root_id__in=root_ids).values_list(
            "group_id", flat=True
        )
        if type(group_id) is int
    )
    current_members = frozenset(
        User.objects.filter(groups__id__in=current_group_ids).values_list("pk", flat=True)
    )
    if current_group_ids != group_ids or current_members != discovered_members:
        raise MembershipDiscoveryChanged("binding authorization membership changed")
    direct_users = frozenset(
        user_id
        for user_id in RootGrant.objects.filter(root_id__in=root_ids).values_list(
            "user_id", flat=True
        )
        if isinstance(user_id, uuid.UUID)
    )
    user_ids = direct_users | current_members
    locked_users = locks.users(user_ids)
    if len(locked_users) != len(user_ids):
        raise MembershipDiscoveryChanged("binding authorization users changed")
    return root_ids, user_ids


def _advance_authorization_epochs(
    root_ids: frozenset[uuid.UUID], user_ids: frozenset[uuid.UUID]
) -> None:
    if root_ids:
        Root.objects.filter(pk__in=root_ids).update(
            authorization_epoch=F("authorization_epoch") + 1
        )
    if user_ids:
        User.objects.filter(pk__in=user_ids).update(
            authorization_epoch=F("authorization_epoch") + 1
        )
    if root_ids or user_ids:
        transaction.on_commit(
            partial(
                invalidate_authorization_cache,
                user_ids=user_ids,
                root_ids=root_ids,
            )
        )


def install_index_binding(manifest: MountManifest | None, policy: ScanPolicy) -> None:
    if current_database_login() != "aegis_migrator":
        raise PrivilegeSynchronizationError(
            "index deployment installation requires the migrator login"
        )
    manifest_identity, slot_ids, policy_values = _configuration(manifest, policy)
    with transaction.atomic():
        deployment = IndexDeployment.objects.select_for_update().filter(pk=1).first()
        if deployment is None:
            IndexDeployment.objects.create(
                id=1,
                epoch=1,
                manifest_identity=manifest_identity,
                slot_ids=slot_ids,
                **policy_values,
            )
            return
        unchanged = (
            deployment.manifest_identity == manifest_identity
            and deployment.slot_ids == slot_ids
            and all(getattr(deployment, name) == value for name, value in policy_values.items())
        )
        if unchanged:
            return

        affected_roots, affected_users = _lock_affected_authorization(
            frozenset(deployment.slot_ids) | frozenset(slot_ids)
        )
        next_epoch = deployment.epoch + 1
        deployment.epoch = next_epoch
        deployment.manifest_identity = manifest_identity
        deployment.slot_ids = slot_ids
        for name, value in policy_values.items():
            setattr(deployment, name, value)
        deployment.save(
            update_fields=(
                "epoch",
                "manifest_identity",
                "slot_ids",
                *policy_values,
                "updated_at",
            )
        )

        settled_at = timezone.now()
        ScanRun.objects.filter(
            root_id__in=affected_roots,
            state__in=(ScanRun.State.QUEUED, ScanRun.State.RUNNING),
        ).update(state=ScanRun.State.FENCED, settled_at=settled_at)
        RootIndexState.objects.filter(root_id__in=affected_roots).update(
            binding_epoch=next_epoch,
            policy_epoch=next_epoch,
            active_run=None,
            rescan_requested=True,
            status=RootIndexState.Status.NOT_INDEXED,
            updated_at=settled_at,
        )
        _advance_authorization_epochs(affected_roots, affected_users)
