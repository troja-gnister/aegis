#!/bin/sh
set -eu

psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 \
    --set=database_name="$POSTGRES_DB" <<'SQL'
\set migrator_password `cat /run/secrets/db_migrator_password`
\set web_password `cat /run/secrets/db_web_password`
\set operations_password `cat /run/secrets/db_operations_password`
\set indexer_password `cat /run/secrets/db_indexer_password`
\set media_password `cat /run/secrets/db_media_password`

SELECT format('CREATE ROLE aegis_migrator LOGIN PASSWORD %L', :'migrator_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aegis_migrator') \gexec
SELECT format('ALTER ROLE aegis_migrator PASSWORD %L', :'migrator_password') \gexec
SELECT format('CREATE ROLE aegis_web LOGIN PASSWORD %L', :'web_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aegis_web') \gexec
SELECT format('ALTER ROLE aegis_web PASSWORD %L', :'web_password') \gexec
SELECT format('CREATE ROLE aegis_operations LOGIN PASSWORD %L', :'operations_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aegis_operations') \gexec
SELECT format('ALTER ROLE aegis_operations PASSWORD %L', :'operations_password') \gexec
SELECT format('CREATE ROLE aegis_indexer LOGIN PASSWORD %L', :'indexer_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aegis_indexer') \gexec
SELECT format('ALTER ROLE aegis_indexer PASSWORD %L', :'indexer_password') \gexec
SELECT format('CREATE ROLE aegis_media LOGIN PASSWORD %L', :'media_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'aegis_media') \gexec
SELECT format('ALTER ROLE aegis_media PASSWORD %L', :'media_password') \gexec

ALTER DATABASE :"database_name" OWNER TO aegis_migrator;
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO aegis_web, aegis_operations, aegis_indexer, aegis_media;
GRANT USAGE ON SCHEMA public TO aegis_web, aegis_operations, aegis_indexer, aegis_media;
ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aegis_web;
ALTER DEFAULT PRIVILEGES FOR ROLE aegis_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO aegis_web;
SQL
