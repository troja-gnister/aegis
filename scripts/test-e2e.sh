#!/usr/bin/env bash
set -euo pipefail
export UV_LOCKED=1

# Public CI failure locator: on failure, exactly one stdout line naming a constant
# phase from this allowlist. Never commands, output, paths, environment or values.
phase=config
annotate_phase() {
    local name=$1
    case "$name" in
        config|verify-diagnostics|prepare|resources-check|prepare-sources|build|\
        mounts-preflight|mounts-render|record-generated|prepare-runtime|check-compose|\
        up|resources-record|bootstrap-admin|seed|index-wait|playwright|resources-cleanup|\
        sources-verify|cleanup) ;;
        *) name=unknown ;;
    esac
    if [[ "${GITHUB_ACTIONS-}" == true ]]; then
        printf '::error title=e2e-phase::%s\n' "$name"
    fi
}
trap 'early_status=$?; if (( early_status != 0 )); then annotate_phase "$phase"; fi' EXIT

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repository_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
cd "$repository_dir"
case "${AEGIS_CONTAINER_ENGINE-docker}" in
    docker) container_engine=(docker) ;;
    podman) container_engine=(podman --remote=false) ;;
    *)
        printf '%s\n' "AEGIS_CONTAINER_ENGINE must be exactly 'docker' or 'podman'." >&2
        exit 64
        ;;
