#!/bin/sh
set -eu

fail_closed() {
    printf '%s\n' 'database role initialization refused' >&2
    exit 1
}

for secret_path in \
    /run/secrets/db_migrator_password \
    /run/secrets/db_web_password \
    /run/secrets/db_operations_password \
    /run/secrets/db_indexer_password \
    /run/secrets/db_media_password
do
    if [ -L "$secret_path" ] || [ ! -f "$secret_path" ] || [ ! -s "$secret_path" ] || [ ! -r "$secret_path" ]; then
        fail_closed
    fi
    if ! stat -c '%u:%g:%a:%s' "$secret_path" | awk -F: '
        NR == 1 {
            if ($1 != "70") next
            if ($2 != "70") next
            if ($3 != "400" && $3 != "600") next
            if ($4 < 1 || $4 > 4096) next
            valid = 1
        }
        END { exit(valid ? 0 : 1) }
    '; then
        fail_closed
    fi
done

unset \
    POSTGRES_PASSWORD \
    POSTGRES_PASSWORD_FILE \
    PGPASSWORD \
    PGPASSFILE \
    PGOPTIONS \
    PGSERVICE \
    PGSERVICEFILE

psql \
    --no-psqlrc \
    --no-password \
    --host=/var/run/postgresql \
    --username "$POSTGRES_USER" \
    --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 <<'SQL'
\set VERBOSITY terse
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_duration = off;
SET LOCAL log_min_duration_statement = -1;
SET LOCAL log_min_duration_sample = -1;
SET LOCAL log_parameter_max_length = 0;
SET LOCAL log_parameter_max_length_on_error = 0;
SET LOCAL log_min_error_statement = 'panic';
SET LOCAL log_error_verbosity = 'terse';
SET LOCAL password_encryption = 'scram-sha-256';
SET LOCAL search_path = pg_catalog;

DO $aegis_role_setup$
DECLARE
    managed_role name;
    role_password text;
    migrator_password text := pg_catalog.regexp_replace(
        pg_catalog.pg_read_file('/run/secrets/db_migrator_password', 0, 4097),
        E'\\r?\\n$',
        ''
    );
    web_password text := pg_catalog.regexp_replace(
        pg_catalog.pg_read_file('/run/secrets/db_web_password', 0, 4097),
        E'\\r?\\n$',
        ''
    );
    operations_password text := pg_catalog.regexp_replace(
        pg_catalog.pg_read_file('/run/secrets/db_operations_password', 0, 4097),
        E'\\r?\\n$',
        ''
    );
    indexer_password text := pg_catalog.regexp_replace(
        pg_catalog.pg_read_file('/run/secrets/db_indexer_password', 0, 4097),
        E'\\r?\\n$',
        ''
    );
    media_password text := pg_catalog.regexp_replace(
        pg_catalog.pg_read_file('/run/secrets/db_media_password', 0, 4097),
        E'\\r?\\n$',
        ''
    );
