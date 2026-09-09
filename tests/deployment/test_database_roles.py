from __future__ import annotations

import secrets
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import psycopg
import pytest
from aegis_apps.audit.services import record_event
from aegis_apps.common.database_privileges import (
    MANAGED_FUNCTION_IDENTITY_ARGUMENTS,
    MANAGED_FUNCTION_SIGNATURES,
    MANAGED_SEQUENCES,
    MANAGED_TABLE_COLUMNS,
    ROLE_COLUMN_PRIVILEGES,
    ROLE_FUNCTION_PRIVILEGES,
    ROLE_SEQUENCE_PRIVILEGES,
    ROLE_TABLE_PRIVILEGES,
    RUNTIME_DATABASE_ROLES,
    PrivilegeSynchronizationError,
    synchronize_database_privileges,
)
from aegis_apps.identity.models import User
from aegis_apps.operations.models import Operation
from aegis_apps.operations.services import create_operation, enqueue_job
from aegis_apps.roots.models import Root, RootGrant
from django.db import connection, transaction
from psycopg import sql
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

MIGRATOR_ROLE = "aegis_migrator"
ALL_TEST_ROLES = (MIGRATOR_ROLE, *RUNTIME_DATABASE_ROLES)
HEARTBEAT_FUNCTIONS = {
    "operations": "aegis_publish_operations_heartbeat",
    "indexer": "aegis_publish_indexer_heartbeat",
    "media": "aegis_publish_media_heartbeat",
}
HEARTBEAT_ARGUMENTS = "%s, %s, %s, %s, %s, %s, %s, %s, %s"


@dataclass(frozen=True, slots=True)
class RoleDatabase:
    database_name: str
    host: str
    port: int
    passwords: dict[str, str] = field(repr=False)

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
                actual_columns = {
                    table: tuple(columns) for table, columns in visible.items()
                }
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
) -> None:
    connection.close()
    connection.ensure_connection()
    with connection.cursor() as cursor:
        cursor.execute("RESET SESSION AUTHORIZATION")
        cursor.execute("RESET ROLE")

        for signature in MANAGED_FUNCTION_SIGNATURES.values():
            cursor.execute(
                "SELECT to_regprocedure(%s)",
                [signature],
            )
            if cursor.fetchone() != (None,):
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
            sql.SQL("ALTER SCHEMA public OWNER TO {}").format(
                _quote(original_schema_owner)
            )
        )
        cursor.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                _quote(connection.settings_dict["NAME"]),
                _quote(original_database_owner),
            )
        )

        for signature in MANAGED_FUNCTION_SIGNATURES.values():
            cursor.execute(f"DROP FUNCTION IF EXISTS {signature}")

        for role in reversed(ALL_TEST_ROLES):
            cursor.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = %s)",
                [role],
            )
            if cursor.fetchone() == (True,):
                cursor.execute(
                    sql.SQL("DROP OWNED BY {}").format(_quote(role))
                )
                cursor.execute(sql.SQL("DROP ROLE {}").format(_quote(role)))


@pytest.fixture(scope="module")
def role_database(
    django_db_setup: object,
    django_db_blocker: Any,
) -> Iterator[RoleDatabase]:
    del django_db_setup
    created_roles: list[str] = []
    original_database_owner = ""
    original_schema_owner = ""
    original_relation_owners: dict[str, tuple[str, str]] = {}
    with django_db_blocker.unblock():
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
                pytest.fail(
                    "database-role tests require no pre-existing Aegis database roles"
                )

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
            original_relation_owners = {
                name: (kind, owner) for name, kind, owner in cursor.fetchall()
            }
            if set(original_relation_owners) != set(MANAGED_TABLE_COLUMNS) | set(
                MANAGED_SEQUENCES
            ):
                pytest.fail("managed database relation manifest is incomplete")

        passwords = {role: secrets.token_urlsafe(48) for role in ALL_TEST_ROLES}
        try:
            for role in ALL_TEST_ROLES:
                _create_login_role(role, passwords[role])
                created_roles.append(role)

            with connection.cursor() as cursor:
                cursor.execute(
                    sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                        _quote(database_name),
                        _quote(MIGRATOR_ROLE),
                    )
                )
                cursor.execute(
                    sql.SQL("ALTER SCHEMA public OWNER TO {}").format(
                        _quote(MIGRATOR_ROLE)
                    )
                )
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
                )


