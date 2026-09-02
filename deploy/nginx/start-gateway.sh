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

attempt=0
while [ "$attempt" -lt 40 ]; do
    attempt=$((attempt + 1))
    if wget -q -O /dev/null -T 2 \
        --header "Host: $authority" \
        --header "X-Forwarded-Proto: $forwarded_proto" \
        http://web:8000/health/live; then
        : > "$ready_file"
        log_event INFO "Upstream health check succeeded"
        exec nginx -g "daemon off;"
    fi
    if [ "$attempt" -lt 40 ]; then
        sleep 1
    fi
done

log_event ERROR "Upstream health check failed"
exit 1
