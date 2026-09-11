#!/usr/bin/env bash
set -Eeuo pipefail

readonly_source_dir=/run/aegis-source-secrets
staged_secret_dir=/run/secrets
reconcile_marker=/run/secrets/.aegis-staged
reconcile_hba=/run/secrets/.aegis-reconcile.pg_hba.conf

fail_closed() {
    printf '%s\n' 'database secret staging refused' >&2
    exit 1
}

validate_staged_file() {
    local staged_path=$1

    if [[ -L "$staged_path" || ! -f "$staged_path" || ! -s "$staged_path" || ! -r "$staged_path" ]]; then
        fail_closed
    fi
    if ! stat -c '%u:%g:%a:%s' "$staged_path" | awk -F: '
        NR == 1 {
            if ($1 != "70") next
            if ($2 != "70") next
            if ($3 != "400") next
            if ($4 < 1 || $4 > 4096) next
            valid = 1
        }
        END { exit(valid ? 0 : 1) }
    '; then
        fail_closed
    fi
}

validate_staged_secrets() {
    local secret_name

    for secret_name in \
        postgres_superuser_password \
        db_migrator_password \
        db_web_password \
        db_operations_password \
        db_indexer_password \
        db_media_password
    do
        validate_staged_file "$staged_secret_dir/$secret_name"
    done
}

clear_database_password_environment() {
    unset \
        POSTGRES_PASSWORD \
        POSTGRES_PASSWORD_FILE \
        PGPASSWORD \
        PGPASSFILE \
        PGOPTIONS \
        PGSERVICE \
        PGSERVICEFILE
}

stop_reconcile_server() {
    if pg_ctl -D "$PGDATA" status >/dev/null 2>&1; then
        pg_ctl -D "$PGDATA" -m fast -w stop
    fi
}

reconcile_existing_database() {
    if [[ "$(id -u)" != '70' || "$(id -g)" != '70' ]]; then
        fail_closed
    fi
    if [[ $# -lt 1 || $1 != 'postgres' || ! -s "$PGDATA/PG_VERSION" ]]; then
        fail_closed
    fi
    validate_staged_secrets
    validate_staged_file "$reconcile_marker"
    validate_staged_file "$reconcile_hba"
    if [[ ${POSTGRES_USER:-} != 'postgres' || -z ${POSTGRES_DB:-} ]]; then
        fail_closed
    fi

    clear_database_password_environment
    trap stop_reconcile_server EXIT HUP INT TERM
    pg_ctl -D "$PGDATA" \
        -o "-c listen_addresses='' -c unix_socket_directories=/var/run/postgresql -c hba_file=$reconcile_hba" \
        -w start
    /docker-entrypoint-initdb.d/001-roles.sh
    stop_reconcile_server
    trap - EXIT HUP INT TERM

    clear_database_password_environment
    umask 0022
    exec "$@"
}

if [[ ${1:-} == '--aegis-reconcile-existing-database' ]]; then
    shift
    reconcile_existing_database "$@"
fi

if [[ "$(id -u)" != '0' || "$(id -g)" != '0' ]]; then
    fail_closed
fi
if [[ -L "$readonly_source_dir" || ! -d "$readonly_source_dir" ]]; then
    fail_closed
fi
if [[ -L "$staged_secret_dir" || ! -d "$staged_secret_dir" ]]; then
    fail_closed
fi
if [[ "$(stat -c '%u:%g:%a' "$readonly_source_dir")" != '0:0:700' ]]; then
    fail_closed
fi
if [[ "$(stat -c '%u:%g:%a' "$staged_secret_dir")" != '70:70:700' ]]; then
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

    if [[ -L "$source_path" || ! -f "$source_path" || ! -s "$source_path" ]]; then
        fail_closed
    fi
    if ! stat -c '%s' "$source_path" | awk '
        NR == 1 && $1 >= 1 && $1 <= 4096 { valid = 1 }
        END { exit(valid ? 0 : 1) }
    '; then
        fail_closed
    fi
    if [[ -e "$staged_path" || -L "$staged_path" ]]; then
        fail_closed
    fi

    if ! cp "$source_path" "$staged_path"; then
        fail_closed
    fi
    if ! chown 70:70 "$staged_path" || ! chmod 0400 "$staged_path"; then
        fail_closed
    fi
    validate_staged_file "$staged_path"
done

printf '%s\n' 'aegis staged secrets v1' > "$reconcile_marker"
printf '%s\n' \
    'local all postgres peer' \
    'local all all reject' \
    'host all all all reject' > "$reconcile_hba"
chown 70:70 "$reconcile_marker" "$reconcile_hba"
chmod 0400 "$reconcile_marker" "$reconcile_hba"
validate_staged_file "$reconcile_marker"
validate_staged_file "$reconcile_hba"

unset PGPASSWORD PGPASSFILE PGOPTIONS PGSERVICE PGSERVICEFILE
if [[ ${1:-} == 'postgres' && -s "$PGDATA/PG_VERSION" ]]; then
    # Reuse the pinned image's directory and ownership preparation before dropping uid.
    source /usr/local/bin/docker-entrypoint.sh
    docker_create_db_directories
    exec gosu postgres "$0" --aegis-reconcile-existing-database "$@"
fi

umask 0022
exec /usr/local/bin/docker-entrypoint.sh "$@"