def _assert_sqlstate(
    role_connection: psycopg.Connection[Any],
    statement: str,
    parameters: tuple[object, ...] = (),
    *,
    sqlstate: str = "42501",
) -> None:
    with pytest.raises(psycopg.Error) as caught, role_connection.cursor() as cursor:
        cursor.execute(statement, parameters)
    assert caught.value.sqlstate == sqlstate


def _heartbeat_parameters(
    *,
    worker_id: str | None = None,
    status: str = "idle",
    metrics: object = None,
    current_job_id: uuid.UUID | None = None,
    retention_seconds: int = 60,
    slot_limit: int = 16,
) -> tuple[object, ...]:
    return (
        str(uuid.uuid4()) if worker_id is None else worker_id,
        "task11-test",
        "schema-test",
        "a" * 64,
        status,
        Jsonb({} if metrics is None else metrics),
        current_job_id,
        retention_seconds,
        slot_limit,
    )


def _heartbeat_statement(role: str) -> str:
    try:
        function_name = HEARTBEAT_FUNCTIONS[role]
    except KeyError:
        raise ValueError("unknown worker role") from None
    return f"SELECT public.{function_name}({HEARTBEAT_ARGUMENTS})"


def _publish_heartbeat(
    role_connection: psycopg.Connection[Any],
    *,
    function_role: str,
    parameters: tuple[object, ...],
) -> uuid.UUID:
    with role_connection.cursor() as cursor:
        cursor.execute(_heartbeat_statement(function_role), parameters)
        row = cursor.fetchone()
    assert row is not None and isinstance(row[0], uuid.UUID)
    return row[0]


def _authorization_is_valid(
    role_connection: psycopg.Connection[Any],
    operation_id: uuid.UUID,
) -> bool:
    with role_connection.cursor() as cursor:
        cursor.execute(
            "SELECT public.aegis_validate_operation_authorization(%s)",
            [operation_id],
        )
        row = cursor.fetchone()
    assert row is not None and isinstance(row[0], bool)
    return row[0]


def _create_authorized_operation() -> tuple[User, Root, Operation]:
    unique = uuid.uuid4().hex
    actor = User.objects.create_user(username=f"authorization-{unique}")
    root = Root.objects.create(
        slot_id=f"r{unique}",
        display_name="Task 11 authorization root",
        mode=Root.Mode.READ_ONLY,
        active=True,
        capabilities={},
    )
    RootGrant.objects.create(root=root, user=actor, permissions=3)
    operation = create_operation(
        actor=actor,
        request_id=f"auth_{unique}",
        kind="foundation.probe",
        intent={"roots": [{"id": str(root.id), "permissions": 3}]},
    )
    return actor, root, operation


def _clear_worker_heartbeats() -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM public.operations_workerheartbeat")


def test_sync_is_idempotent_only_for_an_authenticated_migrator(
    role_database: RoleDatabase,
) -> None:
    with role_database.connect(MIGRATOR_ROLE) as migrator, migrator.cursor() as cursor:
        cursor.execute("SELECT session_user, current_user")
        assert cursor.fetchone() == (MIGRATOR_ROLE, MIGRATOR_ROLE)

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(_quote(MIGRATOR_ROLE)))
        with pytest.raises(PrivilegeSynchronizationError, match="migrator login"):
            synchronize_database_privileges()


