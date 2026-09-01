#!/bin/sh
set -eu

umask 077
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
secret_dir="$repository_dir/deploy/secrets/dev"
mkdir -p "$secret_dir"

for name in \
    postgres-superuser-password \
    db-migrator-password \
    db-web-password \
    db-operations-password \
    db-indexer-password \
    db-media-password \
    django-secret-key
do
    destination="$secret_dir/$name"
    if [ -s "$destination" ]; then
        chmod 600 "$destination"
        printf 'kept %s\n' "$name"
        continue
    fi

    openssl rand -hex 32 >"$destination"
    chmod 600 "$destination"
    printf 'created %s\n' "$name"
done
