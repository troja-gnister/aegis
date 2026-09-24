from __future__ import annotations

import json
import secrets
import subprocess
import sys
import uuid
from pathlib import Path
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
from aegis_apps.operations.leases import (
    claim_next_job,
    fail_job,
    finish_job,
    relinquish_job,
    renew_lease,
    retry_job,
    start_job_execution,
)
from aegis_apps.operations.models import Job, Operation
from aegis_apps.operations.services import create_operation, enqueue_job
from aegis_apps.roots.models import Root, RootGrant
from aegisctl.container_engine import container_command
from django.db import connection, transaction
from django.db.models.functions import Now
from django.test import override_settings
from django.utils import timezone
from psycopg import sql
from psycopg.types.json import Jsonb

from tests.support.database_roles import (
    ALL_TEST_ROLES,
    MIGRATOR_ROLE,
    RoleDatabase,
    _django_login,
    _quote,
    managed_role_database,
)

CONTAINER_COMMAND = container_command()
pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

HEARTBEAT_FUNCTIONS = {
    "operations": "aegis_publish_operations_heartbeat",
    "indexer": "aegis_publish_indexer_heartbeat",
    "media": "aegis_publish_media_heartbeat",
}
HEARTBEAT_ARGUMENTS = "%s, %s, %s, %s, %s, %s, %s, %s, %s"