def test_role_attributes_memberships_and_ownership_are_exact(
    role_database: RoleDatabase,
) -> None:
    del role_database
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolinherit, rolcreaterole,
                   rolcreatedb, rolreplication, rolbypassrls
              FROM pg_catalog.pg_roles
             WHERE rolname = ANY(%s)
             ORDER BY rolname
            """,
            [list(ALL_TEST_ROLES)],
        )
        assert cursor.fetchall() == [
            (role, True, False, False, False, False, False, False)
            for role in sorted(ALL_TEST_ROLES)
        ]
        cursor.execute(
            """
            SELECT member.rolname, granted.rolname
              FROM pg_catalog.pg_auth_members AS membership
              JOIN pg_catalog.pg_roles AS member ON member.oid = membership.member
              JOIN pg_catalog.pg_roles AS granted ON granted.oid = membership.roleid
             WHERE member.rolname = ANY(%s) OR granted.rolname = ANY(%s)
            """,
            [list(RUNTIME_DATABASE_ROLES), list(RUNTIME_DATABASE_ROLES)],
        )
        assert cursor.fetchall() == []
        cursor.execute(
            """
            SELECT pg_catalog.pg_get_userbyid(database.datdba),
                   pg_catalog.pg_get_userbyid(namespace.nspowner)
              FROM pg_catalog.pg_database AS database
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.nspname = 'public'
             WHERE database.datname = pg_catalog.current_database()
            """
        )
        assert cursor.fetchone() == (MIGRATOR_ROLE, MIGRATOR_ROLE)
        cursor.execute(
            """
            SELECT relation.relname, pg_catalog.pg_get_userbyid(relation.relowner)
              FROM pg_catalog.pg_class AS relation
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = relation.relnamespace
             WHERE namespace.nspname = 'public'
               AND (relation.relname = ANY(%s) OR relation.relname = ANY(%s))
            """,
            [list(MANAGED_TABLE_COLUMNS), list(MANAGED_SEQUENCES)],
        )
        assert {
            name: owner for name, owner in cursor.fetchall()
        } == {
            name: MIGRATOR_ROLE
            for name in (*MANAGED_TABLE_COLUMNS, *MANAGED_SEQUENCES)
        }


def test_sync_rejects_unsafe_migrator_attributes(
    role_database: RoleDatabase,
) -> None:
    with connection.cursor() as cursor:
        cursor.execute("ALTER ROLE aegis_migrator CREATEDB")
    try:
        with pytest.raises(
            PrivilegeSynchronizationError,
            match="migrator database role attributes are unsafe",
        ):
            role_database.synchronize()
    finally:
        with connection.cursor() as cursor:
            cursor.execute("ALTER ROLE aegis_migrator NOCREATEDB")
        role_database.synchronize()


def test_table_column_sequence_and_function_grants_match_the_allowlist(
    role_database: RoleDatabase,
) -> None:
    for role in RUNTIME_DATABASE_ROLES:
        with (
            role_database.connect(role) as role_connection,
            role_connection.cursor() as cursor,
        ):
            cursor.execute("SELECT session_user, current_user")
            assert cursor.fetchone() == (role, role)
            cursor.execute("SELECT count(*) FROM public.django_migrations")
            count_row = cursor.fetchone()
            assert count_row is not None and count_row[0] > 0

    with connection.cursor() as cursor:
        for role in RUNTIME_DATABASE_ROLES:
            cursor.execute(
                "SELECT has_database_privilege(%s, current_database(), 'CONNECT'), "
                "has_database_privilege(%s, current_database(), 'CREATE'), "
                "has_database_privilege(%s, current_database(), 'TEMPORARY'), "
                "has_schema_privilege(%s, 'public', 'USAGE'), "
                "has_schema_privilege(%s, 'public', 'CREATE')",
                [role, role, role, role, role],
            )
            assert cursor.fetchone() == (True, False, False, True, False)

            for table, columns in MANAGED_TABLE_COLUMNS.items():
                table_grants = ROLE_TABLE_PRIVILEGES[role].get(table, ())
                column_grants = ROLE_COLUMN_PRIVILEGES[role].get(table, {})
                for privilege in (
                    "SELECT",
                    "INSERT",
                    "UPDATE",
                    "DELETE",
                    "TRUNCATE",
                    "REFERENCES",
                    "TRIGGER",
                ):
                    cursor.execute(
                        "SELECT has_table_privilege(%s, %s, %s)",
                        [role, f"public.{table}", privilege],
                    )
                    assert cursor.fetchone() == (privilege in table_grants,)
                for column in columns:
                    for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                        expected_column_grant = (
                            privilege in table_grants
                            or privilege in column_grants.get(column, ())
                        )
                        cursor.execute(
                            "SELECT has_column_privilege(%s, %s, %s, %s)",
                            [role, f"public.{table}", column, privilege],
                        )
                        assert cursor.fetchone() == (expected_column_grant,)

            for sequence in MANAGED_SEQUENCES:
                expected_sequence_grants = ROLE_SEQUENCE_PRIVILEGES[role].get(
                    sequence, ()
                )
                for privilege in ("USAGE", "SELECT", "UPDATE"):
                    cursor.execute(
                        "SELECT has_sequence_privilege(%s, %s, %s)",
                        [role, f"public.{sequence}", privilege],
                    )
                    assert cursor.fetchone() == (privilege in expected_sequence_grants,)

            for function, signature in MANAGED_FUNCTION_SIGNATURES.items():
                cursor.execute(
                    "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                    [role, signature],
                )
                assert cursor.fetchone() == (
                    function in ROLE_FUNCTION_PRIVILEGES[role],
                )

        cursor.execute(
            """
            SELECT function.proname,
                   function.prosecdef,
                   pg_catalog.pg_get_userbyid(function.proowner),
                   function.proconfig,
                   pg_catalog.oidvectortypes(function.proargtypes)
              FROM pg_catalog.pg_proc AS function
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.oid = function.pronamespace
             WHERE namespace.nspname = 'public'
               AND function.proname = ANY(%s)
             ORDER BY function.proname
            """,
            [list(MANAGED_FUNCTION_SIGNATURES)],
        )
        rows = cursor.fetchall()
        assert len(rows) == len(MANAGED_FUNCTION_SIGNATURES)
        for name, security_definer, owner, config, arguments in rows:
            assert (
                security_definer,
                owner,
                tuple(config or ()),
                arguments,
            ) == (
                True,
                MIGRATOR_ROLE,
                ('search_path=""',),
                MANAGED_FUNCTION_IDENTITY_ARGUMENTS[name],
            )


def test_sync_removes_every_public_managed_object_grant(
    role_database: RoleDatabase,
) -> None:
    database_name = role_database.database_name
    with connection.cursor() as cursor:
        cursor.execute("GRANT SELECT ON public.operations_job TO PUBLIC")
        cursor.execute(
            "GRANT UPDATE (intent) ON public.operations_operation TO PUBLIC"
        )
        cursor.execute("GRANT SELECT ON public.auth_group_id_seq TO PUBLIC")
        cursor.execute("GRANT USAGE, CREATE ON SCHEMA public TO PUBLIC")
        cursor.execute(
            sql.SQL("GRANT CONNECT, TEMPORARY ON DATABASE {} TO PUBLIC").format(
                _quote(database_name)
            )
        )

    role_database.synchronize()

    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT has_database_privilege('public', current_database(), 'CONNECT'), "
            "has_database_privilege('public', current_database(), 'CREATE'), "
            "has_database_privilege('public', current_database(), 'TEMPORARY'), "
            "has_schema_privilege('public', 'public', 'USAGE'), "
            "has_schema_privilege('public', 'public', 'CREATE')"
        )
        assert cursor.fetchone() == (False, False, False, False, False)
        for table, columns in MANAGED_TABLE_COLUMNS.items():
            for privilege in (
                "SELECT",
                "INSERT",
                "UPDATE",
                "DELETE",
                "TRUNCATE",
                "REFERENCES",
                "TRIGGER",
            ):
                cursor.execute(
                    "SELECT has_table_privilege('public', %s, %s)",
                    [f"public.{table}", privilege],
                )
                assert cursor.fetchone() == (False,)
            for column in columns:
                for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    cursor.execute(
                        "SELECT has_column_privilege('public', %s, %s, %s)",
                        [f"public.{table}", column, privilege],
                    )
                    assert cursor.fetchone() == (False,)
        for sequence in MANAGED_SEQUENCES:
            for privilege in ("USAGE", "SELECT", "UPDATE"):
                cursor.execute(
                    "SELECT has_sequence_privilege('public', %s, %s)",
                    [f"public.{sequence}", privilege],
                )
                assert cursor.fetchone() == (False,)


def test_actual_roles_enforce_representative_write_and_read_denials(
    role_database: RoleDatabase,
) -> None:
    actor = User.objects.create_user(username=f"role-boundary-{uuid.uuid4()}")
    operation = create_operation(
        actor=actor,
        request_id=f"roles_{uuid.uuid4().hex}",
        kind="foundation.probe",
        intent={"roots": []},
    )
    job = operation.jobs.get()
    audit = record_event(
        event_type="database.role.test",
        outcome="success",
        actor=actor,
        request_id=f"audit_{uuid.uuid4().hex}",
        metadata={},
    )

    with role_database.connect("aegis_web") as web:
        with web.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM public.operations_operation WHERE id = %s FOR UPDATE",
                [operation.id],
            )
            assert cursor.fetchone() == (operation.id,)
            cursor.execute(
                "INSERT INTO public.auth_group (name) VALUES (%s) RETURNING id",
                [f"web-role-{uuid.uuid4()}"],
            )
            group_row = cursor.fetchone()
            assert group_row is not None
            group_id = group_row[0]
            cursor.execute(
                "UPDATE public.auth_group SET name = %s WHERE id = %s",
                [f"web-updated-{uuid.uuid4()}", group_id],
            )
        _assert_sqlstate(
            web,
            "DELETE FROM public.auth_group WHERE id = %s",
            (group_id,),
        )
        _assert_sqlstate(
            web,
            "UPDATE public.operations_operation SET id = id WHERE id = %s",
            (operation.id,),
            sqlstate="55000",
        )
        _assert_sqlstate(
            web,
            "UPDATE public.operations_operation SET intent = '{}' WHERE id = %s",
            (operation.id,),
        )
        _assert_sqlstate(
            web,
            "UPDATE public.operations_job SET execution_started_at = now() WHERE id = %s",
            (job.id,),
        )
        _assert_sqlstate(
            web,
            "UPDATE public.audit_auditevent SET outcome = 'failure' WHERE id = %s",
            (audit.id,),
        )
        _assert_sqlstate(web, "CREATE TABLE public.forbidden_web_table (id integer)")

    with role_database.connect("aegis_operations") as worker:
        for table in (
            "identity_user",
            "identity_user_groups",
            "auth_group",
            "auth_group_permissions",
            "roots_rootgrant",
        ):
            _assert_sqlstate(worker, f"SELECT * FROM public.{table} LIMIT 1")
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job SET payload = '{}' WHERE id = %s",
            (job.id,),
        )
        _assert_sqlstate(
            worker,
            "INSERT INTO public.operations_workerheartbeat "
            "(id, role, worker_id, release_id, schema_identity, manifest_identity, "
            "last_seen_at, current_job_id, status, metrics) VALUES "
            "(%s, 'operations', %s, 'release', 'schema', %s, now(), NULL, 'idle', '{}')",
            (uuid.uuid4(), str(uuid.uuid4()), "a" * 64),
        )
        _assert_sqlstate(
            worker,
            "DELETE FROM public.audit_auditevent WHERE id = %s",
            (audit.id,),
        )
        _assert_sqlstate(worker, "CREATE TABLE public.forbidden_worker_table (id integer)")


@pytest.mark.parametrize(
    ("role", "other_role"),
    (
        ("operations", "indexer"),
        ("indexer", "media"),
        ("media", "operations"),
    ),
)
def test_each_worker_can_publish_only_its_own_heartbeat(
    role_database: RoleDatabase,
    role: str,
    other_role: str,
) -> None:
    worker_id = str(uuid.uuid4())
    parameters = _heartbeat_parameters(
        worker_id=worker_id,
        metrics={"queueAgeSeconds": 1},
    )
    with role_database.connect(f"aegis_{role}") as worker:
        heartbeat_id = _publish_heartbeat(
            worker,
            function_role=role,
            parameters=parameters,
        )
        with worker.cursor() as cursor:
            cursor.execute(
                "SELECT role, worker_id, status, metrics "
                "FROM public.operations_workerheartbeat WHERE id = %s",
                [heartbeat_id],
            )
            assert cursor.fetchone() == (
                role,
                worker_id,
                "idle",
                {"queueAgeSeconds": 1},
            )
        _assert_sqlstate(
            worker,
            _heartbeat_statement(other_role),
            _heartbeat_parameters(),
        )

    with role_database.connect("aegis_web") as web:
        _assert_sqlstate(
            web,
            _heartbeat_statement(role),
            _heartbeat_parameters(),
        )


def test_heartbeat_function_rejects_null_malformed_and_mismatched_inputs(
    role_database: RoleDatabase,
) -> None:
    statement = _heartbeat_statement("operations")
    with role_database.connect("aegis_operations") as worker:
        valid = list(_heartbeat_parameters())
        for required_index in (0, 1, 2, 3, 4, 5, 7, 8):
            parameters = valid.copy()
            parameters[required_index] = None
            _assert_sqlstate(
                worker,
                statement,
                tuple(parameters),
                sqlstate="22023",
            )

        malformed_values: tuple[tuple[int, object], ...] = (
            (0, "not-a-worker-uuid"),
            (1, ""),
            (3, "not-a-manifest"),
            (4, "unknown"),
            (5, Jsonb({"diskPressure": 2})),
            (7, 0),
            (8, 0),
        )
        for parameter_index, malformed_value in malformed_values:
            parameters = valid.copy()
            parameters[parameter_index] = malformed_value
            _assert_sqlstate(
                worker,
                statement,
                tuple(parameters),
                sqlstate="22023",
            )

        actor = User.objects.create_user(username=f"heartbeat-job-{uuid.uuid4()}")
        operation = create_operation(
            actor=actor,
            request_id=f"heartbeat_{uuid.uuid4().hex}",
            kind="foundation.probe",
            intent={"roots": []},
        )
        queued_job = operation.jobs.get()
        mismatched_job = list(
            _heartbeat_parameters(
                worker_id=str(uuid.uuid4()),
                status="running",
                current_job_id=queued_job.id,
            )
        )
        _assert_sqlstate(
            worker,
            statement,
            tuple(mismatched_job),
            sqlstate="22023",
        )


def test_stopping_heartbeat_status_is_monotonic(
    role_database: RoleDatabase,
) -> None:
    worker_id = str(uuid.uuid4())
    with role_database.connect("aegis_operations") as worker:
        heartbeat_id = _publish_heartbeat(
            worker,
            function_role="operations",
            parameters=_heartbeat_parameters(
                worker_id=worker_id,
                metrics={"queueAgeSeconds": 1},
            ),
        )
        assert _publish_heartbeat(
            worker,
            function_role="operations",
            parameters=_heartbeat_parameters(
                worker_id=worker_id,
                status="stopping",
                metrics={"queueAgeSeconds": 2},
            ),
        ) == heartbeat_id
        assert _publish_heartbeat(
            worker,
            function_role="operations",
            parameters=_heartbeat_parameters(
                worker_id=worker_id,
                status="idle",
                metrics={"queueAgeSeconds": 3},
            ),
        ) == heartbeat_id
        with worker.cursor() as cursor:
            cursor.execute(
                "SELECT status, metrics "
                "FROM public.operations_workerheartbeat WHERE id = %s",
                [heartbeat_id],
            )
            assert cursor.fetchone() == (
                "stopping",
                {"queueAgeSeconds": 2},
            )


def test_heartbeat_capacity_and_recycling_are_role_scoped(
    role_database: RoleDatabase,
) -> None:
    _clear_worker_heartbeats()
    operations_worker = str(uuid.uuid4())
    indexer_worker = str(uuid.uuid4())
    with (
        role_database.connect("aegis_operations") as operations,
        role_database.connect("aegis_indexer") as indexer,
    ):
        operations_id = _publish_heartbeat(
            operations,
            function_role="operations",
            parameters=_heartbeat_parameters(
                worker_id=operations_worker,
                slot_limit=1,
            ),
        )
        indexer_id = _publish_heartbeat(
            indexer,
            function_role="indexer",
            parameters=_heartbeat_parameters(
                worker_id=indexer_worker,
                slot_limit=1,
            ),
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE public.operations_workerheartbeat "
                "SET last_seen_at = pg_catalog.clock_timestamp() - interval '2 minutes' "
                "WHERE id = %s",
                [indexer_id],
            )
        _assert_sqlstate(
            operations,
            _heartbeat_statement("operations"),
            _heartbeat_parameters(slot_limit=1),
            sqlstate="P0001",
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE public.operations_workerheartbeat "
                "SET last_seen_at = pg_catalog.clock_timestamp() - interval '2 minutes' "
                "WHERE id = %s",
                [operations_id],
            )
        replacement_worker = str(uuid.uuid4())
        assert _publish_heartbeat(
            operations,
            function_role="operations",
            parameters=_heartbeat_parameters(
                worker_id=replacement_worker,
                slot_limit=1,
            ),
        ) == operations_id

        with operations.cursor() as cursor:
            cursor.execute(
                "SELECT id, role, worker_id "
                "FROM public.operations_workerheartbeat ORDER BY role"
            )
            assert cursor.fetchall() == [
                (indexer_id, "indexer", indexer_worker),
                (operations_id, "operations", replacement_worker),
            ]


def test_opaque_authorization_is_scoped_to_assigned_worker_roles(
    role_database: RoleDatabase,
) -> None:
    _actor, _root, operation = _create_authorized_operation()
    with role_database.connect("aegis_operations") as operations:
        assert _authorization_is_valid(operations, operation.id) is True
    with role_database.connect("aegis_indexer") as indexer:
        assert _authorization_is_valid(indexer, operation.id) is False
    with role_database.connect("aegis_media") as media:
        assert _authorization_is_valid(media, operation.id) is False

    enqueue_job(operation=operation, target_role="indexer")
    with role_database.connect("aegis_indexer") as indexer:
        assert _authorization_is_valid(indexer, operation.id) is True
    with role_database.connect("aegis_media") as media:
        assert _authorization_is_valid(media, operation.id) is False

    for role in ("aegis_operations", "aegis_indexer", "aegis_media"):
        with role_database.connect(role) as worker:
            _assert_sqlstate(
                worker,
                "SELECT * FROM public.roots_rootgrant LIMIT 1",
            )
            _assert_sqlstate(
                worker,
                "SELECT * FROM public.identity_user_groups LIMIT 1",
            )
    with role_database.connect("aegis_web") as web:
        _assert_sqlstate(
            web,
            "SELECT public.aegis_validate_operation_authorization(%s)",
            (operation.id,),
        )


def test_opaque_authorization_fails_closed_for_stale_and_insufficient_access(
    role_database: RoleDatabase,
) -> None:
    actor, root, operation = _create_authorized_operation()
    with role_database.connect("aegis_operations") as worker:
        assert _authorization_is_valid(worker, operation.id) is True
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE public.roots_rootgrant SET permissions = 1 "
                "WHERE root_id = %s AND user_id = %s",
                [root.id, actor.id],
            )
        assert _authorization_is_valid(worker, operation.id) is False

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE public.roots_rootgrant SET permissions = 3 "
                "WHERE root_id = %s AND user_id = %s",
                [root.id, actor.id],
            )
        assert _authorization_is_valid(worker, operation.id) is True

        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE public.roots_root "
                "SET authorization_epoch = authorization_epoch + 1 "
                "WHERE id = %s",
                [root.id],
            )
        assert _authorization_is_valid(worker, operation.id) is False


def test_opaque_authorization_fails_closed_for_malformed_records(
    role_database: RoleDatabase,
) -> None:
    _actor, _root, operation = _create_authorized_operation()
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE public.operations_operation "
            "SET intent = %s::jsonb WHERE id = %s",
            [
                '{"roots":[{"id":"not-a-uuid","permissions":3}]}',
                operation.id,
            ],
        )
    with role_database.connect("aegis_operations") as worker:
        assert _authorization_is_valid(worker, operation.id) is False


def test_authorization_execute_revocation_is_enforced_and_safely_restored(
    role_database: RoleDatabase,
) -> None:
    _actor, _root, operation = _create_authorized_operation()
    signature = MANAGED_FUNCTION_SIGNATURES[
        "aegis_validate_operation_authorization"
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            f"REVOKE EXECUTE ON FUNCTION {signature} FROM aegis_operations"
        )
    try:
        with role_database.connect("aegis_operations") as worker:
            _assert_sqlstate(
                worker,
                "SELECT public.aegis_validate_operation_authorization(%s)",
                (operation.id,),
            )
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                f"GRANT EXECUTE ON FUNCTION {signature} TO aegis_operations"
            )

    with role_database.connect("aegis_operations") as worker:
        assert _authorization_is_valid(worker, operation.id) is True
