#!/bin/sh
set -eu

ready_file="${AEGIS_GATEWAY_READY_FILE:-/tmp/aegis-upstream-ready}"
rm -f "$ready_file"

log_event() {
    level="$1"
    message="$2"
    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    printf '{"timestamp":"%s","level":"%s","logger":"gateway.startup","message":"%s"}\n' \
        "$timestamp" "$level" "$message" >&2
}

mount_attest="/usr/local/bin/aegis-mount-attest"
if [ ! -x "$mount_attest" ]; then
    mount_attest="$(dirname "$0")/entrypoint/10-aegis-mount-attestation.sh"
fi
if ! /bin/sh "$mount_attest"; then
    exit 1
fi

public_url="${AEGIS_PUBLIC_URL:-}"
case "$public_url" in
    http://*)
        forwarded_proto="http"
        authority="${public_url#http://}"
        ;;
    https://*)
        forwarded_proto="https"
        authority="${public_url#https://}"
        ;;
    *)
        log_event ERROR "Gateway startup configuration invalid"
        exit 1
        ;;
esac
authority="${authority%%/*}"
case "$authority" in
    ""|*[[:space:]]*)
        log_event ERROR "Gateway startup configuration invalid"
        exit 1
        ;;
esac

max_attempts=40
attempt_limit="${AEGIS_GATEWAY_ATTESTATION_ATTEMPTS:-$max_attempts}"
case "$attempt_limit" in
    ""|*[!0-9]*)
        log_event ERROR "Gateway startup configuration invalid"
        exit 1
        ;;
esac
if [ "$attempt_limit" -lt 1 ] || [ "$attempt_limit" -gt "$max_attempts" ]; then
    log_event ERROR "Gateway startup configuration invalid"
    exit 1
fi

attempt=0
while [ "$attempt" -lt "$attempt_limit" ]; do
    attempt=$((attempt + 1))
    if wget -q -O /dev/null -T 2 \
        --header "Host: $authority" \
        --header "X-Forwarded-Proto: $forwarded_proto" \
        --header "X-Forwarded-For: 192.0.2.254" \
        --header "X-Aegis-Proxy-Attestation: startup-v1" \
        http://web:8000/health/proxy-attestation; then
        : > "$ready_file"
        log_event INFO "Upstream health check succeeded"
        exec nginx -g "daemon off;"
    fi
    if [ "$attempt" -lt "$attempt_limit" ]; then
        sleep 1
    fi
done

log_event ERROR "Upstream health check failed"
exit 1