def test_role_fixture_restores_preexisting_managed_function_and_dependency() -> None:
    signature = "public.aegis_validate_operation_authorization(uuid)"
    dependent = "public.aegis_test_function_dependency()"
    owner = f"aegis_test_function_owner_{uuid.uuid4().hex}"
    with connection.cursor() as cursor:
        cursor.execute("SELECT to_regprocedure(%s)", [signature])
        prior = cursor.fetchone()
        assert prior == (None,), "preservation test requires its own function"
        cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(_quote(owner)))
        cursor.execute("""
            CREATE FUNCTION public.aegis_validate_operation_authorization(p_operation_id uuid)
            RETURNS boolean LANGUAGE sql AS 'SELECT false'
        """)
        cursor.execute(
            sql.SQL("ALTER FUNCTION {} OWNER TO {}").format(sql.SQL(signature), _quote(owner))
        )
        cursor.execute("""
            CREATE FUNCTION public.aegis_test_function_dependency()
            RETURNS boolean LANGUAGE sql
            BEGIN ATOMIC
                SELECT public.aegis_validate_operation_authorization(NULL::uuid);
            END
        """)
        cursor.execute("SELECT pg_get_functiondef(%s::regprocedure)", [signature])
        definition = cursor.fetchone()[0]
    try:
        with (
            managed_role_database() as database,
            database.as_django_role("aegis_web"),
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT session_user, current_user")
            assert cursor.fetchone() == ("aegis_web", "aegis_web")
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT pg_get_functiondef(oid), pg_get_userbyid(proowner)
                FROM pg_proc WHERE oid = %s::regprocedure
            """,
                [signature],
            )
            assert cursor.fetchone() == (definition, owner)
            cursor.execute("SELECT public.aegis_test_function_dependency()")
            assert cursor.fetchone() == (False,)
            cursor.execute(
                "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", [list(ALL_TEST_ROLES)]
            )
            assert cursor.fetchall() == []
    finally:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP FUNCTION {}").format(sql.SQL(dependent)))
            cursor.execute(sql.SQL("DROP FUNCTION {}").format(sql.SQL(signature)))
            cursor.execute(sql.SQL("DROP ROLE {}").format(_quote(owner)))



def _scan_scalar(cursor: psycopg.Cursor[Any]) -> Any:
    row = cursor.fetchone()
    assert row is not None
    return row[0]


def _checkpoint_fixture(database: RoleDatabase) -> tuple[Root, dict[str, Any], dict[str, Any]]:
    root, worker, digest = _create_scan_fixture()
    with database.connect("aegis_indexer") as caller:
        run_id = _scan_scalar(caller.execute(
            "SELECT public.aegis_schedule_root_scan(%s,%s,%s)", [root.pk, worker, digest],
        ))
        lease = _scan_scalar(caller.execute(
            "SELECT public.aegis_claim_scan_directory(%s,%s)", [run_id, worker],
        ))
    batch = {"sequence": 1, "observations": [{
        "raw": "c2FmZQ==", "display": "safe", "name_key": "c2FmZQ==", "type_hint": None,
        "kind": "file", "state": "present", "size": 1, "mtime_ns": 2, "ctime_ns": 3,
        "device": 4, "inode": 5,
    }]}
    return root, lease, batch


@pytest.mark.parametrize("role", ["aegis_web", "aegis_operations", "aegis_media"])
@pytest.mark.parametrize("statement,argument", [
    ("SELECT public.aegis_record_scan_batch(%s,%s)", Jsonb({})),
    ("SELECT public.aegis_seal_scan_directory(%s,%s)", Jsonb([1, 1, 1, 1])),
    ("SELECT public.aegis_finalize_scan_directory(%s,%s)", 1),
    ("SELECT public.aegis_fail_scan_directory(%s,%s)", "permission_denied"),
])
def test_checkpoint_functions_require_actual_indexer_login(
    role_database: RoleDatabase, role: str, statement: str, argument: Any,
) -> None:
    with role_database.connect(role) as caller, pytest.raises(psycopg.errors.InsufficientPrivilege):
        caller.execute(statement, [Jsonb({}), argument])


@pytest.mark.parametrize("field,value", [
    ("raw", "Li4="), ("raw", "YQAv"), ("raw", "L2E="), ("raw", "c2FmZQ=\n="),
    ("raw", "c2FmZR=="), ("raw", "a"), ("raw", ""), ("raw", 1),
    ("name_key", "a"), ("name_key", ""), ("name_key", "eA==\n"),
    ("display", ""), ("display", "x" * 1531), ("display", 1),
    ("type_hint", "JPG"), ("type_hint", []), ("kind", "unknown"),
    ("kind", "special"), ("state", "missing"), ("state", "unsupported"),
    ("state", "inaccessible"), ("size", None), ("size", -1), ("size", True),
    ("size", 1.5), ("size", "1"), ("size", 2**63), ("inode", 0),
    ("device", 2**64), ("mtime_ns", -(2**63)-1),
    ("root_id", "00000000-0000-0000-0000-000000000000"),
    ("logical_name", "forged organization"),
])
def test_direct_sql_validates_entire_observation_before_any_write(
    role_database: RoleDatabase, field: str, value: Any,
) -> None:
    from aegis_apps.catalog.models import CatalogEntry
    from aegis_apps.indexing.models import DirectoryWork

    root, lease, batch = _checkpoint_fixture(role_database)
    valid = dict(batch["observations"][0])
    batch["observations"].append(dict(valid, raw="YmFk", display="bad", name_key="YmFk"))
    batch["observations"][1][field] = value
    with (role_database.connect("aegis_indexer") as caller,
          pytest.raises(psycopg.errors.InvalidParameterValue)):
        caller.execute("SELECT public.aegis_record_scan_batch(%s,%s)", [Jsonb(lease), Jsonb(batch)])
    assert CatalogEntry.objects.filter(root=root).count() == 1
    assert DirectoryWork.objects.get(pk=lease["work_id"]).last_batch_sequence == 0


def test_direct_checkpoint_replay_fail_and_stale_root_keep_metadata(
    role_database: RoleDatabase,
) -> None:
    from aegis_apps.catalog.models import CatalogEntry

    root, lease, batch = _checkpoint_fixture(role_database)
    with role_database.connect("aegis_indexer") as caller:
        first = _scan_scalar(caller.execute(
            "SELECT public.aegis_record_scan_batch(%s,%s)", [Jsonb(lease), Jsonb(batch)],
        ))
        assert first == {"observed": 1, "inserted": 1, "changed": 0}
        assert _scan_scalar(caller.execute(
            "SELECT public.aegis_record_scan_batch(%s,%s)", [Jsonb(lease), Jsonb(batch)],
        )) == {"observed": 0, "inserted": 0, "changed": 0}
        assert _scan_scalar(caller.execute(
            "SELECT public.aegis_fail_scan_directory(%s,%s)", [Jsonb(lease), "reader_timeout"],
        )) is True
        assert _scan_scalar(caller.execute(
            "SELECT public.aegis_finalize_scan_directory(%s,%s)", [Jsonb(lease), 500],
        )) is None
    assert CatalogEntry.objects.get(root=root, raw_name=b"safe").source_state == "present"


def test_commit_trigger_has_no_runtime_execution_grant(role_database: RoleDatabase) -> None:
    for role in ("aegis_web", "aegis_operations", "aegis_indexer", "aegis_media"):
        with role_database.connect(role) as caller:
            assert caller.execute(
                "SELECT pg_catalog.has_function_privilege(current_user,"
                "'public.aegis_guard_directory_commit()', 'EXECUTE')",
            ).fetchone() == (False,)


@pytest.mark.parametrize("invalid", [
    None, [], {}, {"sequence": 1, "observations": []},
    {"sequence": True, "observations": []},
    {"sequence": 2**31, "observations": []},
    {"sequence": 1, "observations": {}, "extra": 1},
    {"sequence": 1, "observations": [None]},
])
def test_direct_checkpoint_rejects_malformed_batch_envelopes(
    role_database: RoleDatabase, invalid: Any,
) -> None:
    _, lease, _ = _checkpoint_fixture(role_database)
    with (role_database.connect("aegis_indexer") as caller,
          pytest.raises(psycopg.errors.InvalidParameterValue)):
        caller.execute("SELECT public.aegis_record_scan_batch(%s,%s)",
                       [Jsonb(lease), Jsonb(invalid)])


@pytest.mark.parametrize("identity", [None, {}, [], [1, 1, 1], [True, 1, 1, 1],
                                      [1, 0, 1, 1], [2**64, 1, 1, 1], [1, 1, 2**63, 1]])
def test_direct_checkpoint_rejects_malformed_eof_identity(
    role_database: RoleDatabase, identity: Any,
) -> None:
    _, lease, _ = _checkpoint_fixture(role_database)
    with (role_database.connect("aegis_indexer") as caller,
          pytest.raises(psycopg.errors.InvalidParameterValue)):
        caller.execute("SELECT public.aegis_seal_scan_directory(%s,%s)",
                       [Jsonb(lease), Jsonb(identity)])


@pytest.mark.parametrize("limit", [None, 0, -1, 501])
def test_direct_checkpoint_rejects_unbounded_finalization(
    role_database: RoleDatabase, limit: int | None,
) -> None:
    _, lease, _ = _checkpoint_fixture(role_database)
    with (role_database.connect("aegis_indexer") as caller,
          pytest.raises(psycopg.errors.InvalidParameterValue)):
        caller.execute("SELECT public.aegis_finalize_scan_directory(%s,%s)", [Jsonb(lease), limit])


def _create_scan_fixture() -> tuple[Root, str, str]:
    from aegis_apps.catalog.models import CatalogEntry
    from aegis_apps.indexing.models import IndexDeployment
    from aegis_apps.operations.models import WorkerHeartbeat

    root = Root.objects.create(
        slot_id=f"scan-{uuid.uuid4().hex}", display_name="Synthetic scan root",
        mode=Root.Mode.READ_ONLY, active=True,
    )
    CatalogEntry.objects.create(
        root=root, raw_name=b"", display_name="", name_key=b"", kind="directory",
    )
    deployment, created = IndexDeployment.objects.get_or_create(
        pk=1, defaults={
            "manifest_identity": "a" * 64, "slot_ids": [root.slot_id],
            "interval_seconds": 3600, "idle_timeout_seconds": 120,
            "batch_records": 500, "readers": 2,
        },
    )
    if not created:
        deployment.slot_ids = sorted(set([*deployment.slot_ids, root.slot_id]))
        deployment.save(update_fields=("slot_ids",))
    worker_id = str(uuid.uuid4())
    WorkerHeartbeat.objects.create(
        role="indexer", worker_id=worker_id, release_id="task4-test",
        schema_identity="schema-test", manifest_identity=deployment.manifest_identity,
        last_seen_at=Now(), status="idle", metrics={},
    )
    return root, worker_id, deployment.manifest_identity


def test_only_indexer_can_schedule_and_renew(role_database: RoleDatabase) -> None:
    root, worker_id, digest = _create_scan_fixture()
    with role_database.connect("aegis_web") as web:
        _assert_sqlstate(web, "SELECT public.aegis_schedule_root_scan(%s,%s,%s)",
                         (root.pk, worker_id, digest))
    with role_database.connect("aegis_indexer") as indexer:
        run_id = _scan_scalar(indexer.execute(
            "SELECT public.aegis_schedule_root_scan(%s,%s,%s)",
            (root.pk, worker_id, digest),
        ))
        assert isinstance(run_id, uuid.UUID)
        lease = _scan_scalar(indexer.execute(
            "SELECT public.aegis_claim_scan_directory(%s,%s)", (run_id, worker_id),
        ))
        assert _scan_scalar(indexer.execute(
            "SELECT public.aegis_renew_scan_directory(%s)", (Jsonb(lease),),
        )) is True


@pytest.mark.parametrize("role", ["aegis_web", "aegis_operations", "aegis_media", "aegis_migrator"])
def test_scan_maintenance_checks_actual_caller(role_database: RoleDatabase, role: str) -> None:
    root, worker, digest = _create_scan_fixture()
    with role_database.connect(role) as caller:
        _assert_sqlstate(caller, "SELECT public.aegis_schedule_root_scan(%s,%s,%s)",
                         (root.pk, worker, digest))
        _assert_sqlstate(caller, "SELECT public.aegis_claim_scan_directory(%s,%s)",
                         (uuid.uuid4(), worker))
        _assert_sqlstate(caller, "SELECT public.aegis_renew_scan_directory(%s)", (Jsonb({}),))


@pytest.mark.parametrize(
    "role", ["aegis_indexer", "aegis_operations", "aegis_media", "aegis_migrator"]
)
def test_only_web_can_request_scan(role_database: RoleDatabase, role: str) -> None:
    with role_database.connect(role) as caller:
        _assert_sqlstate(caller, "SELECT public.aegis_request_root_scan(%s,%s,%s,%s)",
                         (uuid.uuid4(), uuid.uuid4(), 0, "request_123"))


def test_built_runtime_wheel_installs_and_executes_scan_sql(role_database: RoleDatabase) -> None:
    root, worker, digest = _create_scan_fixture()
    manual_root, _, _ = _create_scan_fixture()
    actor = User.objects.create_user(username=f"artifact-manual-{uuid.uuid4().hex}")
    RootGrant.objects.create(root=manual_root, user=actor, permissions=128)
    image_tag = f"aegis-task4-artifact-{uuid.uuid4().hex}"
    repository = Path(__file__).resolve().parents[2]
    script = """
import json, sys
from importlib import metadata, resources
import django
from django.conf import settings
from django.db import transaction
import psycopg
from psycopg.types.json import Jsonb
data = json.load(sys.stdin)
settings.configure(DATABASES={'default': {
    'ENGINE': 'django.db.backends.postgresql', 'NAME': data['database'],
    'USER': 'aegis_migrator', 'PASSWORD': data['migrator_password'],
    'HOST': data['host'], 'PORT': data['port'],
}}, INSTALLED_APPS=[])
django.setup()
from aegis_apps.common import database_privileges
assert '/app/.venv/' in database_privileges.__file__
wheel_files = {str(path) for path in metadata.files('aegis-platform')}
for name in ('schedule.sql', 'lease.sql', 'start_run.sql', 'observations.sql', 'finalize.sql'):
    assert 'aegis_apps/indexing/sql/' + name in wheel_files
    resource = resources.files('aegis_apps.indexing').joinpath('sql', name)
    assert '/app/.venv/' in str(resource)
with transaction.atomic():
    database_privileges.synchronize_database_privileges()
with psycopg.connect(dbname=data['database'], host=data['host'], port=data['port'],
        user='aegis_web', password=data['web_password'], autocommit=True) as web:
    manual_run = web.execute('SELECT public.aegis_request_root_scan(%s,%s,%s,%s)',
        [data['manual_root'], data['actor'], data['actor_epoch'], 'artifact_manual']).fetchone()[0]
    assert manual_run is not None
with psycopg.connect(dbname=data['database'], host=data['host'], port=data['port'],
        user='aegis_indexer', password=data['indexer_password'], autocommit=True) as caller:
    assert caller.execute('SELECT session_user, current_user').fetchone() == (
        'aegis_indexer', 'aegis_indexer')
    run = caller.execute('SELECT public.aegis_schedule_root_scan(%s,%s,%s)',
        [data['root'], data['worker'], data['digest']]).fetchone()[0]
    assert run is not None
    lease = caller.execute('SELECT public.aegis_claim_scan_directory(%s,%s)',
        [run, data['worker']]).fetchone()[0]
    assert caller.execute('SELECT public.aegis_renew_scan_directory(%s)',
        [Jsonb(lease)]).fetchone()[0] is True
    manual_lease = caller.execute('SELECT public.aegis_claim_scan_directory(%s,%s)',
        [manual_run, data['worker']]).fetchone()[0]
    assert manual_lease['root_id'] == data['manual_root']
    assert manual_lease['generation'] == lease['generation'] == 1
    assert manual_lease['attempt'] == lease['attempt'] == 1
    assert caller.execute('SELECT public.aegis_renew_scan_directory(%s)',
        [Jsonb(manual_lease)]).fetchone()[0] is True
    batch = {'sequence': 1, 'observations': [{
        'raw': 'c2FmZQ==', 'display': 'safe', 'name_key': 'c2FmZQ==', 'type_hint': None,
        'kind': 'file', 'state': 'present', 'size': 1, 'mtime_ns': 2, 'ctime_ns': 3,
        'device': 4, 'inode': 5,
    }]}
    assert caller.execute('SELECT public.aegis_record_scan_batch(%s,%s)',
        [Jsonb(lease), Jsonb(batch)]).fetchone()[0] == {'observed': 1, 'inserted': 1, 'changed': 0}
    assert caller.execute('SELECT public.aegis_seal_scan_directory(%s,%s)',
        [Jsonb(lease), Jsonb([1, 1, 1, 1])]).fetchone()[0] is True
    assert caller.execute('SELECT public.aegis_finalize_scan_directory(%s,%s)',
        [Jsonb(lease), 500]).fetchone()[0] == {'affected': 0, 'complete': True}
    assert caller.execute('SELECT public.aegis_fail_scan_directory(%s,%s)',
        [Jsonb(manual_lease), 'reader_timeout']).fetchone()[0] is True
print('Installed wheel shared initialization: periodic and manual runtime login round trip passed.')
"""
    try:
        built = subprocess.run(
            [*CONTAINER_COMMAND, "build", "--label", f"aegis.verify.owner={image_tag}",
             "--tag", image_tag,
             "--file", "docker/backend.Dockerfile", "."],
            cwd=repository, capture_output=True, text=True, timeout=300,
        )
        assert built.returncode == 0, built.stderr[-4000:]
        # Build time must not make the synthetic heartbeat stale before the runtime proof.
        with connection.cursor() as cursor:
            cursor.execute("UPDATE operations_workerheartbeat SET last_seen_at=clock_timestamp() "
                           "WHERE worker_id=%s", [worker])
        host = "host.docker.internal" if sys.platform == "darwin" else "127.0.0.1"
        network = [] if sys.platform == "darwin" else ["--network", "host"]
        result = subprocess.run(
            [*CONTAINER_COMMAND, "run", "--rm", "--interactive", *network, "--workdir", "/tmp",
             "--entrypoint", "python", image_tag, "-I", "-c", script],
            input=json.dumps({
                "host": host, "port": role_database.port, "database": role_database.database_name,
                "migrator_password": role_database.passwords["aegis_migrator"],
                "indexer_password": role_database.passwords["aegis_indexer"],
                "web_password": role_database.passwords["aegis_web"],
                "manual_root": str(manual_root.pk), "actor": str(actor.pk),
                "actor_epoch": actor.authorization_epoch,
                "root": str(root.pk), "worker": worker, "digest": digest,
            }), capture_output=True, text=True, timeout=60,
        )
        assert result.returncode == 0, result.stderr[-4000:]
        assert "runtime login round trip passed" in result.stdout
    finally:
        inspected = subprocess.run([*CONTAINER_COMMAND, "image", "inspect", image_tag],
                                   capture_output=True, text=True, timeout=30)
        if inspected.returncode == 0:
            info = json.loads(inspected.stdout)[0]
            assert info["Config"]["Labels"].get("aegis.verify.owner") == image_tag
            subprocess.run([*CONTAINER_COMMAND, "image", "rm", image_tag],
                           check=True, capture_output=True, timeout=30)


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


def _create_job_for_role(
    role: str,
    *,
    max_attempts: int = 5,
) -> Job:
    unique = uuid.uuid4().hex
    actor = User.objects.create_user(username=f"job-lifecycle-{unique}")
    operation = create_operation(
        actor=actor,
        request_id=f"lifecycle_{unique}",
        kind="foundation.probe",
        intent={"roots": []},
    )
    if role == "operations":
        job = operation.jobs.get(target_role=role)
        if max_attempts != job.max_attempts:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE public.operations_job SET max_attempts = %s WHERE id = %s",
                    [max_attempts, job.id],
                )
            job.refresh_from_db()
        return job
    return enqueue_job(
        operation=operation,
        target_role=role,
        max_attempts=max_attempts,
    )


def _clear_worker_heartbeats() -> None:
    with connection.cursor() as cursor:
        cursor.execute("DELETE FROM public.operations_workerheartbeat")



@pytest.mark.parametrize("role", RUNTIME_DATABASE_ROLES)
def test_catalog_actual_login_read_and_mutation_boundary(
    role_database: RoleDatabase, role: str
) -> None:
    from aegis_apps.catalog.models import CatalogEntry

    root = Root.objects.create(
        slot_id="catalog-role-test", display_name="Synthetic", mode="read_only"
    )
    anchor = CatalogEntry.objects.create(
        root=root, raw_name=b"", display_name="", name_key=b"", kind="directory"
    )
    with role_database.connect(role) as runtime:
        if role in ("aegis_web", "aegis_indexer"):
            with runtime.cursor() as cursor:
                cursor.execute("SELECT id FROM catalog_catalogentry WHERE id = %s", [anchor.pk])
                assert cursor.fetchone() == (anchor.pk,)
        else:
            _assert_sqlstate(runtime, "SELECT id FROM catalog_catalogentry")
        for statement in (
            "INSERT INTO catalog_catalogentry SELECT * FROM catalog_catalogentry WHERE false",
            "UPDATE catalog_catalogentry SET display_name = 'changed'",
            "DELETE FROM catalog_catalogentry",
            "TRUNCATE catalog_catalogentry",
        ):
            _assert_sqlstate(runtime, statement)
    anchor.refresh_from_db()
    assert anchor.display_name == ""


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


def test_sync_rejects_memberships_connected_to_the_migrator(
    role_database: RoleDatabase,
) -> None:
    unexpected_role = f"aegis_membership_{uuid.uuid4().hex[:12]}"
    with connection.cursor() as cursor:
        cursor.execute(
            sql.SQL("CREATE ROLE {} NOLOGIN").format(_quote(unexpected_role))
        )
        cursor.execute(
            sql.SQL("GRANT {} TO aegis_migrator").format(_quote(unexpected_role))
        )
    try:
        with pytest.raises(
            PrivilegeSynchronizationError,
            match="managed database role membership is unsafe",
        ):
            role_database.synchronize()
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL("REVOKE {} FROM aegis_migrator").format(
                    _quote(unexpected_role)
                )
            )
            cursor.execute(
                sql.SQL("DROP ROLE {}").format(_quote(unexpected_role))
            )
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


def test_sync_removes_every_unexpected_explicit_grantee(
    role_database: RoleDatabase,
) -> None:
    rogue_role = f'aegis rogue " {uuid.uuid4().hex[:8]}'
    rogue_password = secrets.token_urlsafe(48)
    heartbeat_signature = MANAGED_FUNCTION_SIGNATURES[
        "aegis_publish_operations_heartbeat"
    ]
    heartbeat_name = heartbeat_signature.split("(", maxsplit=1)[0]
    rogue_connection: psycopg.Connection[Any] | None = None
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(_quote(rogue_role), sql.Literal(rogue_password))
            )
            cursor.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    _quote(role_database.database_name),
                    _quote(rogue_role),
                )
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    _quote(rogue_role)
                )
            )
            cursor.execute(
                sql.SQL("GRANT SELECT ON public.operations_job TO {}").format(
                    _quote(rogue_role)
                )
            )
            cursor.execute(
                sql.SQL(
                    "GRANT UPDATE (intent) ON public.operations_operation TO {}"
                ).format(_quote(rogue_role))
            )
            cursor.execute(
                sql.SQL("GRANT USAGE ON public.auth_group_id_seq TO {}").format(
                    _quote(rogue_role)
                )
            )
            cursor.execute(
                sql.SQL(f"GRANT EXECUTE ON FUNCTION {heartbeat_signature} TO {{}}")
                .format(_quote(rogue_role))
            )
            cursor.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator "
                    "GRANT SELECT ON TABLES TO {}"
                ).format(_quote(rogue_role))
            )

        rogue_connection = psycopg.connect(
            dbname=role_database.database_name,
            host=role_database.host,
            port=role_database.port,
            user=rogue_role,
            password=rogue_password,
            connect_timeout=5,
            autocommit=True,
        )

        role_database.synchronize()

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT has_database_privilege(%s, current_database(), 'CONNECT'), "
                "has_schema_privilege(%s, 'public', 'USAGE'), "
                "has_table_privilege(%s, 'public.operations_job', 'SELECT'), "
                "has_column_privilege(%s, 'public.operations_operation', "
                "'intent', 'UPDATE'), "
                "has_sequence_privilege(%s, 'public.auth_group_id_seq', 'USAGE'), "
                "has_function_privilege(%s, %s, 'EXECUTE')",
                [
                    rogue_role,
                    rogue_role,
                    rogue_role,
                    rogue_role,
                    rogue_role,
                    rogue_role,
                    heartbeat_signature,
                ],
            )
            assert cursor.fetchone() == (False, False, False, False, False, False)
            cursor.execute(
                """
                SELECT count(*)
                  FROM pg_catalog.pg_default_acl AS defaults
                  CROSS JOIN LATERAL pg_catalog.aclexplode(defaults.defaclacl) AS acl
                 WHERE defaults.defaclrole = (
                           SELECT oid
                             FROM pg_catalog.pg_roles
                            WHERE rolname = 'aegis_migrator'
                       )
                   AND acl.grantee = (
                           SELECT oid FROM pg_catalog.pg_roles WHERE rolname = %s
                       )
                """,
                [rogue_role],
            )
            assert cursor.fetchone() == (0,)

        _assert_sqlstate(
            rogue_connection,
            f"SELECT {heartbeat_name}(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            _heartbeat_parameters(),
        )
    finally:
        if rogue_connection is not None:
            rogue_connection.close()
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP OWNED BY {}").format(_quote(rogue_role)))
            cursor.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(_quote(rogue_role)))


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


@pytest.mark.parametrize("key", [
    "scanObservedEntries", "scanCompletedDirectories", "scanDegradedDirectories", "unknown",
])
@pytest.mark.parametrize("value", [0, 9223372036854775807, True, False, -1, 2**63, 1.0, "1", None])
def test_scan_heartbeat_counter_python_and_actual_login_parity(
    role_database: RoleDatabase, key: str, value: object,
) -> None:
    from aegis_apps.operations.serializers import validate_heartbeat_metrics

    allowed = key != "unknown" and type(value) is int and 0 <= value <= 9223372036854775807
    metrics = {key: value}
    if allowed:
        assert validate_heartbeat_metrics(metrics) == metrics
    else:
        with pytest.raises(ValueError):
            validate_heartbeat_metrics(metrics)
    with role_database.connect("aegis_indexer") as caller:
        if allowed:
            heartbeat = _publish_heartbeat(
                caller, function_role="indexer", parameters=_heartbeat_parameters(metrics=metrics),
            )
            stored = caller.execute(
                "SELECT metrics, current_job_id FROM public.operations_workerheartbeat WHERE id=%s",
                [heartbeat],
            ).fetchone()
            assert stored == (metrics, None)
        else:
            with pytest.raises(psycopg.errors.InvalidParameterValue):
                caller.execute(
                    _heartbeat_statement("indexer"), _heartbeat_parameters(metrics=metrics),
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


def test_actual_worker_cannot_forge_job_state_or_fencing_fields(
    role_database: RoleDatabase,
) -> None:
    actor = User.objects.create_user(username=f"job-guard-{uuid.uuid4()}")
    operation = create_operation(
        actor=actor,
        request_id=f"job_guard_{uuid.uuid4().hex}",
        kind="foundation.probe",
        intent={"roots": []},
    )
    job = operation.jobs.get()

    with role_database.connect("aegis_operations") as worker:
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET state = 'succeeded', result = '{\"ok\": true}' "
            "WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET attempt_token = attempt_token + 1 WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET available_at = available_at + interval '1 day' WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )

        owner_id = str(uuid.uuid4())
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.operations_job
                   SET state = 'running', attempts = 1, attempt_token = 1,
                       execution_started_at = NULL, lease_owner = %s,
                       lease_expires_at = pg_catalog.clock_timestamp()
                                          + interval '30 seconds',
                       safe_error_code = NULL, safe_error_detail = NULL,
                       result = NULL
                 WHERE id = %s
                """,
                [owner_id, job.id],
            )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job SET lease_owner = %s WHERE id = %s",
            (str(uuid.uuid4()), job.id),
            sqlstate="55000",
        )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET attempt_token = attempt_token + 1 WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET attempts = attempts + 1, attempt_token = attempt_token + 1 "
            "WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET available_at = available_at + interval '1 day' WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET lease_expires_at = lease_expires_at + interval '1 day' "
            "WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )
        with worker.cursor() as cursor:
            cursor.execute(
                "UPDATE public.operations_job "
                "SET updated_at = '2000-01-01T00:00:00Z' WHERE id = %s "
                "RETURNING updated_at",
                [job.id],
            )
            updated_row = cursor.fetchone()
        assert updated_row is not None
        assert updated_row[0].year != 2000

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.operations_job
                   SET state = 'retry_wait', available_at = pg_catalog.clock_timestamp(),
                       execution_started_at = NULL, lease_owner = NULL,
                       lease_expires_at = NULL,
                       safe_error_code = 'claim_relinquished',
                       safe_error_detail = 'The unstarted claim was released safely.',
                       result = NULL
                 WHERE id = %s
                """,
                [job.id],
            )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET safe_error_code = 'retryable_failure', "
            "safe_error_detail = 'The job will be retried.' WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )

        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE public.operations_job
                   SET state = 'succeeded', execution_started_at = NULL,
                       lease_owner = NULL, lease_expires_at = NULL,
                       safe_error_code = NULL, safe_error_detail = NULL,
                       result = '{"ok": true}'
                 WHERE id = %s
                """,
                [job.id],
            )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET result = '{\"ok\": false}' WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )
        _assert_sqlstate(
            worker,
            "UPDATE public.operations_job "
            "SET state = 'failed', result = NULL, "
            "safe_error_code = 'handler_failed', "
            "safe_error_detail = 'The job handler failed safely.' WHERE id = %s",
            (job.id,),
            sqlstate="55000",
        )


