from __future__ import annotations

import pytest
from django.db import connection
from psycopg import sql

from tests.support.database_roles import managed_role_database

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _boundary_acls() -> tuple[tuple[str, str, bool, str], ...]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT boundary.kind,
                   CASE acl.grantee
                       WHEN 0 THEN 'PUBLIC'
                       ELSE pg_catalog.pg_get_userbyid(acl.grantee)
                   END,
                   acl.privilege_type,
                   acl.is_grantable
              FROM pg_catalog.pg_database AS database
              JOIN pg_catalog.pg_namespace AS namespace
                ON namespace.nspname = 'public'
              CROSS JOIN LATERAL (
                  VALUES
                      (
                          'database',
                          COALESCE(
                              database.datacl,
                              pg_catalog.acldefault('d', database.datdba)
                          )
                      ),
                      (
                          'schema',
                          COALESCE(
                              namespace.nspacl,
                              pg_catalog.acldefault('n', namespace.nspowner)
                          )
                      )
              ) AS boundary(kind, privileges)
              CROSS JOIN LATERAL pg_catalog.aclexplode(boundary.privileges) AS acl
             WHERE database.datname = pg_catalog.current_database()
             ORDER BY boundary.kind, 2, 3, 4
            """
        )
        return tuple(cursor.fetchall())


def test_managed_role_database_restores_database_and_schema_acls() -> None:
    before = _boundary_acls()

    with managed_role_database():
        assert _boundary_acls() != before

    assert _boundary_acls() == before


def test_managed_role_database_restores_named_grants_and_grant_options() -> None:
    role = "aegis_acl_preservation_probe"
    with connection.cursor() as cursor:
        cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {} WITH GRANT OPTION").format(
                sql.Identifier(connection.settings_dict["NAME"]), sql.Identifier(role)
            )
        )
        cursor.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {} WITH GRANT OPTION").format(
                sql.Identifier(role)
            )
        )
    try:
        before = _boundary_acls()
        assert ("database", role, "CONNECT", True) in before
        assert ("schema", role, "USAGE", True) in before

        with managed_role_database():
            assert not any(entry[1] == role for entry in _boundary_acls())

        assert _boundary_acls() == before
    finally:
        with connection.cursor() as cursor:
            cursor.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