esac
if [[ "${container_engine[0]}" == podman ]]; then
    provider=${PODMAN_COMPOSE_PROVIDER:-}
    if [[ "$provider" != /* || ! -f "$provider" || -L "$provider" || ! -x "$provider" ]]; then
        printf '%s\n' "PODMAN_COMPOSE_PROVIDER must name an owned absolute executable." >&2
        exit 64
    fi
    read -r provider_owner provider_mode < <(stat -Lc '%u %a' -- "$provider")
    if [[ "$provider_owner" != "$(id -u)" ]] || (( (8#$provider_mode & 8#22) != 0 )); then
        printf '%s\n' "PODMAN_COMPOSE_PROVIDER must name an owned absolute executable." >&2
        exit 64
    fi
    podman_socket=${AEGIS_PODMAN_SOCKET:-}
    socket_parent=${podman_socket%/*}
    if [[ "$podman_socket" != /* || "$podman_socket" == *//* ||
          "$podman_socket" == */./* || "$podman_socket" == */../* ||
          "$socket_parent" == "$podman_socket" ||
          -L "$podman_socket" || ! -S "$podman_socket" || -L "$socket_parent" ||
          ! -d "$socket_parent" ]]; then
        printf '%s\n' "AEGIS_PODMAN_SOCKET must name an owned local socket in a private directory." >&2
        exit 64
    fi
    read -r socket_owner socket_mode < <(stat -Lc '%u %a' -- "$podman_socket")
    read -r parent_owner parent_mode < <(stat -Lc '%u %a' -- "$socket_parent")
    if [[ "$(id -u)" == 0 || "$socket_owner" != "$(id -u)" ||
          "$parent_owner" != "$(id -u)" || "$parent_mode" != 700 ]]; then
        printf '%s\n' "AEGIS_PODMAN_SOCKET must name an owned local socket in a private directory." >&2
        exit 64
    fi
    ancestor=${socket_parent%/*}
    [[ -n "$ancestor" ]] || ancestor=/
    while :; do
        if [[ -L "$ancestor" || ! -d "$ancestor" ]]; then
            printf '%s\n' "AEGIS_PODMAN_SOCKET has unsafe ancestry." >&2
            exit 64
        fi
        read -r ancestor_owner ancestor_mode < <(stat -Lc '%u %a' -- "$ancestor")
        if (( (8#$ancestor_mode & 8#22) != 0 )) &&
           [[ "$ancestor_owner" != "$(id -u)" ]] &&
           ! { [[ "$ancestor_owner" == 0 ]] && (( (8#$ancestor_mode & 8#1000) != 0 )); }; then
            printf '%s\n' "AEGIS_PODMAN_SOCKET has writable foreign ancestry." >&2
            exit 64
        fi
        [[ "$ancestor" == / ]] && break
        ancestor=${ancestor%/*}
        [[ -n "$ancestor" ]] || ancestor=/
    done
    compose_engine=(env "DOCKER_HOST=unix://$podman_socket" "${container_engine[@]}")
else
    compose_engine=("${container_engine[@]}")
fi
unset DOCKER_HOST DOCKER_CONTEXT CONTAINER_HOST CONTAINER_CONNECTION
if [[ "$(node -p "process.versions.node.split('.')[0]")" != 24 ]]; then
    printf '%s\n' "Phase 1 E2E requires Node.js 24 LTS." >&2
    exit 64
fi

# Uses the same locked browser package/config as acceptance. Its intentionally
# failing subprocess must never disclose a synthetic credential in diagnostics.
phase=verify-diagnostics
node frontend/e2e/verify-diagnostics.mjs

phase=prepare
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
export AEGIS_TEST_RESOURCE_TOKEN="${work_dir##*/}"
export AEGIS_UID="$(id -u)"
export AEGIS_GID="$(id -g)"
export AEGIS_DB_NAME=aegis
export AEGIS_DB_HOST=postgres
export AEGIS_DB_PORT=5432
export DJANGO_SETTINGS_MODULE=aegis.settings.test
unset COMPOSE_FILE COMPOSE_PROFILES AEGIS_MOUNT_MANIFEST AEGIS_MOUNT_MANIFEST_SHA256

compose_options=(--env-file /dev/null \
    --project-name aegis-phase1-e2e
    --project-directory "$repository_dir" -f compose.yaml -f compose.test.yaml)
compose_base=("${compose_engine[@]}" compose "${compose_options[@]}")
compose_options+=(-f "$work_dir/compose.mounts.yaml")
if [[ "${container_engine[0]}" == podman ]]; then
    compose_base+=(-f "$repository_dir/compose.podman.yaml")
    compose_options+=(-f "$repository_dir/compose.podman.yaml")
fi
compose=("${compose_engine[@]}" compose "${compose_options[@]}")

controlled_compose() {
    if [[ "${container_engine[0]}" == podman ]]; then
        uv run python scripts/e2e_support.py controlled-compose "${compose_options[@]}" "$@"
    else
        "${compose[@]}" "$@"
    fi
}
started=0

cleanup() {
    local result=$? cleanup_result=0 failed_phase=$phase cleanup_phase=resources-cleanup
    trap - EXIT
    set +e
    if (( started )); then
        if (( result != 0 )); then
            "${compose[@]}" logs --no-color --no-log-prefix --tail 500 2>&1 |
                uv run python scripts/e2e_support.py sanitize-logs >&2
        fi
    fi
    # Removing the exact recorded containers stops every worker; only then are the
    # owned sources compared with their pre-mount manifest, and only verified,
    # recorded fixture entries are deleted. Any refusal retains the fixture.
    uv run python scripts/e2e_support.py resources-cleanup "$work_dir" || cleanup_result=$?
    if (( cleanup_result == 0 )); then
        cleanup_phase=sources-verify
        uv run python scripts/e2e_support.py sources-verify "$work_dir" || cleanup_result=$?
    fi
    if (( cleanup_result == 0 )); then
        cleanup_phase=cleanup
        uv run python scripts/e2e_support.py cleanup "$work_dir" || cleanup_result=$?
    fi
    if (( result != 0 )); then
        annotate_phase "$failed_phase"
    elif (( cleanup_result != 0 )); then
        annotate_phase "$cleanup_phase"
        result=$cleanup_result
    fi
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

uv run python scripts/e2e_support.py prepare "$work_dir"
phase=resources-check
uv run python scripts/e2e_support.py resources-check "$work_dir"
phase=prepare-sources
uv run python scripts/e2e_support.py prepare-sources "$work_dir"
phase=build
"${compose_base[@]}" build
phase=mounts-preflight
uv run aegisctl mounts preflight --config "$work_dir/mounts.toml" --manifest "$work_dir/mounts.manifest.json"
phase=mounts-render
uv run aegisctl mounts render --config "$work_dir/mounts.toml" \
    --manifest "$work_dir/mounts.manifest.json" --output "$work_dir/compose.mounts.yaml" \
    --gateway-attestation "$work_dir/mounts.gateway.attestation"
# Preserve host-readable values before the generated credential files are mapped
# to subordinate container IDs. These shell variables are not exported to Compose.
phase=record-generated
IFS= read -r E2E_ALICE_PASSWORD < "$AEGIS_TEST_SECRET_DIR/e2e-alice-password"
IFS= read -r E2E_BOB_PASSWORD < "$AEGIS_TEST_SECRET_DIR/e2e-bob-password"
IFS= read -r E2E_ADMIN_PASSWORD < "$AEGIS_TEST_SECRET_DIR/e2e-admin-password"
uv run python scripts/e2e_support.py record-generated "$work_dir"
phase=prepare-runtime
uv run python scripts/e2e_support.py prepare-runtime "$work_dir"
phase=check-compose
"${compose[@]}" config --format json | uv run python scripts/e2e_support.py check-compose

started=1
phase=up
set +e
controlled_compose up --build --wait --wait-timeout 180
compose_status=$?
phase=resources-record
uv run python scripts/e2e_support.py resources-record "$work_dir" up
record_status=$?
set -e
if (( record_status != 0 )); then exit "$record_status"; fi
phase=up
if (( compose_status != 0 )); then exit "$compose_status"; fi
phase=resources-check
uv run python scripts/e2e_support.py resources-check "$work_dir"
phase=bootstrap-admin
set +e
controlled_compose run --rm --no-deps \
    --volume "$AEGIS_TEST_SECRET_DIR/e2e-admin-password:/run/secrets/bootstrap-password:ro" \
    web python manage.py bootstrap_admin --username phase1-admin \
    --email phase1-admin@e2e.invalid --password-file /run/secrets/bootstrap-password
compose_status=$?
phase=resources-record
uv run python scripts/e2e_support.py resources-record "$work_dir" run-web
record_status=$?
set -e
if (( record_status != 0 )); then exit "$record_status"; fi
phase=bootstrap-admin
if (( compose_status != 0 )); then exit "$compose_status"; fi
phase=seed
"${compose[@]}" exec -T web python manage.py seed_phase1_e2e
"${compose[@]}" exec -T web python manage.py seed_phase1_e2e

# Browser assertions start only after the real indexer has settled every root,
# observed through the authenticated status API with a bounded deadline.
phase=index-wait
E2E_ALICE_PASSWORD="$E2E_ALICE_PASSWORD" E2E_BOB_PASSWORD="$E2E_BOB_PASSWORD" \
    uv run python scripts/e2e_support.py index-wait "$work_dir"

# Credentials exist only in the browser runner process, never in command
# arguments, browser storage, traces, reports, or production configuration.
export E2E_ALICE_PASSWORD E2E_BOB_PASSWORD E2E_ADMIN_PASSWORD
export E2E_BASE_URL="$AEGIS_PUBLIC_URL"
phase=playwright
npm --prefix frontend run test:e2e