@pytest.mark.parametrize("role", ("operations", "indexer", "media"))
def test_actual_worker_can_claim_renew_start_and_finish_its_job(
    role_database: RoleDatabase,
    role: str,
) -> None:
    job = _create_job_for_role(role)
    worker_id = str(uuid.uuid4())
    with (
        override_settings(AEGIS_PROCESS_ROLE=role),
        _django_login(f"aegis_{role}", role_database.passwords[f"aegis_{role}"]),
    ):
        claim_time = timezone.now()
        lease = claim_next_job(role, worker_id, claim_time)
        assert lease is not None and lease.job_id == job.id
        assert renew_lease(lease, now=claim_time) is True
        assert start_job_execution(lease, now=claim_time) is True
        assert finish_job(lease, result={"ok": True}, now=claim_time) is True

        stored = Job.objects.get(pk=job.id)
        assert stored.state == "succeeded"
        assert stored.execution_started_at is not None
        assert stored.result == {"ok": True}
        assert stored.lease_owner is None
        assert stored.lease_expires_at is None


def test_actual_worker_can_relinquish_and_immediately_reclaim(
    role_database: RoleDatabase,
) -> None:
    job = _create_job_for_role("operations")
    with (
        override_settings(AEGIS_PROCESS_ROLE="operations"),
        _django_login(
            "aegis_operations",
            role_database.passwords["aegis_operations"],
        ),
    ):
        first = claim_next_job("operations", str(uuid.uuid4()), timezone.now())
        assert first is not None and first.job_id == job.id
        assert relinquish_job(first, now=timezone.now()) is True
        replacement = claim_next_job(
            "operations",
            str(uuid.uuid4()),
            timezone.now(),
        )
        assert replacement is not None and replacement.job_id == job.id
        assert replacement.attempt_token == first.attempt_token + 1

        stored = Job.objects.get(pk=job.id)
        assert stored.state == "running"
        assert stored.attempts == 1
        assert stored.execution_started_at is None


