from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from aegis_apps.common.database_privileges import (
    MANAGED_FUNCTION_SIGNATURES,
    MANAGED_SEQUENCES,
    MANAGED_TABLE_COLUMNS,
    RUNTIME_DATABASE_ROLES,
    synchronize_database_privileges,
)
from django.db import connection, transaction
from psycopg import sql

MIGRATOR_ROLE = "aegis_migrator"
ALL_TEST_ROLES = (MIGRATOR_ROLE, *RUNTIME_DATABASE_ROLES)
AclEntry = tuple[str, str, bool]
TEST_OWNED_FUNCTION_SIGNATURES = (
    *MANAGED_FUNCTION_SIGNATURES.values(),
    "public.aegis_directory_counter_guard()",
)


@dataclass(frozen=True, slots=True)
class RoleDatabase:
    database_name: str
    host: str
    port: int
    passwords: dict[str, str] = field(repr=False)

    @contextmanager
    def as_django_role(self, role: str) -> Iterator[None]:
        if role not in self.passwords:
            raise ValueError("unknown test database role")
        with _django_login(role, self.passwords[role]):
            yield

    def connect(self, role: str) -> psycopg.Connection[Any]:
        if role not in self.passwords:
            raise ValueError("unknown test database role")
        return psycopg.connect(
            dbname=self.database_name,
            host=self.host,
            port=self.port,
            user=role,
            password=self.passwords[role],
            connect_timeout=5,
            autocommit=True,
        )

    def synchronize(self) -> None:
        with _django_login(MIGRATOR_ROLE, self.passwords[MIGRATOR_ROLE]):
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT table_name, column_name
                      FROM information_schema.columns
                     WHERE table_schema = 'public'
                     ORDER BY table_name, ordinal_position
                    """
                )
                visible: dict[str, list[str]] = {}
                for table, column in cursor.fetchall():
                    visible.setdefault(table, []).append(column)
                actual_columns = {table: tuple(columns) for table, columns in visible.items()}
                assert actual_columns == MANAGED_TABLE_COLUMNS, actual_columns
                cursor.execute(
                    """
                    SELECT sequencename
                      FROM pg_catalog.pg_sequences
                     WHERE schemaname = 'public'
                     ORDER BY sequencename
                    """
                )
                actual_sequences = tuple(row[0] for row in cursor.fetchall())
                if actual_sequences != MANAGED_SEQUENCES:
                    cursor.execute(
                        """
                        SELECT relation.relname,
                               pg_catalog.pg_get_userbyid(relation.relowner)
                          FROM pg_catalog.pg_class AS relation
                          JOIN pg_catalog.pg_namespace AS namespace
                            ON namespace.oid = relation.relnamespace
                         WHERE namespace.nspname = 'public'
                           AND relation.relkind = 'S'
                         ORDER BY relation.relname
                        """
                    )
                    pytest.fail(
                        f"sequence visibility drift: {actual_sequences!r}; "
                        f"catalog: {cursor.fetchall()!r}"
                    )
            for _attempt in range(2):
                with transaction.atomic(durable=True):
                    synchronize_database_privileges()


def _quote(identifier: str) -> sql.Identifier:
    return sql.Identifier(identifier)


def _create_login_role(role: str, password: str) -> None:
    if role not in ALL_TEST_ROLES:
        raise ValueError("unknown test database role")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config(%s, %s, true)",
            ["aegis.test_role_password", password],
        )
        cursor.execute(
            sql.SQL(
                """
                DO $aegis_test_role$
                DECLARE
                    role_password text := pg_catalog.current_setting(
                        'aegis.test_role_password'
                    );
                BEGIN
                    IF role_password = ''
                       OR pg_catalog.octet_length(role_password) > 4096 THEN
                        RAISE EXCEPTION 'invalid test role password';
                    END IF;
                    EXECUTE pg_catalog.format(
                        'CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                        'NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
                        {role},
                        role_password
                    );
                END
                $aegis_test_role$;
                """
            ).format(role=sql.Literal(role))
        )


@contextmanager
def _django_login(role: str, password: str) -> Iterator[None]:
    settings_dict = connection.settings_dict
    original_user = settings_dict["USER"]
    original_password = settings_dict["PASSWORD"]
    connection.close()
    settings_dict["USER"] = role
    settings_dict["PASSWORD"] = password
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT session_user, current_user")
            assert cursor.fetchone() == (role, role)
        yield
    finally:
        connection.close()
        settings_dict["USER"] = original_user
        settings_dict["PASSWORD"] = original_password
        connection.ensure_connection()


def _alter_relation_owner(*, relation: str, kind: str, owner: str) -> None:
    keyword = "SEQUENCE" if kind == "S" else "TABLE"
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("ALTER {} {} OWNER TO {}").format(
                sql.SQL(keyword),
                sql.Identifier("public", relation),
                _quote(owner),
            )
        )


def _restore_and_drop_roles(
    *,
    original_database_owner: str,
    original_schema_owner: str,
    original_relation_owners: dict[str, tuple[str, str]],
    original_functions: dict[str, tuple[str, str]],
    original_database_acl: tuple[AclEntry, ...],
    original_schema_acl: tuple[AclEntry, ...],
) -> None:
    connection.close()
    connection.ensure_connection()
    with connection.cursor() as cursor:
        cursor.execute("RESET SESSION AUTHORIZATION")
        cursor.execute("RESET ROLE")

        for signature in TEST_OWNED_FUNCTION_SIGNATURES:
            cursor.execute(
                "SELECT to_regprocedure(%s)",
                [signature],
            )
            if signature in original_functions:
                definition, owner = original_functions[signature]
                # CREATE OR REPLACE preserves dependent objects and the function OID.
                cursor.execute(definition)
                cursor.execute(
                    sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
                        sql.SQL(signature),
                        _quote(owner),
                    )
                )
            elif cursor.fetchone() != (None,):
                cursor.execute(
                    sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
                        sql.SQL(signature),
                        _quote(original_schema_owner),
                    )
                )

        for relation, (kind, owner) in original_relation_owners.items():
            if kind == "S":
                continue
            _alter_relation_owner(relation=relation, kind=kind, owner=owner)
        for relation, (kind, owner) in original_relation_owners.items():
            if kind != "S":
                continue
            _alter_relation_owner(relation=relation, kind=kind, owner=owner)

        cursor.execute(
            sql.SQL("ALTER SCHEMA public OWNER TO {}").format(_quote(original_schema_owner))
        )
        cursor.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                _quote(connection.settings_dict["NAME"]),
                _quote(original_database_owner),
            )
        )

        for signature in TEST_OWNED_FUNCTION_SIGNATURES:
            if signature not in original_functions:
                cursor.execute(f"DROP FUNCTION IF EXISTS {signature}")

        for role in reversed(ALL_TEST_ROLES):
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s)",
                [role],
            )
            if cursor.fetchone() == (True,):
                cursor.execute(sql.SQL("DROP OWNED BY {}").format(_quote(role)))
                cursor.execute(sql.SQL("DROP ROLE {}").format(_quote(role)))

        _restore_acl(
            cursor,
            kind="DATABASE",
            target=str(connection.settings_dict["NAME"]),
            entries=original_database_acl,
        )
        _restore_acl(
            cursor,
            kind="SCHEMA",
            target="public",
            entries=original_schema_acl,
        )


def _acl_entries(cursor: Any, *, kind: str) -> tuple[AclEntry, ...]:
    if kind == "DATABASE":
        source = "database.datacl"
        fallback = "pg_catalog.acldefault('d', database.datdba)"
        join = ""
    elif kind == "SCHEMA":
        source = "namespace.nspacl"
        fallback = "pg_catalog.acldefault('n', namespace.nspowner)"
        join = "JOIN pg_catalog.pg_namespace AS namespace ON namespace.nspname = 'public'"
    else:
        raise ValueError("unknown ACL kind")
    cursor.execute(
        f"""
        SELECT CASE acl.grantee
                   WHEN 0 THEN 'PUBLIC'
                   ELSE pg_catalog.pg_get_userbyid(acl.grantee)
               END,
               acl.privilege_type,
               acl.is_grantable
          FROM pg_catalog.pg_database AS database
          {join}
          CROSS JOIN LATERAL pg_catalog.aclexplode(
              COALESCE({source}, {fallback})
          ) AS acl
         WHERE database.datname = pg_catalog.current_database()
         ORDER BY 1, 2, 3
        """
    )
    return tuple(cursor.fetchall())


def _restore_acl(
    cursor: Any,
    *,
    kind: str,
    target: str,
    entries: tuple[AclEntry, ...],
) -> None:
    if kind not in ("DATABASE", "SCHEMA"):
        raise ValueError("unknown ACL kind")
    _clear_acl(cursor, kind=kind, target=target, entries=entries)
    for grantee, privilege, grantable in entries:
        recipient = sql.SQL("PUBLIC") if grantee == "PUBLIC" else _quote(grantee)
        suffix = sql.SQL(" WITH GRANT OPTION") if grantable else sql.SQL("")
        cursor.execute(
            sql.SQL("GRANT {} ON {} {} TO {}{}").format(
                sql.SQL(privilege),
                sql.SQL(kind),
                sql.Identifier(target),
                recipient,
                suffix,
            )
        )


def _clear_acl(
    cursor: Any,
    *,
    kind: str,
    target: str,
    entries: tuple[AclEntry, ...],
) -> None:
    grantees = {entry[0] for entry in entries} | {"PUBLIC"}
    for grantee in sorted(grantees):
        recipient = sql.SQL("PUBLIC") if grantee == "PUBLIC" else _quote(grantee)
        cursor.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ON {} {} FROM {}").format(
                sql.SQL(kind), sql.Identifier(target), recipient
            )
        )


@contextmanager
def managed_role_database() -> Iterator[RoleDatabase]:
    created_roles: list[str] = []
    original_database_owner = ""
    original_schema_owner = ""
    original_relation_owners: dict[str, tuple[str, str]] = {}
    original_functions: dict[str, tuple[str, str]] = {}
    original_database_acl: tuple[AclEntry, ...] = ()
    original_schema_acl: tuple[AclEntry, ...] = ()
    connection.ensure_connection()
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_catalog.current_database(),
                   pg_catalog.current_setting('server_version_num')::integer,
                   pg_catalog.pg_get_userbyid(database.datdba),
                   pg_catalog.pg_get_userbyid(namespace.nspowner)
              FROM pg_catalog.pg_database AS database
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.nspname = 'public'
             WHERE database.datname = pg_catalog.current_database()
            """
        )
        row = cursor.fetchone()
        assert row is not None
        database_name, server_version, original_database_owner, original_schema_owner = row
        if not str(database_name).startswith("test_") or server_version // 10_000 != 18:
            pytest.fail("database-role tests require a disposable PostgreSQL 18 test database")

        cursor.execute(
            "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)",
            [list(ALL_TEST_ROLES)],
        )
        if cursor.fetchall():
            pytest.fail("database-role tests require no pre-existing Aegis database roles")

        original_database_acl = _acl_entries(cursor, kind="DATABASE")
        original_schema_acl = _acl_entries(cursor, kind="SCHEMA")

        cursor.execute(
            """
            SELECT relation.relname,
                   relation.relkind,
                   pg_catalog.pg_get_userbyid(relation.relowner)
              FROM pg_catalog.pg_class AS relation
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = relation.relnamespace
             WHERE namespace.nspname = 'public'
               AND (
                    relation.relname = ANY(%s)
                    OR relation.relname = ANY(%s)
               )
            """,
            [list(MANAGED_TABLE_COLUMNS), list(MANAGED_SEQUENCES)],
        )
        original_relation_owners = {name: (kind, owner) for name, kind, owner in cursor.fetchall()}
        if set(original_relation_owners) != set(MANAGED_TABLE_COLUMNS) | set(MANAGED_SEQUENCES):
            pytest.fail("managed database relation manifest is incomplete")

        for signature in TEST_OWNED_FUNCTION_SIGNATURES:
            cursor.execute(
                """
                SELECT pg_catalog.pg_get_functiondef(oid),
                       pg_catalog.pg_get_userbyid(proowner)
                  FROM pg_catalog.pg_proc
                 WHERE oid = pg_catalog.to_regprocedure(%s)
                """,
                [signature],
            )
            function = cursor.fetchone()
            if function is not None:
                original_functions[signature] = (function[0], function[1])

    passwords = {role: secrets.token_urlsafe(48) for role in ALL_TEST_ROLES}
    try:
        for role in ALL_TEST_ROLES:
            _create_login_role(role, passwords[role])
            created_roles.append(role)

        with connection.cursor() as cursor:
            for signature in original_functions:
                cursor.execute(
                    sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(
                        sql.SQL(signature),
                        _quote(MIGRATOR_ROLE),
                    )
                )

        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    _quote(database_name),
                    _quote(MIGRATOR_ROLE),
                )
            )
            cursor.execute(sql.SQL("ALTER SCHEMA public OWNER TO {}").format(_quote(MIGRATOR_ROLE)))
        for relation, (kind, _owner) in original_relation_owners.items():
            if kind == "S":
                continue
            _alter_relation_owner(
                relation=relation,
                kind=kind,
                owner=MIGRATOR_ROLE,
            )
        for relation, (kind, _owner) in original_relation_owners.items():
            if kind != "S":
                continue
            _alter_relation_owner(
                relation=relation,
                kind=kind,
                owner=MIGRATOR_ROLE,
            )

        with connection.cursor() as cursor:
            _clear_acl(
                cursor,
                kind="DATABASE",
                target=str(database_name),
                entries=original_database_acl,
            )
            _clear_acl(
                cursor,
                kind="SCHEMA",
                target="public",
                entries=original_schema_acl,
            )

        environment = RoleDatabase(
            database_name=str(database_name),
            host=str(connection.settings_dict["HOST"]),
            port=int(connection.settings_dict["PORT"]),
            passwords=passwords,
        )
        environment.synchronize()
        yield environment
    finally:
        if created_roles:
            _restore_and_drop_roles(
                original_database_owner=original_database_owner,
                original_schema_owner=original_schema_owner,
                original_relation_owners=original_relation_owners,
                original_functions=original_functions,
                original_database_acl=original_database_acl,
                original_schema_acl=original_schema_acl,
            )


@pytest.fixture(scope="module")
def role_database(
    django_db_setup: object,
    django_db_blocker: Any,
) -> Iterator[RoleDatabase]:
    del django_db_setup
    with django_db_blocker.unblock(), managed_role_database() as database:
        yield database
