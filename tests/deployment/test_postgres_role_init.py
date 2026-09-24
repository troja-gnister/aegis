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
from aegisctl.container_engine import ContainerEngineError, container_command
from psycopg import sql

from tests.support.container_runtime import (
    OwnedDirectResource,
    OwnedDirectScope,
    cleanup_owned_resources,
    copy_bind_inputs,
    prepare_owned_test_inventory,
    read_optional_cidfile,
    record_created_test_path,
    record_fresh_test_tree,
    record_test_tree_inventory,
    recover_owned_resource,
)
from tests.support.fake_container_engine import select_fake_engine

REPOSITORY = Path(__file__).resolve().parents[2]
CONTAINER_COMMAND = container_command()
SCRIPT = REPOSITORY / "deploy" / "postgres" / "init" / "001-roles.sh"
DATABASE_NAME = "aegis_role_init_test"
POSTGRES_IMAGE = (
    "docker.io/library/postgres:18.6-alpine@"
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
    return _run([*CONTAINER_COMMAND, *arguments], timeout=timeout, input_text=input_text)


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
    container_id: str

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
            _docker("exec", self.container_id, "rm", "-f", path, incoming),
            "clear secret path",
        )
        _require_success(
            _docker(
                "exec",
                "--interactive",
                self.container_id,
                "dd",
                f"of={incoming}",
                input_text=value.decode("utf-8"),
            ),
            "stage secret",
        )
        commands = (
            (("exec", self.container_id, "chown", "70:70", incoming), "own secret"),
            (("exec", self.container_id, "chmod", f"{mode:o}", incoming), "mode secret"),
            (("exec", self.container_id, "mv", incoming, path), "install secret"),
        )
        for arguments, action in commands:
            _require_success(_docker(*arguments), action)

    def stage_passwords(self, passwords: dict[str, str]) -> None:
        for role, password in passwords.items():
            self.stage_regular(role, password.encode("utf-8") + b"\n")

    def replace_with_symlink(self, role: str) -> None:
        path = SECRET_PATHS[role]
        _require_success(
            _docker("exec", self.container_id, "rm", "-f", path), "clear secret"
        )
        _require_success(
            _docker(
                "exec",
                self.container_id,
                "ln",
                "-s",
                SECRET_PATHS[MIGRATOR_ROLE],
                path,
            ),
            "create secret symlink",
        )

    def replace_with_fifo(self, role: str) -> None:
        path = SECRET_PATHS[role]
        _require_success(
            _docker("exec", self.container_id, "rm", "-f", path), "clear secret"
        )
        _require_success(
            _docker("exec", self.container_id, "mkfifo", path), "create secret fifo"
        )
        _require_success(
            _docker("exec", self.container_id, "chown", "70:70", path), "own fifo"
        )
        _require_success(
            _docker("exec", self.container_id, "chmod", "400", path), "mode fifo"
        )

    def run_init(self) -> subprocess.CompletedProcess[str]:
        return _docker(
            "exec",
            "--user",
            "70:70",
            self.container_id,
            "/aegis-init/001-roles.sh",
            timeout=30,
        )