def test_actual_worker_can_retry_before_and_after_execution_start(
    role_database: RoleDatabase,
) -> None:
    first_job = _create_job_for_role("media")
    second_job = _create_job_for_role("media")
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE public.operations_job SET priority = 2 WHERE id = %s",
            [first_job.id],
        )
        cursor.execute(
            "UPDATE public.operations_job SET priority = 1 WHERE id = %s",
            [second_job.id],
        )
    with (
        override_settings(AEGIS_PROCESS_ROLE="media"),
        _django_login("aegis_media", role_database.passwords["aegis_media"]),
    ):
        first = claim_next_job("media", str(uuid.uuid4()), timezone.now())
        assert first is not None and first.job_id == first_job.id
        assert retry_job(
            first,
            error_code="retryable_failure",
            now=timezone.now(),
        ) is True

        second = claim_next_job("media", str(uuid.uuid4()), timezone.now())
        assert second is not None and second.job_id == second_job.id
        assert start_job_execution(second, now=timezone.now()) is True
        assert retry_job(
            second,
            error_code="retryable_failure",
            now=timezone.now(),
        ) is True

        for job_id in (first_job.id, second_job.id):
            stored = Job.objects.get(pk=job_id)
            assert stored.state == "retry_wait"
            assert stored.execution_started_at is None
            assert stored.lease_owner is None
            assert stored.safe_error_code == "retryable_failure"
            assert stored.safe_error_detail == "The job will be retried."


