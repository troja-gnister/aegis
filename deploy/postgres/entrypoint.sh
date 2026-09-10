#!/bin/sh
set -eu

readonly_source_dir=/run/aegis-source-secrets
staged_secret_dir=/run/secrets

fail_closed() {
    printf '%s\n' 'database secret staging refused' >&2
    exit 1
}

if [ -L "$readonly_source_dir" ] || [ ! -d "$readonly_source_dir" ]; then
    fail_closed
fi
if [ -L "$staged_secret_dir" ] || [ ! -d "$staged_secret_dir" ]; then
    fail_closed
fi
if [ "$(stat -c '%u:%g:%a' "$readonly_source_dir")" != '0:0:700' ]; then
    fail_closed
fi
if [ "$(stat -c '%u:%g:%a' "$staged_secret_dir")" != '70:70:700' ]; then
    fail_closed
fi

umask 077
for secret_name in \
    postgres_superuser_password \
    db_migrator_password \
    db_web_password \
    db_operations_password \
    db_indexer_password \
    db_media_password
do
    source_path="$readonly_source_dir/$secret_name"
    staged_path="$staged_secret_dir/$secret_name"

    if [ -L "$source_path" ] || [ ! -f "$source_path" ] || [ ! -s "$source_path" ]; then
        fail_closed
    fi
    if ! stat -c '%s' "$source_path" | awk '
        NR == 1 && $1 >= 1 && $1 <= 4096 { valid = 1 }
        END { exit(valid ? 0 : 1) }
    '; then
        fail_closed
    fi
    if [ -e "$staged_path" ] || [ -L "$staged_path" ]; then
        fail_closed
    fi

    if ! cp "$source_path" "$staged_path"; then
        fail_closed
    fi
    if ! chown 70:70 "$staged_path" || ! chmod 0400 "$staged_path"; then
        fail_closed
    fi
    if [ "$(stat -c '%u:%g:%a' "$staged_path")" != '70:70:400' ]; then
        fail_closed
    fi
done

unset PGPASSWORD PGPASSFILE PGOPTIONS PGSERVICE PGSERVICEFILE
exec /usr/local/bin/docker-entrypoint.sh "$@"