def _protected_volume_created_at() -> str | None:
    result = _docker("volume", "inspect", PROTECTED_VOLUME, "--format", "{{.CreatedAt}}")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _create_role_init_resource(
    name: str, owner: str, cidfile: Path, arguments: Sequence[str],
    recorded: list[OwnedDirectResource], sensitive_values: Sequence[str],
) -> OwnedDirectResource:
    result: subprocess.CompletedProcess[str] | None = None
    error: Exception | None = None
    interruption: KeyboardInterrupt | SystemExit | None = None
    try:
        result = _docker(
            "create", "--cidfile", str(cidfile), "--name", name,
            "--label", f"aegis.test.owner={owner}",
            "--label", f"aegis.test.resource={name}", *arguments,
        )
    except (KeyboardInterrupt, SystemExit) as exc:
        interruption = exc
    except Exception as exc:
        error = exc
    candidate = read_optional_cidfile(cidfile)
    try:
        resource = recover_owned_resource(
            "container", candidate, "aegis.test.owner", owner,
            "aegis.test.resource", name,
            lambda *command: _docker(*command),
        )
    except BaseException as exc:
        if interruption is not None:
            raise interruption from exc
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        status = (
            f"exit status {result.returncode}; stderr characters={len(result.stderr or '')}"
            if result is not None else f"create exception={type(error).__name__}"
        )
        cid_status = "available" if candidate else "unavailable"
        # Recovery errors contain fixed validation messages and counts only. Never
        # include subprocess output, command arguments, CID contents or credentials.
        recovery = str(exc) if isinstance(exc, ContainerEngineError) else type(exc).__name__
        for value in sensitive_values:
            if value:
                recovery = recovery.replace(value, "[redacted]")
        failure = (
            "create failed" if result is None or result.returncode else "inconsistent recovery"
        )
        raise AssertionError(
            f"disposable PostgreSQL {failure} ({status}; CID {cid_status}); "
            f"recovery failed: {recovery[:200]}"
        ) from None
    recorded.append(resource)
    if interruption is not None:
        raise interruption
    if error is not None:
        raise error
    if result is None or result.returncode:
        raise AssertionError("disposable PostgreSQL create failed after identity recovery")
    _assert_values_absent(result, sensitive_values)
    started = _docker("start", resource.immutable_id)
    _require_success(started, "start disposable PostgreSQL")
    return resource


def _cleanup_role_init_resources(
    resources: tuple[OwnedDirectResource, ...], owner: str,
) -> None:
    cleanup_owned_resources(
        resources,
        lambda *command: _docker(*command),
        scopes=(OwnedDirectScope("container", "aegis.test.owner", owner),),
    )