def test_actual_worker_can_publish_only_fixed_safe_failure_transitions(
    role_database: RoleDatabase,
) -> None:
    authorization_job = _create_job_for_role("indexer")
    handler_job = _create_job_for_role("indexer")
    exhausted_job = _create_job_for_role("indexer", max_attempts=1)
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE public.operations_job SET priority = 30 WHERE id = %s",
            [authorization_job.id],
        )
        cursor.execute(
            "UPDATE public.operations_job SET priority = 20 WHERE id = %s",
            [handler_job.id],
        )
        cursor.execute(
            "UPDATE public.operations_job SET priority = 10 WHERE id = %s",
            [exhausted_job.id],
        )

    with (
        override_settings(AEGIS_PROCESS_ROLE="indexer"),
        _django_login("aegis_indexer", role_database.passwords["aegis_indexer"]),
    ):
        authorization = claim_next_job(
            "indexer",
            str(uuid.uuid4()),
            timezone.now(),
        )
        assert authorization is not None
        assert authorization.job_id == authorization_job.id
        assert fail_job(
            authorization,
            error_code="authorization_stale",
            now=timezone.now(),
        ) is True

        handler = claim_next_job("indexer", str(uuid.uuid4()), timezone.now())
        assert handler is not None and handler.job_id == handler_job.id
        assert start_job_execution(handler, now=timezone.now()) is True
        assert fail_job(
            handler,
            error_code="handler_failed",
            now=timezone.now(),
        ) is True

        exhausted = claim_next_job("indexer", str(uuid.uuid4()), timezone.now())
        assert exhausted is not None and exhausted.job_id == exhausted_job.id
        assert retry_job(
            exhausted,
            error_code="retryable_failure",
            now=timezone.now(),
        ) is True

        assert Job.objects.get(pk=authorization_job.id).safe_error_detail == (
            "Authorization changed before execution completed."
        )
        assert Job.objects.get(pk=handler_job.id).safe_error_detail == (
            "The job handler failed safely."
        )
        exhausted_stored = Job.objects.get(pk=exhausted_job.id)
        assert exhausted_stored.state == "failed"
        assert exhausted_stored.safe_error_code == "attempts_exhausted"
        assert exhausted_stored.safe_error_detail == (
            "The job exhausted its attempts."
        )