BEGIN
    IF session_user <> current_user
       OR NOT EXISTS (
           SELECT 1
             FROM pg_catalog.pg_roles
            WHERE rolname = session_user
              AND rolsuper
       ) THEN
        RAISE EXCEPTION 'database role initialization requires its bootstrap owner';
    END IF;

    FOREACH managed_role IN ARRAY ARRAY[
        'aegis_migrator'::name,
        'aegis_web'::name,
        'aegis_operations'::name,
        'aegis_indexer'::name,
        'aegis_media'::name
    ]
    LOOP
        IF EXISTS (
            SELECT 1
              FROM pg_catalog.pg_authid
             WHERE rolname = managed_role
               AND (
                    rolsuper
                    OR rolinherit
                    OR rolcreatedb
                    OR rolcreaterole
                    OR rolreplication
                    OR rolbypassrls
               )
        ) OR EXISTS (
            SELECT 1
              FROM pg_catalog.pg_auth_members AS membership
              JOIN pg_catalog.pg_authid AS existing_role
                ON existing_role.oid = membership.roleid
                OR existing_role.oid = membership.member
             WHERE existing_role.rolname = managed_role
        ) THEN
            RAISE EXCEPTION 'managed database role has an unsafe security state';
        END IF;
    END LOOP;

    FOREACH role_password IN ARRAY ARRAY[
        migrator_password,
        web_password,
        operations_password,
        indexer_password,
        media_password
    ]
    LOOP
        IF role_password = '' OR pg_catalog.octet_length(role_password) > 4096 THEN
            RAISE EXCEPTION 'invalid database role secret';
        END IF;
    END LOOP;

    FOR managed_role, role_password IN
        SELECT role_data.role_name, role_data.password
          FROM (
              VALUES
                  ('aegis_migrator'::name, migrator_password),
                  ('aegis_web'::name, web_password),
                  ('aegis_operations'::name, operations_password),
                  ('aegis_indexer'::name, indexer_password),
                  ('aegis_media'::name, media_password)
          ) AS role_data(role_name, password)
    LOOP
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = managed_role
            ) THEN
                EXECUTE pg_catalog.format(
                    'ALTER ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                    'NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
                    managed_role,
                    role_password
                );
            ELSE
                EXECUTE pg_catalog.format(
                    'CREATE ROLE %I LOGIN NOINHERIT NOSUPERUSER NOCREATEDB '
                    'NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L',
                    managed_role,
                    role_password
                );
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE EXCEPTION 'database role credential mutation failed';
        END;
    END LOOP;

    EXECUTE pg_catalog.format(
        'ALTER DATABASE %I OWNER TO %I',
        pg_catalog.current_database(),
        'aegis_migrator'
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL PRIVILEGES ON DATABASE %I FROM PUBLIC',
        pg_catalog.current_database()
    );
    EXECUTE pg_catalog.format(
        'REVOKE ALL PRIVILEGES ON DATABASE %I FROM %I, %I, %I, %I',
        pg_catalog.current_database(),
        'aegis_web',
        'aegis_operations',
        'aegis_indexer',
        'aegis_media'
    );
    EXECUTE pg_catalog.format(
        'GRANT CONNECT ON DATABASE %I TO %I, %I, %I, %I',
        pg_catalog.current_database(),
        'aegis_web',
        'aegis_operations',
        'aegis_indexer',
        'aegis_media'
    );
END
$aegis_role_setup$;

ALTER SCHEMA public OWNER TO aegis_migrator;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM PUBLIC;
REVOKE ALL PRIVILEGES ON SCHEMA public
    FROM aegis_web, aegis_operations, aegis_indexer, aegis_media;
GRANT USAGE ON SCHEMA public
    TO aegis_web, aegis_operations, aegis_indexer, aegis_media;

ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator
    REVOKE ALL PRIVILEGES ON TABLES
    FROM PUBLIC, aegis_web, aegis_operations, aegis_indexer, aegis_media;
ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator
    REVOKE ALL PRIVILEGES ON SEQUENCES
    FROM PUBLIC, aegis_web, aegis_operations, aegis_indexer, aegis_media;
ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator
    REVOKE ALL PRIVILEGES ON FUNCTIONS
    FROM PUBLIC, aegis_web, aegis_operations, aegis_indexer, aegis_media;
ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator IN SCHEMA public
    REVOKE ALL PRIVILEGES ON TABLES
    FROM aegis_web, aegis_operations, aegis_indexer, aegis_media;
ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator IN SCHEMA public
    REVOKE ALL PRIVILEGES ON SEQUENCES
    FROM aegis_web, aegis_operations, aegis_indexer, aegis_media;
ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator IN SCHEMA public
    REVOKE ALL PRIVILEGES ON FUNCTIONS
    FROM aegis_web, aegis_operations, aegis_indexer, aegis_media;

COMMIT;
SQL