def test_role_init_create_exception_recovers_canonical_id_before_propagating(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    recorded: list[OwnedDirectResource] = []
    commands: list[tuple[str, ...]] = []

    def completed(*arguments: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(arguments)
        if arguments[0] == "create":
            raise OSError("transport failed after create")
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(arguments, 0, "short-id\n", "")
        if arguments == ("container", "inspect", "short-id"):
            payload = [{"Id": "canonical-id", "Config": {"Labels": {
                "aegis.test.owner": "unique-owner",
                "aegis.test.resource": "unique-name",
            }}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    monkeypatch.setattr(f"{__name__}._docker", completed)
    with pytest.raises(OSError, match="transport failed"):
        _create_role_init_resource(
            "unique-name", "unique-owner", tmp_path / "missing.cid",
            (POSTGRES_IMAGE,), recorded, (),
        )
    assert recorded == [OwnedDirectResource(
        "container", "canonical-id", "aegis.test.owner", "unique-owner",
    )]
    assert not any(command[:2] == ("rm", "--force") for command in commands)


@pytest.mark.parametrize("failure", ("replacement", "query-error"))
def test_role_init_cleanup_refuses_uncertain_or_replaced_identity_before_delete(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    commands: list[tuple[str, ...]] = []

    def completed(*arguments: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        commands.append(arguments)
        if arguments[:2] == ("container", "ls"):
            return subprocess.CompletedProcess(
                arguments, 125 if failure == "query-error" else 0,
                "" if failure == "query-error" else "same-name\n", "",
            )
        if arguments == ("container", "inspect", "same-name"):
            payload = [{"Id": "replacement-id", "Config": {"Labels": {
                "aegis.test.owner": "unique-owner",
                "aegis.test.resource": "unique-name",
            }}}]
            return subprocess.CompletedProcess(arguments, 0, json.dumps(payload), "")
        raise AssertionError(arguments)

    monkeypatch.setattr(f"{__name__}._docker", completed)
    recorded = (OwnedDirectResource(
        "container", "original-id", "aegis.test.owner", "unique-owner",
    ),)
    with pytest.raises(ValueError):
        _cleanup_role_init_resources(recorded, "unique-owner")
    assert not any(command[:2] == ("rm", "--force") for command in commands)


@pytest.fixture(scope="module")
def role_init_database(tmp_path_factory: pytest.TempPathFactory) -> Iterator[RoleInitDatabase]:
    protected_volume_created_at = _protected_volume_created_at()
    token = secrets.token_hex(16)
    container_name = f"aegis-role-init-{token}"
    owner = f"role-init-{token}"
    recorded: list[OwnedDirectResource] = []

    scratch = tmp_path_factory.mktemp("postgres-role-init")
    tree = record_fresh_test_tree(scratch)
    bootstrap_password = f"bootstrap-{secrets.token_urlsafe(48)}"
    bootstrap_file = scratch / "postgres-password"
    bootstrap_file.write_text(bootstrap_password, encoding="utf-8")
    bootstrap_file.chmod(0o600)
    record_created_test_path(tree, bootstrap_file)
    script_copy = copy_bind_inputs(
        scratch, {"001-roles.sh": SCRIPT}, parent_tree=tree,
    )["001-roles.sh"]
    prepare_owned_test_inventory(record_test_tree_inventory(tree))
    try:
        resource = _create_role_init_resource(
            container_name,
            owner,
            scratch / "postgres.cid",
            (
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
            f"type=bind,src={script_copy},dst=/aegis-init/001-roles.sh,readonly",
            POSTGRES_IMAGE,
            ),
            recorded,
            (bootstrap_password,),
        )

        mounts_result = _docker(
            "inspect", resource.immutable_id, "--format", "{{json .Mounts}}"
        )
        _require_success(mounts_result, "inspect disposable PostgreSQL mounts")
        mounts = json.loads(mounts_result.stdout)
        assert all(mount.get("Name") != PROTECTED_VOLUME for mount in mounts)
        assert all(mount.get("Type") != "volume" for mount in mounts)

        port_result = _docker("port", resource.immutable_id, "5432/tcp")
        _require_success(port_result, "discover disposable PostgreSQL port")
        port = int(port_result.stdout.strip().rsplit(":", maxsplit=1)[1])

        deadline = time.monotonic() + 60
        while True:
            try:
                database = RoleInitDatabase(port, bootstrap_password, resource.immutable_id)
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
        _cleanup_role_init_resources(tuple(recorded), owner)
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


def test_role_init_removes_password_environment_before_starting_psql(
    tmp_path: Path,
) -> None:
    tree = record_fresh_test_tree(tmp_path)
    psql_probe = tmp_path / "psql"
    psql_probe.write_text(
        """#!/bin/sh
set -eu
for name in POSTGRES_PASSWORD POSTGRES_PASSWORD_FILE PGPASSWORD PGPASSFILE \
    PGOPTIONS PGSERVICE PGSERVICEFILE
do
    if env | grep -q "^${name}="; then
        printf '%s\\n' 'password environment reached psql' >&2
        exit 97
    fi
done
exit 0
""",
        encoding="utf-8",
    )
    psql_probe.chmod(0o555)
    record_created_test_path(tree, psql_probe)
    script_copy = copy_bind_inputs(
        tmp_path, {"001-roles.sh": SCRIPT}, parent_tree=tree,
    )["001-roles.sh"]
    prepare_owned_test_inventory(record_test_tree_inventory(tree))

    result = _docker(
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "FOWNER",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--tmpfs",
        "/run/secrets:rw,nosuid,nodev,size=64k,mode=0700",
        "--mount",
        f"type=bind,src={script_copy},dst=/aegis-init/001-roles.sh,readonly",
        "--mount",
        f"type=bind,src={psql_probe},dst=/test-bin/psql,readonly",
        "--env",
        "PATH=/test-bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "--env",
        "POSTGRES_USER=postgres",
        "--env",
        f"POSTGRES_DB={DATABASE_NAME}",
        "--env",
        "POSTGRES_PASSWORD=superuser-environment-canary",
        "--env",
        "POSTGRES_PASSWORD_FILE=/run/secrets/superuser-environment-canary",
        "--env",
        "PGPASSWORD=libpq-environment-canary",
        "--env",
        "PGPASSFILE=/run/secrets/libpq-environment-canary",
        "--env",
        "PGOPTIONS=environment-canary",
        "--env",
        "PGSERVICE=environment-canary",
        "--env",
        "PGSERVICEFILE=/run/secrets/environment-canary",
        "--entrypoint",
        "sh",
        POSTGRES_IMAGE,
        "-c",
        "set -eu; "
        "for name in db_migrator_password db_web_password "
        "db_operations_password db_indexer_password db_media_password; do "
        "printf 'role-secret\\n' > /run/secrets/$name; "
        "chown 70:70 /run/secrets/$name; chmod 0400 /run/secrets/$name; done; "
        "chown 70:70 /run/secrets; chmod 0700 /run/secrets; "
        "exec gosu postgres /aegis-init/001-roles.sh",
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "canary" not in result.stdout + result.stderr


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
        _docker("exec", database.container_id, "chown", "0:0", role_secret_path),
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
        stat_result = _docker(
            "exec", database.container_id, "stat", "-c", "%u:%g:%a", path
        )
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
    logs = _docker("logs", database.container_id)
    _require_success(logs, "read disposable PostgreSQL logs")
    _assert_values_absent(
        logs,
        [database.admin_password, *initial_passwords.values(), *rotated_passwords.values()],
    )


@pytest.mark.parametrize("engine", ("docker", "podman"))
@pytest.mark.parametrize("create_status,handles,expected", (
    (125, "", "create failed"),
    (0, "", "inconsistent recovery"),
    (0, "first\nsecond\n", "inconsistent recovery"),
))
def test_role_init_create_recovery_reports_safe_status_and_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    engine: str, create_status: int, handles: str, expected: str,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    recorded: list[OwnedDirectResource] = []
    commands: list[list[str]] = []
    secret = "private-password-canary"

    def completed(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert arguments[:len(prefix)] == prefix
        command = arguments[len(prefix):]
        commands.append(command)
        if command[0] == "create":
            return subprocess.CompletedProcess(arguments, create_status, secret, secret * 4000)
        assert command == [
            "container", "ls", "--all", "--no-trunc", "--quiet", "--filter",
            "label=aegis.test.owner=unique-owner", "--filter",
            "label=aegis.test.resource=unique-name",
        ]
        return subprocess.CompletedProcess(arguments, 0, handles, secret)

    monkeypatch.setattr(subprocess, "run", completed)
    with pytest.raises(AssertionError, match=expected) as caught:
        _create_role_init_resource(
            "unique-name", "unique-owner", tmp_path / "missing.cid",
            ("--env", f"PASSWORD={secret}", POSTGRES_IMAGE), recorded, (secret,),
        )
    diagnostic = str(caught.value)
    assert f"exit status {create_status}" in diagnostic
    assert f"handles={len(handles.split())}" in diagnostic
    assert "CID unavailable" in diagnostic
    assert "recovery" in diagnostic
    assert secret not in diagnostic
    assert POSTGRES_IMAGE not in diagnostic
    assert len(diagnostic) < 512
    assert recorded == []
    assert [command[0] for command in commands] == ["create", "container"]


@pytest.mark.parametrize("engine", ("docker", "podman"))
@pytest.mark.parametrize("interruption", (KeyboardInterrupt(), SystemExit(143)))
def test_role_init_interruption_during_recovery_propagates_without_adoption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, engine: str,
    interruption: BaseException,
) -> None:
    prefix = select_fake_engine(engine, tmp_path, monkeypatch)
    monkeypatch.setattr(f"{__name__}.CONTAINER_COMMAND", prefix)
    recorded: list[OwnedDirectResource] = []
    commands: list[list[str]] = []

    def completed(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        assert arguments[:len(prefix)] == prefix
        command = arguments[len(prefix):]
        commands.append(command)
        if command[0] == "create":
            return subprocess.CompletedProcess(arguments, 125, "", "create failed")
        assert command[:2] == ["container", "ls"]
        raise interruption

    monkeypatch.setattr(subprocess, "run", completed)
    with pytest.raises(type(interruption)) as caught:
        _create_role_init_resource(
            "unique-name", "unique-owner", tmp_path / "missing.cid",
            (POSTGRES_IMAGE,), recorded, (),
        )
    assert caught.value is interruption
    assert recorded == []
    assert [command[0] for command in commands] == ["create", "container"]