def test_actual_worker_can_take_over_expired_started_attempt(
    role_database: RoleDatabase,
) -> None:
    job = _create_job_for_role("operations")
    original_worker = str(uuid.uuid4())
    with (
        override_settings(AEGIS_PROCESS_ROLE="operations"),
        _django_login(
            "aegis_operations",
            role_database.passwords["aegis_operations"],
        ),
    ):
        original = claim_next_job("operations", original_worker, timezone.now())
        assert original is not None and original.job_id == job.id
        assert start_job_execution(original, now=timezone.now()) is True
        original_started_at = Job.objects.get(pk=job.id).execution_started_at
        assert original_started_at is not None

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE public.operations_job "
            "SET lease_expires_at = pg_catalog.clock_timestamp() - interval '1 second' "
            "WHERE id = %s",
            [job.id],
        )

    replacement_worker = str(uuid.uuid4())
    with (
        override_settings(AEGIS_PROCESS_ROLE="operations"),
        _django_login(
            "aegis_operations",
            role_database.passwords["aegis_operations"],
        ),
    ):
        replacement = claim_next_job(
            "operations",
            replacement_worker,
            timezone.now(),
        )
        assert replacement is not None and replacement.job_id == job.id
        assert replacement.attempt_token == original.attempt_token + 1
        taken_over = Job.objects.get(pk=job.id)
        assert taken_over.execution_started_at is None
        assert taken_over.lease_owner == replacement_worker
        assert start_job_execution(replacement, now=timezone.now()) is True
        assert finish_job(
            replacement,
            result={"ok": True},
            now=timezone.now(),
        ) is True


