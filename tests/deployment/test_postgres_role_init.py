from __future__ import annotations

import json
import re
import secrets
import subprocess
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "deploy" / "postgres" / "init" / "001-roles.sh"
CONTAINER_NAME = "aegis-task11-role-init-pg"
DATABASE_NAME = "aegis_role_init_test"
POSTGRES_IMAGE = (
    "postgres:18.6-alpine@"
    "sha256:d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2"
)
PROTECTED_VOLUME = "aegis_postgres-data"
MIGRATOR_ROLE = "aegis_migrator"
RUNTIME_ROLES = (
    "aegis_web",
    "aegis_operations",
    "aegis_indexer",
    "aegis_media",
)
MANAGED_ROLES = (MIGRATOR_ROLE, *RUNTIME_ROLES)
SECRET_PATHS = {
    "aegis_migrator": "/run/secrets/db_migrator_password",
    "aegis_web": "/run/secrets/db_web_password",
    "aegis_operations": "/run/secrets/db_operations_password",
    "aegis_indexer": "/run/secrets/db_indexer_password",
    "aegis_media": "/run/secrets/db_media_password",
}


def _run(
    arguments: Sequence[str],
    *,
    timeout: int = 60,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(f"command timed out after {timeout} seconds") from error


def _docker(
    *arguments: str,
    timeout: int = 60,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run(["docker", *arguments], timeout=timeout, input_text=input_text)


def _require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode != 0:
        pytest.fail(
            f"{action} failed with exit status {result.returncode}: "
            f"{(result.stderr or result.stdout)[-2000:]}",
            pytrace=False,
        )


def _assert_values_absent(
    result: subprocess.CompletedProcess[str], values: Sequence[str]
) -> None:
    output = result.stdout + result.stderr
    if any(value and value in output for value in values):
        pytest.fail("a database password appeared in command output", pytrace=False)


@dataclass(slots=True)
class RoleInitDatabase:
    port: int
    admin_password: str = field(repr=False)

    def admin_connect(self) -> psycopg.Connection[Any]:
        return self.connect("postgres", self.admin_password)

    def connect(self, role: str, password: str) -> psycopg.Connection[Any]:
        return psycopg.connect(
            dbname=DATABASE_NAME,
            host="127.0.0.1",
            port=self.port,
            user=role,
            password=password,
            connect_timeout=5,
            autocommit=True,
        )

    def role_names(self) -> tuple[str, ...]:
        with self.admin_connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT rolname
                  FROM pg_catalog.pg_roles
                 WHERE rolname = ANY(%s)
                 ORDER BY rolname
                """,
                [list(MANAGED_ROLES)],
            )
            return tuple(row[0] for row in cursor.fetchall())

    def stage_regular(self, role: str, value: bytes, *, mode: int = 0o400) -> None:
        path = SECRET_PATHS[role]
        incoming = f"/run/secrets/.{Path(path).name}.incoming"
        _require_success(
            _docker("exec", CONTAINER_NAME, "rm", "-f", path, incoming),
            "clear secret path",
        )
        _require_success(
            _docker(
                "exec",
                "--interactive",
                CONTAINER_NAME,
                "dd",
                f"of={incoming}",
                input_text=value.decode("utf-8"),
            ),
            "stage secret",
        )
        commands = (
            (("exec", CONTAINER_NAME, "chown", "70:70", incoming), "own secret"),
            (("exec", CONTAINER_NAME, "chmod", f"{mode:o}", incoming), "mode secret"),
            (("exec", CONTAINER_NAME, "mv", incoming, path), "install secret"),
        )
        for arguments, action in commands:
            _require_success(_docker(*arguments), action)

    def stage_passwords(self, passwords: dict[str, str]) -> None:
        for role, password in passwords.items():
            self.stage_regular(role, password.encode("utf-8") + b"\n")

    def replace_with_symlink(self, role: str) -> None:
        path = SECRET_PATHS[role]
        _require_success(_docker("exec", CONTAINER_NAME, "rm", "-f", path), "clear secret")
        _require_success(
            _docker(
                "exec",
                CONTAINER_NAME,
                "ln",
                "-s",
                SECRET_PATHS[MIGRATOR_ROLE],
                path,
            ),
            "create secret symlink",
        )

    def replace_with_fifo(self, role: str) -> None:
        path = SECRET_PATHS[role]
        _require_success(_docker("exec", CONTAINER_NAME, "rm", "-f", path), "clear secret")
        _require_success(_docker("exec", CONTAINER_NAME, "mkfifo", path), "create secret fifo")
        _require_success(_docker("exec", CONTAINER_NAME, "chown", "70:70", path), "own fifo")
        _require_success(_docker("exec", CONTAINER_NAME, "chmod", "400", path), "mode fifo")

    def run_init(self) -> subprocess.CompletedProcess[str]:
        return _docker(
            "exec",
            "--user",
            "70:70",
            CONTAINER_NAME,
            "/aegis-init/001-roles.sh",
            timeout=30,
        )


def _protected_volume_created_at() -> str | None:
    result = _docker("volume", "inspect", PROTECTED_VOLUME, "--format", "{{.CreatedAt}}")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


@pytest.fixture(scope="module")
def role_init_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RoleInitDatabase]:
    protected_volume_created_at = _protected_volume_created_at()
    if _docker("inspect", CONTAINER_NAME).returncode == 0:
        pytest.fail(f"refusing to modify pre-existing container {CONTAINER_NAME}", pytrace=False)

    scratch = tmp_path_factory.mktemp("postgres-role-init")
    bootstrap_password = f"bootstrap-{secrets.token_urlsafe(48)}"
    bootstrap_file = scratch / "postgres-password"
    bootstrap_file.write_text(bootstrap_password, encoding="utf-8")
    bootstrap_file.chmod(0o600)
    created = False
    try:
        result = _docker(
            "run",
            "--detach",
            "--name",
            CONTAINER_NAME,
            "--tmpfs",
            "/var/lib/postgresql:rw,nosuid,nodev,size=256m",
            "--tmpfs",
            "/run/secrets:rw,noexec,nosuid,nodev,uid=70,gid=70,mode=0700",
            "--publish",
            "127.0.0.1::5432",
            "--env",
            f"POSTGRES_DB={DATABASE_NAME}",
            "--env",
            "POSTGRES_USER=postgres",
            "--env",
            "POSTGRES_PASSWORD_FILE=/bootstrap/postgres-password",
            "--env",
            "POSTGRES_HOST_AUTH_METHOD=scram-sha-256",
            "--mount",
            f"type=bind,src={bootstrap_file},dst=/bootstrap/postgres-password,readonly",
            "--mount",
            f"type=bind,src={SCRIPT},dst=/aegis-init/001-roles.sh,readonly",
            POSTGRES_IMAGE,
        )
        created = _docker("inspect", CONTAINER_NAME).returncode == 0
        _assert_values_absent(result, [bootstrap_password])
        _require_success(result, "start disposable PostgreSQL")

        mounts_result = _docker("inspect", CONTAINER_NAME, "--format", "{{json .Mounts}}")
        _require_success(mounts_result, "inspect disposable PostgreSQL mounts")
        mounts = json.loads(mounts_result.stdout)
        assert all(mount.get("Name") != PROTECTED_VOLUME for mount in mounts)
        assert all(mount.get("Type") != "volume" for mount in mounts)

        port_result = _docker("port", CONTAINER_NAME, "5432/tcp")
        _require_success(port_result, "discover disposable PostgreSQL port")
        port = int(port_result.stdout.strip().rsplit(":", maxsplit=1)[1])

        deadline = time.monotonic() + 60
        while True:
            try:
                database = RoleInitDatabase(port, bootstrap_password)
                with database.admin_connect() as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT current_setting('server_version_num')::integer")
                    server_version = cursor.fetchone()
                    assert server_version is not None
                    assert server_version[0] // 10_000 == 18
                break
            except psycopg.OperationalError:
                if time.monotonic() >= deadline:
                    pytest.fail("disposable PostgreSQL did not become ready", pytrace=False)
                time.sleep(0.25)
        yield database
    finally:
        if created:
            _require_success(
                _docker("rm", "--force", CONTAINER_NAME),
                "remove disposable PostgreSQL",
            )
        assert _docker("inspect", CONTAINER_NAME).returncode != 0
        assert _protected_volume_created_at() == protected_volume_created_at


def test_role_init_keeps_passwords_out_of_shell_and_psql_state() -> None:
    source = SCRIPT.read_text(encoding="utf-8")

    assert subprocess.run(
        ["sh", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 0
    assert "--no-psqlrc" in source
    assert (
        "unset PGPASSWORD PGPASSFILE PGOPTIONS PGSERVICE PGSERVICEFILE" in source
    )
    assert "set -x" not in source
    assert "$(" not in source
    assert "`" not in source
    assert re.search(r"\bcat\b", source) is None
    assert re.search(r"\\set\s+\w*password", source, re.IGNORECASE) is None
    assert re.search(r"PGPASSWORD\s*=", source) is None
    assert re.search(r"--set(?:=|\s+)\w*password", source, re.IGNORECASE) is None
    assert set(re.findall(r"/run/secrets/[a-z0-9_]+", source)) == set(
        SECRET_PATHS.values()
    )
    for path in SECRET_PATHS.values():
        assert f"pg_catalog.pg_read_file('{path}'" in source
    assert "%I" in source
    assert "%L" in source
    first_server_read = source.index("pg_catalog.pg_read_file")
    for setting in (
        "log_statement = 'none'",
        "log_duration = off",
        "log_min_duration_statement = -1",
        "log_min_duration_sample = -1",
        "log_parameter_max_length = 0",
        "log_parameter_max_length_on_error = 0",
        "log_min_error_statement = 'panic'",
        "log_error_verbosity = 'terse'",
        "password_encryption = 'scram-sha-256'",
    ):
        assert f"SET LOCAL {setting};" in source
        assert source.index(f"SET LOCAL {setting};") < first_server_read


@pytest.mark.integration
def test_role_init_runtime_contract(role_init_database: RoleInitDatabase) -> None:
    database = role_init_database
    initial_passwords = {
        role: f"{role}-'\\$`; Ω {secrets.token_urlsafe(20)}" for role in MANAGED_ROLES
    }
    database.stage_passwords(initial_passwords)
    all_sensitive_values = [database.admin_password, *initial_passwords.values()]

    invalid_cases: tuple[tuple[str, bytes, int], ...] = (
        ("empty", b"", 0o400),
        ("oversized", b"x" * 4097, 0o400),
        ("group-readable", b"group-readable-canary", 0o440),
        ("world-readable", b"world-readable-canary", 0o404),
        ("newline-only", b"\n", 0o400),
    )
    for _label, invalid_value, mode in invalid_cases:
        database.stage_regular("aegis_web", invalid_value, mode=mode)
        result = database.run_init()
        invalid_text = invalid_value.decode("ascii") if invalid_value.strip() else ""
        _assert_values_absent(result, [*all_sensitive_values, invalid_text])
        assert result.returncode != 0
        assert database.role_names() == ()
        database.stage_regular(
            "aegis_web", initial_passwords["aegis_web"].encode("utf-8") + b"\n"
        )

    database.replace_with_symlink("aegis_web")
    result = database.run_init()
    _assert_values_absent(result, all_sensitive_values)
    assert result.returncode != 0
    assert database.role_names() == ()
    database.stage_regular(
        "aegis_web", initial_passwords["aegis_web"].encode("utf-8") + b"\n"
    )

    database.replace_with_fifo("aegis_web")
    result = database.run_init()
    _assert_values_absent(result, all_sensitive_values)
    assert result.returncode != 0
    assert database.role_names() == ()
    database.stage_regular(
        "aegis_web", initial_passwords["aegis_web"].encode("utf-8") + b"\n"
    )

    role_secret_path = SECRET_PATHS["aegis_web"]
    _require_success(
        _docker("exec", CONTAINER_NAME, "chown", "0:0", role_secret_path),
        "set unsafe secret owner",
    )
    result = database.run_init()
    _assert_values_absent(result, all_sensitive_values)
    assert result.returncode != 0
    assert database.role_names() == ()
    database.stage_regular(
        "aegis_web", initial_passwords["aegis_web"].encode("utf-8") + b"\n"
    )

    with database.admin_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT pg_catalog.pg_get_userbyid(database.datdba),
                   pg_catalog.pg_get_userbyid(namespace.nspowner)
              FROM pg_catalog.pg_database AS database
              JOIN pg_catalog.pg_namespace AS namespace ON namespace.nspname = 'public'
             WHERE database.datname = current_database()
            """
        )
        original_owners = cursor.fetchone()
        broad_password = f"broad-{secrets.token_urlsafe(32)}"
        cursor.execute(
            sql.SQL(
                "CREATE ROLE aegis_web LOGIN NOINHERIT CREATEDB PASSWORD {}"
            ).format(sql.Literal(broad_password))
        )
    result = database.run_init()
    _assert_values_absent(result, [*all_sensitive_values, broad_password])
    assert result.returncode != 0
    assert database.role_names() == ("aegis_web",)
    with database.connect("aegis_web", broad_password) as broad_connection:
        assert not broad_connection.closed
    with pytest.raises(psycopg.OperationalError):
        database.connect("aegis_web", initial_passwords["aegis_web"])
    with database.admin_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolcreatedb FROM pg_catalog.pg_roles WHERE rolname = 'aegis_web'"
        )
        assert cursor.fetchone() == (True,)
        cursor.execute(
            """
            SELECT pg_catalog.pg_get_userbyid(database.datdba),
                   pg_catalog.pg_get_userbyid(namespace.nspowner)
              FROM pg_catalog.pg_database AS database
              JOIN pg_catalog.pg_namespace AS namespace ON namespace.nspname = 'public'
             WHERE database.datname = current_database()
            """
        )
        assert cursor.fetchone() == original_owners
        cursor.execute("DROP ROLE aegis_web")

    database.stage_passwords(initial_passwords)
    for path in SECRET_PATHS.values():
        stat_result = _docker("exec", CONTAINER_NAME, "stat", "-c", "%u:%g:%a", path)
        _require_success(stat_result, "inspect staged role secret")
        assert stat_result.stdout.strip() == "70:70:400"
    with database.admin_connect() as connection, connection.cursor() as cursor:
        for role, path in SECRET_PATHS.items():
            cursor.execute("SELECT pg_catalog.octet_length(pg_catalog.pg_read_file(%s))", [path])
            assert cursor.fetchone() == (len(initial_passwords[role].encode("utf-8")) + 1,)
        cursor.execute("ALTER SYSTEM SET log_statement = 'all'")
        cursor.execute("ALTER SYSTEM SET log_min_duration_statement = 0")
        cursor.execute("ALTER SYSTEM SET log_parameter_max_length = -1")
        cursor.execute("ALTER SYSTEM SET log_parameter_max_length_on_error = -1")
        cursor.execute("SELECT pg_catalog.pg_reload_conf()")
        assert cursor.fetchone() == (True,)

    with database.admin_connect() as connection, connection.cursor() as cursor:
        cursor.execute("SHOW log_statement")
        assert cursor.fetchone() == ("all",)
        cursor.execute("SHOW log_min_duration_statement")
        assert cursor.fetchone() == ("0",)

    result = database.run_init()
    _assert_values_absent(result, all_sensitive_values)
    _require_success(result, "initialize database roles")

    for role, password in initial_passwords.items():
        with database.connect(role, password) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT session_user, current_user")
            assert cursor.fetchone() == (role, role)

    with database.admin_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT role.rolname, role.rolcanlogin, role.rolinherit, role.rolsuper,
                   role.rolcreatedb, role.rolcreaterole, role.rolreplication,
                   role.rolbypassrls,
                   authentication.rolpassword LIKE 'SCRAM-SHA-256$%%'
              FROM pg_catalog.pg_roles AS role
              JOIN pg_catalog.pg_authid AS authentication USING (rolname)
             WHERE rolname = ANY(%s)
             ORDER BY rolname
            """,
            [list(MANAGED_ROLES)],
        )
        assert cursor.fetchall() == [
            (role, True, False, False, False, False, False, False, True)
            for role in sorted(MANAGED_ROLES)
        ]
        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_auth_members AS membership
             WHERE membership.roleid = ANY(
                       SELECT oid FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)
                   )
                OR membership.member = ANY(
                       SELECT oid FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)
                   )
            """,
            [list(MANAGED_ROLES), list(MANAGED_ROLES)],
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            """
            SELECT pg_catalog.pg_get_userbyid(database.datdba),
                   pg_catalog.pg_get_userbyid(namespace.nspowner)
              FROM pg_catalog.pg_database AS database
              JOIN pg_catalog.pg_namespace AS namespace ON namespace.nspname = 'public'
             WHERE database.datname = current_database()
            """
        )
        assert cursor.fetchone() == (MIGRATOR_ROLE, MIGRATOR_ROLE)
        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_database AS database
              CROSS JOIN LATERAL pg_catalog.aclexplode(
                  COALESCE(database.datacl, pg_catalog.acldefault('d', database.datdba))
              ) AS acl
             WHERE database.datname = current_database() AND acl.grantee = 0
            """
        )
        assert cursor.fetchone() == (0,)
        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_namespace AS namespace
              CROSS JOIN LATERAL pg_catalog.aclexplode(
                  COALESCE(namespace.nspacl, pg_catalog.acldefault('n', namespace.nspowner))
              ) AS acl
             WHERE namespace.nspname = 'public' AND acl.grantee = 0
            """
        )
        assert cursor.fetchone() == (0,)
        for role in RUNTIME_ROLES:
            cursor.execute(
                """
                SELECT has_database_privilege(%s, current_database(), 'CONNECT'),
                       has_database_privilege(%s, current_database(), 'CREATE'),
                       has_database_privilege(%s, current_database(), 'TEMP'),
                       has_schema_privilege(%s, 'public', 'USAGE'),
                       has_schema_privilege(%s, 'public', 'CREATE')
                """,
                [role, role, role, role, role],
            )
            assert cursor.fetchone() == (True, False, False, True, False)

    with (
        database.connect(MIGRATOR_ROLE, initial_passwords[MIGRATOR_ROLE]) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("CREATE TABLE public.aegis_init_acl_probe (id bigint)")
        cursor.execute("CREATE SEQUENCE public.aegis_init_acl_probe_sequence")
        cursor.execute(
            """
            CREATE FUNCTION public.aegis_init_acl_probe_function()
            RETURNS integer LANGUAGE sql AS 'SELECT 1'
            """
        )
    with database.admin_connect() as connection, connection.cursor() as cursor:
        for role in RUNTIME_ROLES:
            cursor.execute(
                """
                SELECT has_table_privilege(%s, 'public.aegis_init_acl_probe', 'SELECT'),
                       has_sequence_privilege(
                           %s, 'public.aegis_init_acl_probe_sequence', 'USAGE'
                       ),
                       has_function_privilege(
                           %s, 'public.aegis_init_acl_probe_function()', 'EXECUTE'
                       )
                """,
                [role, role, role],
            )
            assert cursor.fetchone() == (False, False, False)
        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_default_acl AS defaults
              CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
             WHERE defaults.defaclrole = (
                       SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                   )
               AND defaults.defaclnamespace = (
                       SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'public'
                   )
               AND (
                    acl.grantee = 0
                    OR acl.grantee = ANY(
                        SELECT oid FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)
                    )
               )
            """,
            [MIGRATOR_ROLE, list(RUNTIME_ROLES)],
        )
        assert cursor.fetchone() == (0,)

        cursor.execute(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator
                GRANT SELECT ON TABLES TO aegis_web
            """
        )
        cursor.execute(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator
                GRANT USAGE ON SEQUENCES TO aegis_operations
            """
        )
        cursor.execute(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator
                GRANT EXECUTE ON FUNCTIONS TO aegis_indexer
            """
        )
        cursor.execute(
            """
            ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator IN SCHEMA public
                GRANT INSERT ON TABLES TO aegis_media
            """
        )

    rotated_passwords = {
        role: f"rotated-{role}-'\\$`; 雪 {secrets.token_urlsafe(20)}" for role in MANAGED_ROLES
    }
    database.stage_passwords(rotated_passwords)
    rotation = database.run_init()
    _assert_values_absent(
        rotation,
        [database.admin_password, *initial_passwords.values(), *rotated_passwords.values()],
    )
    _require_success(rotation, "rotate database role passwords")
    for role in MANAGED_ROLES:
        with pytest.raises(psycopg.OperationalError):
            database.connect(role, initial_passwords[role])
        with database.connect(role, rotated_passwords[role]) as connection:
            assert not connection.closed

    with (
        database.connect(MIGRATOR_ROLE, rotated_passwords[MIGRATOR_ROLE]) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("CREATE TABLE public.aegis_init_acl_rerun_probe (id bigint)")
        cursor.execute("CREATE SEQUENCE public.aegis_init_acl_rerun_probe_sequence")
        cursor.execute(
            """
            CREATE FUNCTION public.aegis_init_acl_rerun_probe_function()
            RETURNS integer LANGUAGE sql AS 'SELECT 1'
            """
        )
    with database.admin_connect() as connection, connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT has_table_privilege(
                       'aegis_web', 'public.aegis_init_acl_rerun_probe', 'SELECT'
                   ),
                   has_sequence_privilege(
                       'aegis_operations',
                       'public.aegis_init_acl_rerun_probe_sequence',
                       'USAGE'
                   ),
                   has_function_privilege(
                       'aegis_indexer',
                       'public.aegis_init_acl_rerun_probe_function()',
                       'EXECUTE'
                   ),
                   has_table_privilege(
                       'aegis_media', 'public.aegis_init_acl_rerun_probe', 'INSERT'
                   )
            """
        )
        assert cursor.fetchone() == (False, False, False, False)
        cursor.execute(
            """
            SELECT count(*)
              FROM pg_catalog.pg_default_acl AS defaults
              CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
             WHERE defaults.defaclrole = (
                       SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                   )
               AND defaults.defaclnamespace IN (
                       0,
                       (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = 'public')
                   )
               AND (
                    acl.grantee = 0
                    OR acl.grantee = ANY(
                        SELECT oid FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)
                    )
               )
            """,
            [MIGRATOR_ROLE, list(RUNTIME_ROLES)],
        )
        assert cursor.fetchone() == (0,)

    rerun = database.run_init()
    _assert_values_absent(rerun, [database.admin_password, *rotated_passwords.values()])
    _require_success(rerun, "rerun database role initialization")
    logs = _docker("logs", CONTAINER_NAME)
    _require_success(logs, "read disposable PostgreSQL logs")
    _assert_values_absent(
        logs,
        [database.admin_password, *initial_passwords.values(), *rotated_passwords.values()],
    )
