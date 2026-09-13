#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repository_dir"
if [[ "$(node -p "process.versions.node.split('.')[0]")" != 24 ]]; then
    printf '%s\n' "Phase 1 E2E requires Node.js 24 LTS." >&2
    exit 64
fi

# Refuse existing state before installing cleanup or starting any container.
for resource in container network volume; do
    existing=$(docker "$resource" ls -q --filter label=com.docker.compose.project=aegis-phase1-e2e)
    if [[ -n "$existing" ]]; then
        printf '%s\n' "The reserved E2E project already has resources; refusing to reuse them." >&2
        exit 64
    fi
done
existing=$(docker container ls -aq --filter label=com.docker.compose.project=aegis-phase1-e2e)
if [[ -n "$existing" ]]; then
    printf '%s\n' "The reserved E2E project has stopped containers; refusing to reuse them." >&2
    exit 64
fi

umask 077
work_dir=$(mktemp -d /tmp/aegis-phase1-e2e.XXXXXXXX)
work_dir=$(CDPATH= cd -- "$work_dir" && pwd -P)
export AEGIS_ENV=test
export AEGIS_RELEASE_ID=phase1-e2e
export AEGIS_PUBLIC_URL=http://127.0.0.1:18080
export AEGIS_ALLOWED_HOSTS=127.0.0.1,localhost,web
export AEGIS_HTTP_PORT=127.0.0.1:18080
export AEGIS_TEST_DB_PORT=55432
export AEGIS_TEST_SECRET_DIR="$work_dir/secrets"
export AEGIS_UID="$(id -u)"
export AEGIS_GID="$(id -g)"
export AEGIS_DB_NAME=aegis
export AEGIS_DB_HOST=postgres
export AEGIS_DB_PORT=5432
export DJANGO_SETTINGS_MODULE=aegis.settings.test
unset COMPOSE_FILE COMPOSE_PROFILES AEGIS_MOUNT_MANIFEST AEGIS_MOUNT_MANIFEST_SHA256

compose_base=(docker compose --env-file /dev/null --project-name aegis-phase1-e2e
    --project-directory "$repository_dir" -f compose.yaml -f compose.test.yaml)
compose=("${compose_base[@]}" -f "$work_dir/compose.mounts.yaml")
started=0

cleanup() {
    local result=$? cleanup_result=0
    trap - EXIT
    set +e
    if (( started )); then
        if (( result != 0 )); then
            "${compose[@]}" logs --no-color --no-log-prefix --tail 500 2>&1 |
                uv run python scripts/e2e_support.py sanitize-logs >&2
        fi
        "${compose[@]}" down --volumes --remove-orphans --timeout 10 || cleanup_result=$?
        if (( cleanup_result == 0 )); then
            printf '%s\n' "Removed disposable E2E containers and test volumes."
        fi
    fi
    uv run python scripts/e2e_support.py cleanup "$work_dir" || cleanup_result=$?
    if (( result == 0 && cleanup_result != 0 )); then result=$cleanup_result; fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

uv run python scripts/e2e_support.py prepare "$work_dir"
"${compose_base[@]}" build
uv run aegisctl mounts preflight --config "$work_dir/mounts.toml" --manifest "$work_dir/mounts.manifest.json"
uv run aegisctl mounts render --config "$work_dir/mounts.toml" \
    --manifest "$work_dir/mounts.manifest.json" --output "$work_dir/compose.mounts.yaml" \
    --gateway-attestation "$work_dir/mounts.gateway.attestation"
"${compose[@]}" config --format json | uv run python scripts/e2e_support.py check-compose

started=1
"${compose[@]}" up --build --wait --wait-timeout 180
"${compose[@]}" run --rm --no-deps \
    --volume "$AEGIS_TEST_SECRET_DIR/e2e-admin-password:/run/secrets/bootstrap-password:ro" \
    web python manage.py bootstrap_admin --username phase1-admin \
    --email phase1-admin@e2e.invalid --password-file /run/secrets/bootstrap-password
"${compose[@]}" exec -T web python manage.py seed_phase1_e2e
"${compose[@]}" exec -T web python manage.py seed_phase1_e2e

# Credentials exist only in the browser runner process, never in command
# arguments, browser storage, traces, reports, or production configuration.
IFS= read -r E2E_ALICE_PASSWORD < "$AEGIS_TEST_SECRET_DIR/e2e-alice-password"
IFS= read -r E2E_BOB_PASSWORD < "$AEGIS_TEST_SECRET_DIR/e2e-bob-password"
IFS= read -r E2E_ADMIN_PASSWORD < "$AEGIS_TEST_SECRET_DIR/e2e-admin-password"
export E2E_ALICE_PASSWORD E2E_BOB_PASSWORD E2E_ADMIN_PASSWORD
export E2E_BASE_URL="$AEGIS_PUBLIC_URL"
npm --prefix frontend run test:e2e