def test_actual_worker_terminally_fences_an_expired_final_attempt(
    role_database: RoleDatabase,
) -> None:
    job = _create_job_for_role("media", max_attempts=1)
    with (
        override_settings(AEGIS_PROCESS_ROLE="media"),
        _django_login("aegis_media", role_database.passwords["aegis_media"]),
    ):
        lease = claim_next_job("media", str(uuid.uuid4()), timezone.now())
        assert lease is not None and lease.job_id == job.id
        assert start_job_execution(lease, now=timezone.now()) is True
        started_at = Job.objects.get(pk=job.id).execution_started_at
        assert started_at is not None

    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE public.operations_job "
            "SET lease_expires_at = pg_catalog.clock_timestamp() - interval '1 second' "
            "WHERE id = %s",
            [job.id],
        )

    with (
        override_settings(AEGIS_PROCESS_ROLE="media"),
        _django_login("aegis_media", role_database.passwords["aegis_media"]),
    ):
        assert claim_next_job("media", str(uuid.uuid4()), timezone.now()) is None
        stored = Job.objects.get(pk=job.id)
        assert stored.state == "failed"
        assert stored.attempt_token == lease.attempt_token + 1
        assert stored.execution_started_at == started_at
        assert stored.safe_error_code == "attempts_exhausted"
