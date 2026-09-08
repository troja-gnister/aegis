from __future__ import annotations

import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
from typing import Any
from unittest.mock import patch

import pytest
from aegis_apps.identity.admin_services import set_user_active
from aegis_apps.identity.models import User
from aegis_apps.operations.services import create_operation, validate_authorization_snapshot
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from aegis_apps.roots.services import set_group_grant, set_user_grant
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]
type _ExecuteQuery = Callable[[str, Any, bool, dict[str, Any]], Any]


def _root() -> Root:
    return Root.objects.create(
        slot_id=f"slot-{uuid.uuid4().hex[:16]}",
        display_name="Opaque root",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )


def _lock_table(sql: str) -> str | None:
    normalized = " ".join(sql.upper().split())
    if not normalized.startswith("SELECT") or "FOR UPDATE" not in normalized:
        return None
    for table in ("AUTH_GROUP", "ROOTS_ROOT", "IDENTITY_USER"):
        if f'FROM "{table}"' in normalized:
            return table.lower()
    return None


def test_operation_acceptance_and_user_grant_share_root_then_user_lock_order() -> None:
    actor = User.objects.create_user(username="operation-lock-actor")
    grant_actor = User.objects.create_superuser(username="operation-lock-admin")
    root = _root()
    RootGrant.objects.create(
        root=root,
        user=actor,
        permissions=int(Permission.BROWSE),
    )
    operation_root_locked = Event()
    grant_root_attempted = Event()
    first_operation_lock: list[str] = []
    first_lock_guard = Lock()

    def create() -> uuid.UUID:
        close_old_connections()
        try:
            thread_actor = User.objects.get(pk=actor.pk)

            def coordinate(
                execute: _ExecuteQuery,
                sql: str,
                params: Any,
                many: bool,
                context: dict[str, Any],
            ) -> Any:
                table = _lock_table(sql)
                if table is not None:
                    with first_lock_guard:
                        if not first_operation_lock:
                            first_operation_lock.append(table)
                            if table != "roots_root":
                                operation_root_locked.set()
                                raise AssertionError(
                                    "operation acceptance must lock roots before users"
                                )
                result = execute(sql, params, many, context)
                if table == "roots_root" and not operation_root_locked.is_set():
                    operation_root_locked.set()
                    assert grant_root_attempted.wait(timeout=5)
                return result

            with connection.execute_wrapper(coordinate):
                return create_operation(
                    actor=thread_actor,
                    request_id="task10_lock_acceptance",
                    kind="foundation.probe",
                    intent={
                        "roots": [
                            {
                                "id": str(root.id),
                                "permissions": int(Permission.BROWSE),
                            }
                        ]
                    },
                ).id
        finally:
            close_old_connections()

    def mutate_grant() -> uuid.UUID:
        close_old_connections()
        try:
            assert operation_root_locked.wait(timeout=5)
            thread_admin = User.objects.get(pk=grant_actor.pk)

            def observe_attempt(
                execute: _ExecuteQuery,
                sql: str,
                params: Any,
                many: bool,
                context: dict[str, Any],
            ) -> Any:
                if _lock_table(sql) == "roots_root":
                    grant_root_attempted.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe_attempt):
                return set_user_grant(
                    actor=thread_admin,
                    root_id=root.id,
                    user_id=actor.id,
                    permissions=Permission.BROWSE,
                    request_id="task10_lock_grant",
                ).id
        finally:
            close_old_connections()

    with (
        patch("aegis_apps.roots.services._validate_root_definition"),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        operation_future = executor.submit(create)
        grant_future = executor.submit(mutate_grant)
        operation_id = operation_future.result(timeout=10)
        grant_id = grant_future.result(timeout=10)

    assert isinstance(operation_id, uuid.UUID)
    assert isinstance(grant_id, uuid.UUID)
    assert first_operation_lock == ["roots_root"]


def test_active_user_mutation_and_snapshot_validation_share_root_then_user_order() -> None:
    actor = User.objects.create_superuser(username="active-lock-admin")
    subject = User.objects.create_user(username="active-lock-subject")
    root = _root()
    RootGrant.objects.create(
        root=root,
        user=subject,
        permissions=int(Permission.BROWSE),
    )
    operation = create_operation(
        actor=subject,
        request_id="task10_lock_snapshot",
        kind="foundation.probe",
        intent={
            "roots": [
                {"id": str(root.id), "permissions": int(Permission.BROWSE)}
            ]
        },
    )
    active_root_locked = Event()
    validation_root_attempted = Event()
    first_active_lock: list[str] = []

    def deactivate() -> bool:
        close_old_connections()
        try:
            thread_actor = User.objects.get(pk=actor.pk)

            def coordinate(
                execute: _ExecuteQuery,
                sql: str,
                params: Any,
                many: bool,
                context: dict[str, Any],
            ) -> Any:
                table = _lock_table(sql)
                if table is not None and not first_active_lock:
                    first_active_lock.append(table)
                    if table != "roots_root":
                        active_root_locked.set()
                        raise AssertionError(
                            "active-user mutation must lock roots before users"
                        )
                result = execute(sql, params, many, context)
                if table == "roots_root" and not active_root_locked.is_set():
                    active_root_locked.set()
                    assert validation_root_attempted.wait(timeout=5)
                return result

            with connection.execute_wrapper(coordinate):
                changed = set_user_active(
                    actor=thread_actor,
                    user_id=subject.id,
                    active=False,
                    request_id="task10_lock_deactivate",
                )
            return changed.is_active
        finally:
            close_old_connections()

    def validate() -> bool:
        close_old_connections()
        try:
            assert active_root_locked.wait(timeout=5)
            thread_operation = type(operation).objects.get(pk=operation.pk)

            def observe_attempt(
                execute: _ExecuteQuery,
                sql: str,
                params: Any,
                many: bool,
                context: dict[str, Any],
            ) -> Any:
                if _lock_table(sql) == "roots_root":
                    validation_root_attempted.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe_attempt):
                return validate_authorization_snapshot(thread_operation)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        active_future = executor.submit(deactivate)
        validation_future = executor.submit(validate)
        assert active_future.result(timeout=10) is False
        assert validation_future.result(timeout=10) is False

    assert first_active_lock == ["roots_root"]


def test_group_grant_locks_group_then_root_then_sorted_members() -> None:
    actor = User.objects.create_superuser(username="group-lock-admin")
    group = Group.objects.create(name="Lock protocol group")
    members = [
        User.objects.create_user(username="group-lock-member-b"),
        User.objects.create_user(username="group-lock-member-a"),
    ]
    group.user_set.add(*reversed(members))
    root = _root()
    lock_tables: list[str] = []
    user_lock_sql: list[str] = []

    def observe_locks(
        execute: _ExecuteQuery,
        sql: str,
        params: Any,
        many: bool,
        context: dict[str, Any],
    ) -> Any:
        table = _lock_table(sql)
        if table is not None:
            lock_tables.append(table)
            if table == "identity_user":
                user_lock_sql.append(" ".join(sql.upper().split()))
        return execute(sql, params, many, context)

    with (
        patch("aegis_apps.roots.services._validate_root_definition"),
        connection.execute_wrapper(observe_locks),
    ):
        set_group_grant(
            actor=actor,
            root_id=root.id,
            group_id=group.id,
            permissions=Permission.BROWSE,
            request_id="task10_group_lock",
        )

    assert lock_tables[:3] == ["auth_group", "roots_root", "identity_user"]
    assert user_lock_sql
    assert 'ORDER BY "IDENTITY_USER"."ID" ASC' in user_lock_sql[0]
