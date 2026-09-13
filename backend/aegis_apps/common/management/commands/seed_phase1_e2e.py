from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from aegis_apps.identity.models import User
from aegis_apps.identity.validators import read_private_secret
from aegis_apps.roots.manifest import configured_manifest
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from aegis_apps.roots.services import create_root, set_group_grant, set_user_grant

_FIXTURE_ERROR = "phase1 fixture configuration is invalid"


class FixtureDrift(ValueError):
    """A synthetic fixture already exists with an unexpected shape."""


@dataclass(frozen=True, slots=True)
class FixtureUser:
    user: User
    created: bool


def _password(setting_name: str) -> str:
    value = getattr(settings, setting_name, "")
    if not isinstance(value, str) or not value:
        raise FixtureDrift
    return read_private_secret(Path(value))


def _ensure_user(
    *,
    username: str,
    email: str,
    password: str,
    is_staff: bool,
    is_superuser: bool,
) -> FixtureUser:
    user = User.objects.filter(username=username).first()
    if user is None:
        user = User(
            username=username,
            email=email,
            is_active=True,
            is_staff=is_staff,
            is_superuser=is_superuser,
        )
        user.set_password(password)
        user.full_clean(validate_unique=False)
        user.save(force_insert=True)
        return FixtureUser(user, True)

    expected = (
        user.email == email
        and user.first_name == ""
        and user.last_name == ""
        and user.is_active
        and user.is_staff is is_staff
        and user.is_superuser is is_superuser
        and not user.user_permissions.exists()
        and check_password(password, user.password, setter=None)
    )
    if not expected:
        raise FixtureDrift
    return FixtureUser(user, False)


def _ensure_group(bob: FixtureUser) -> Group:
    group, group_created = Group.objects.get_or_create(name="e2e-bob")
    if group.permissions.exists():
        raise FixtureDrift
    bob_group_ids = set(bob.user.groups.values_list("pk", flat=True))
    if bob_group_ids - {group.pk}:
        raise FixtureDrift
    member_ids = set(group.user_set.values_list("pk", flat=True))
    if bob.user.pk not in member_ids:
        if not (bob.created or group_created) or member_ids:
            raise FixtureDrift
        bob.user.groups.add(group)
        member_ids.add(bob.user.pk)
    if member_ids != {bob.user.pk}:
        raise FixtureDrift
    return group


def _ensure_root(*, actor: User, slot_id: str, display_name: str) -> Root:
    root = Root.objects.filter(slot_id=slot_id).first()
    if root is not None:
        if not (
            root.display_name == display_name
            and root.mode == Root.Mode.READ_ONLY
            and root.active
            and root.capabilities == {}
        ):
            raise FixtureDrift
        return root
    return create_root(
        actor=actor,
        slot_id=slot_id,
        display_name=display_name,
        mode=Root.Mode.READ_ONLY,
        active=True,
        request_id=f"phase1_e2e_root_{slot_id}",
    )


def _ensure_user_grant(*, actor: User, root: Root, user: User) -> RootGrant:
    grant = RootGrant.objects.filter(root=root, user=user).first()
    if grant is not None:
        if grant.group_id is not None or grant.permissions != int(Permission.BROWSE):
            raise FixtureDrift
        return grant
    return set_user_grant(
        actor=actor,
        root_id=root.pk,
        user_id=user.pk,
        permissions=Permission.BROWSE,
        request_id="phase1_e2e_alice_grant",
    )


def _ensure_group_grant(*, actor: User, root: Root, group: Group) -> RootGrant:
    grant = RootGrant.objects.filter(root=root, group=group).first()
    if grant is not None:
        if grant.user_id is not None or grant.permissions != int(Permission.BROWSE):
            raise FixtureDrift
        return grant
    return set_group_grant(
        actor=actor,
        root_id=root.pk,
        group_id=group.pk,
        permissions=Permission.BROWSE,
        request_id="phase1_e2e_bob_group_grant",
    )


def _assert_exact_scope(
    *, alice: User, bob: User, admin: User, group: Group, alice_root: Root, bob_root: Root
) -> None:
    if alice.groups.exists() or admin.groups.exists() or bob.groups.exclude(pk=group.pk).exists():
        raise FixtureDrift
    if RootGrant.objects.filter(user=bob).exists() or RootGrant.objects.filter(user=admin).exists():
        raise FixtureDrift
    if RootGrant.objects.filter(user=alice).exclude(root=alice_root).exists():
        raise FixtureDrift
    if RootGrant.objects.filter(group=group).exclude(root=bob_root).exists():
        raise FixtureDrift
    if RootGrant.objects.filter(root__in=(alice_root, bob_root)).count() != 2:
        raise FixtureDrift


class Command(BaseCommand):
    help = "Create deterministic, test-only Phase 1 browser fixtures."

    def handle(self, *args: object, **options: object) -> None:
        del args, options
        if settings.AEGIS_ENVIRONMENT != "test":
            raise CommandError("seed_phase1_e2e is restricted to the test environment")
        try:
            manifest = configured_manifest()
            if manifest is None or set(manifest.slots) != {"e2e-alice", "e2e-bob"}:
                raise FixtureDrift
            if any(slot.mode != "read_only" for slot in manifest.slots.values()):
                raise FixtureDrift
            alice_password = _password("E2E_ALICE_PASSWORD_FILE")
            bob_password = _password("E2E_BOB_PASSWORD_FILE")
            admin_password = _password("E2E_ADMIN_PASSWORD_FILE")
            with transaction.atomic():
                admin = _ensure_user(
                    username="phase1-admin",
                    email="phase1-admin@e2e.invalid",
                    password=admin_password,
                    is_staff=True,
                    is_superuser=True,
                )
                alice = _ensure_user(
                    username="alice",
                    email="alice@e2e.invalid",
                    password=alice_password,
                    is_staff=False,
                    is_superuser=False,
                )
                bob = _ensure_user(
                    username="bob",
                    email="bob@e2e.invalid",
                    password=bob_password,
                    is_staff=False,
                    is_superuser=False,
                )
                group = _ensure_group(bob)
                alice_root = _ensure_root(
                    actor=admin.user,
                    slot_id="e2e-alice",
                    display_name="Alice files",
                )
                bob_root = _ensure_root(
                    actor=admin.user,
                    slot_id="e2e-bob",
                    display_name="Bob files",
                )
                _ensure_user_grant(actor=admin.user, root=alice_root, user=alice.user)
                _ensure_group_grant(actor=admin.user, root=bob_root, group=group)
                _assert_exact_scope(
                    alice=alice.user,
                    bob=bob.user,
                    admin=admin.user,
                    group=group,
                    alice_root=alice_root,
                    bob_root=bob_root,
                )
        except (DatabaseError, ValidationError, ValueError):
            raise CommandError(_FIXTURE_ERROR) from None

        self.stdout.write("phase1 fixtures ready")
