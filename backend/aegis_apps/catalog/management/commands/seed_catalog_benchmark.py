"""Test-only: seed a freshly owned, empty benchmark database with the synthetic catalog.

This is synthetic catalog setup for the 2A.1 benchmark, never an optimistic backfill
of operator data. It refuses non-test settings, any database or login other than the
one named by the per-run ownership record, and any database that already contains
identities, roots or catalog rows. There is deliberately no truncate/reset option.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections import Counter
from datetime import timedelta
from pathlib import Path
from typing import Any

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from aegis_apps.identity.models import User
from aegis_apps.identity.validators import read_private_secret
from aegis_apps.indexing.models import IndexDeployment, RootIndexState
from aegis_apps.roots.manifest import ManifestError, configured_manifest
from aegis_apps.roots.models import Root
from aegis_apps.roots.permissions import Permission
from aegis_apps.roots.services import create_root, set_group_grant, set_user_grant

MIGRATOR_ROLE = "aegis_migrator"
RECORD_KEYS = frozenset({
    "version", "token", "database", "role", "entries", "wideFolder", "seed", "users",
    "scheduleHoldSeconds",
})
TOKEN_RE = re.compile(r"[0-9a-f]{12}\Z", re.ASCII)
# A per-run benchmark database, or pytest-django's disposable test database.
DATABASE_RE = re.compile(r"(?:aegis_bench_[0-9a-f]{12}|test_[a-z0-9_]{1,50})\Z", re.ASCII)
FRESHNESS_TABLES = (
    "identity_user", "auth_group", "roots_root", "roots_rootgrant",
    "catalog_catalogentry", "indexing_rootindexstate", "indexing_scanrun",
)
MAX_HOLD_SECONDS = 30 * 86_400
POST_SEED_MAINTENANCE = "VACUUM (ANALYZE) public.catalog_catalogentry"
# Fixture control, recorded in the output: due times beyond the measurement window
# keep the real indexer from reconciling an empty synthetic mount over the seeded
# catalog. It is not a production skip-authentication/fencing flag or scan evidence.
FIXTURE_CONTROL = "root_index_state_due_beyond_measurement_window"


def _refuse(message: str) -> CommandError:
    return CommandError(message)


def _load_record(value: str) -> dict[str, Any]:
    invalid = "benchmark ownership record is invalid"
    path = Path(value)
    try:
        metadata = path.lstat()
        if (
            not path.is_absolute() or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077
            or metadata.st_nlink != 1 or metadata.st_size > 4096
        ):
            raise _refuse(invalid)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                raise _refuse(invalid)
            raw = os.read(descriptor, 4097)
        finally:
            os.close(descriptor)
        record = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        raise _refuse(invalid) from None
    if not isinstance(record, dict) or set(record) != RECORD_KEYS:
        raise _refuse(invalid)
    if (
        record["version"] != 1 or not isinstance(record["token"], str)
        or TOKEN_RE.fullmatch(record["token"]) is None
        or not isinstance(record["database"], str) or record["role"] != MIGRATOR_ROLE
        or type(record["users"]) is not int
        or type(record["scheduleHoldSeconds"]) is not int
        or not 60 <= record["scheduleHoldSeconds"] <= MAX_HOLD_SECONDS
    ):
        raise _refuse(invalid)
    from scripts.benchmarks.dataset import ROOTS, DatasetShape

    users = {user for root in ROOTS for user in (*root.direct_users, *root.group_users)}
    if record["users"] != len(users):
        raise _refuse(invalid)
    try:
        record["shape"] = DatasetShape(
            entries=record["entries"], wide_folder=record["wideFolder"], seed=record["seed"],
        )
    except ValueError:
        raise _refuse(invalid) from None
    return record


def _require_owned_database(record: dict[str, Any]) -> None:
    database = record["database"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_database(), session_user")
        row = cursor.fetchone()
    if (
        row is None or row[0] != database or DATABASE_RE.fullmatch(database) is None
        or (database.startswith("aegis_bench_") and database != f"aegis_bench_{record['token']}")
    ):
        raise _refuse("unexpected benchmark database")
    if row[1] != MIGRATOR_ROLE:
        raise _refuse("unexpected benchmark database role")


def _require_fresh() -> None:
    with connection.cursor() as cursor:
        for table in FRESHNESS_TABLES:
            cursor.execute(f"SELECT EXISTS (SELECT 1 FROM public.{table})")  # fixed names
            row = cursor.fetchone()
            if row is None or row[0]:
                raise _refuse("benchmark seed requires a fresh empty benchmark database")


def _binding() -> IndexDeployment:
    from scripts.benchmarks.dataset import ROOTS

    try:
        manifest = configured_manifest()
    except ManifestError:
        manifest = None
    deployment = IndexDeployment.objects.filter(pk=1).first()
    slots = sorted(root.slot_id for root in ROOTS)
    if (
        manifest is None or deployment is None or sorted(manifest.slots) != slots
        or deployment.manifest_identity != manifest.digest or deployment.slot_ids != slots
        or any(slot.mode != "read_only" for slot in manifest.slots.values())
    ):
        raise _refuse("benchmark mount binding is not ready")
    return deployment


def _identities(token: str, password: str) -> tuple[User, dict[int, User], dict[str, Root]]:
    from scripts.benchmarks.dataset import ADMIN_USERNAME, ROOTS, username

    admin = User(username=ADMIN_USERNAME, email=f"{ADMIN_USERNAME}@benchmark.invalid",
                 is_active=True, is_staff=True, is_superuser=True)
    admin.set_password(password)
    admin.save(force_insert=True)
    numbers = sorted({user for root in ROOTS for user in (*root.direct_users, *root.group_users)})
    users: dict[int, User] = {}
    for number in numbers:
        user = User(username=username(number), email=f"{username(number)}@benchmark.invalid",
                    is_active=True)
        user.set_password(password)
        user.save(force_insert=True)
        users[number] = user
    roots: dict[str, Root] = {}
    for spec in ROOTS:
        root = create_root(
            actor=admin, slot_id=spec.slot_id, display_name=spec.display_name,
            mode=Root.Mode.READ_ONLY, active=True, request_id=f"bench_root_{spec.key}_{token}",
        )
        roots[spec.key] = root
        for number in spec.direct_users:
            set_user_grant(
                actor=admin, root_id=root.pk, user_id=users[number].pk,
                permissions=Permission.BROWSE,
                request_id=f"bench_grant_{spec.key}_{number}_{token}",
            )
        if spec.group_name is not None:
            group = Group.objects.create(name=spec.group_name)
            for number in spec.group_users:
                users[number].groups.add(group)
            set_group_grant(
                actor=admin, root_id=root.pk, group_id=group.pk, permissions=Permission.BROWSE,
                request_id=f"bench_group_{spec.key}_{token}",
            )
    return admin, users, roots


def _copy_catalog(shape: Any, roots: dict[str, Root]) -> dict[str, Any]:
    from scripts.benchmarks.dataset import COLUMNS, catalog_records

    kinds: Counter[str] = Counter()
    states: Counter[str] = Counter()
    per_root: Counter[str] = Counter()
    directories: Counter[str] = Counter()
    root_ids = {key: root.pk for key, root in roots.items()}
    total = 0
    with connection.cursor() as cursor:
        # Parents stream before children; check each foreign key as rows arrive instead
        # of queuing a million deferred trigger events until commit.
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
        raw = cursor.cursor
        statement = f"COPY public.catalog_catalogentry ({', '.join(COLUMNS)}) FROM STDIN"
        with raw.copy(statement) as copy:
            for record in catalog_records(shape):
                root = str(record["root"])
                values = {**record, "root_id": root_ids[root]}
                copy.write_row(tuple(values[column] for column in COLUMNS))
                total += 1
                kinds[str(record["kind"])] += 1
                states[str(record["source_state"])] += 1
                per_root[root] += 1
                if record["kind"] == "directory":
                    directories[root] += 1
    return {
        "entries": total, "kinds": dict(sorted(kinds.items())),
        "states": dict(sorted(states.items())), "roots": dict(sorted(per_root.items())),
        "directories": dict(sorted(directories.items())),
    }


class Command(BaseCommand):
    help = "Test-only: seed one freshly owned, empty benchmark database (no reset option)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--ownership-record", required=True)
        parser.add_argument("--password-file", required=True)

    def handle(self, *args: object, **options: Any) -> None:
        del args
        if (
            settings.AEGIS_ENVIRONMENT != "test"
            or getattr(settings, "SETTINGS_MODULE", "") != "aegis.settings.test"
        ):
            raise _refuse("seed_catalog_benchmark is restricted to the test environment")
        try:
            record = _load_record(str(options["ownership_record"]))
        except ImportError:
            raise _refuse("benchmark dataset module is unavailable") from None
        _require_owned_database(record)
        try:
            password = read_private_secret(Path(str(options["password_file"])))
        except ValueError:
            raise _refuse("benchmark password file is invalid") from None
        shape = record["shape"]
        try:
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [0x2A15EED])
                _require_fresh()
                deployment = _binding()
                _admin, _users, roots = _identities(record["token"], password)
                summary = _copy_catalog(shape, roots)
                if summary["entries"] != shape.entries:
                    raise _refuse("benchmark seed count mismatch")
                hold = timedelta(seconds=record["scheduleHoldSeconds"])
                now = timezone.now()
                for key, root in roots.items():
                    RootIndexState.objects.create(
                        root=root, binding_epoch=deployment.epoch,
                        policy_epoch=deployment.epoch, next_generation=2,
                        due_at=now + hold, status=RootIndexState.Status.READY,
                        observed_entries=summary["roots"][key],
                        completed_directories=summary["directories"][key],
                        degraded_directories=0, last_completed_at=now,
                    )
        except CommandError:
            raise
        except (DatabaseError, ValueError) as error:
            raise _refuse("benchmark seed failed") from error
        # Steady-state statistics/visibility that autovacuum would otherwise reach
        # minutes later; recorded in the summary as an explicit fixture step.
        with connection.cursor() as cursor:
            cursor.execute(POST_SEED_MAINTENANCE)
        self.stdout.write(json.dumps({
            "version": 1, "token": record["token"], "fixtureControl": FIXTURE_CONTROL,
            "scheduleHoldSeconds": record["scheduleHoldSeconds"],
            "postSeedMaintenance": POST_SEED_MAINTENANCE,
            "shape": {"entries": shape.entries, "wideFolder": shape.wide_folder,
                      "seed": shape.seed},
            "users": record["users"], "roots": {key: str(root.pk) for key, root in roots.items()},
            "counts": summary,
        }, sort_keys=True))
