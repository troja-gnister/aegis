#!/bin/sh
set -eu

attestation="${AEGIS_GATEWAY_MOUNT_ATTESTATION:-}"
expected_digest="${AEGIS_GATEWAY_MOUNT_ATTESTATION_SHA256:-}"

if [ -z "$attestation" ] && [ -z "$expected_digest" ]; then
    exit 0
fi

fail() {
    timestamp="$(date -u '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null || printf 'unknown')"
    printf '{"timestamp":"%s","level":"ERROR","logger":"gateway.mounts","message":"Gateway mount attestation failed"}\n' \
        "$timestamp" >&2
    exit 1
}

if [ "$attestation" != "/run/aegis/mounts.gateway.attestation" ]; then
    fail
fi
case "$expected_digest" in
    *[!0-9a-f]*|"") fail ;;
esac
if [ "${#expected_digest}" -ne 64 ]; then
    fail
fi
if [ ! -f "$attestation" ] || [ -L "$attestation" ]; then
    fail
fi

umask 077
work_dir="$(mktemp -d /tmp/aegis-mount-attest.XXXXXX 2>/dev/null)" || fail
attestation_snapshot="$work_dir/attestation"
mountinfo_snapshot="$work_dir/mountinfo"
cleanup() {
    rm -f "$attestation_snapshot" "$mountinfo_snapshot" >/dev/null 2>&1 || :
    rmdir "$work_dir" >/dev/null 2>&1 || :
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

head -c 131073 "$attestation" > "$attestation_snapshot" 2>/dev/null || fail
size="$(wc -c < "$attestation_snapshot" 2>/dev/null)" || fail
case "$size" in
    *[!0-9]*|"") fail ;;
esac
if [ "$size" -lt 1 ] || [ "$size" -gt 131072 ]; then
    fail
fi
actual_digest="$(sha256sum < "$attestation_snapshot" 2>/dev/null | awk '{print $1}')" || fail
if [ "$actual_digest" != "$expected_digest" ]; then
    fail
fi
if LC_ALL=C grep -q '[^ -~]' "$attestation_snapshot" 2>/dev/null; then
    fail
fi
if ! awk -F '|' 'length($0) > 1024 || NF != 6 { exit 1 } END { if (NR < 1 || NR > 128) exit 1 }' \
    "$attestation_snapshot" >/dev/null 2>&1; then
    fail
fi
last_byte="$(tail -c 1 "$attestation_snapshot" 2>/dev/null | od -An -tuC 2>/dev/null | tr -d '[:space:]')" || fail
[ "$last_byte" = 10 ] || fail

head -c 1048577 /proc/self/mountinfo > "$mountinfo_snapshot" 2>/dev/null || fail
mountinfo_size="$(wc -c < "$mountinfo_snapshot" 2>/dev/null)" || fail
case "$mountinfo_size" in
    *[!0-9]*|"") fail ;;
esac
if [ "$mountinfo_size" -gt 1048576 ]; then
    fail
fi

valid_uint() {
    value="$1"
    allow_zero="$2"
    case "$value" in
        *[!0-9]*|"") return 1 ;;
    esac
    case "$value" in
        0)
            [ "$allow_zero" = yes ]
            return
            ;;
        [1-9]|[1-9][0-9]*) ;;
        *) return 1 ;;
    esac
    length="${#value}"
    if [ "$length" -gt 19 ]; then
        return 1
    fi
    if [ "$length" -eq 19 ] && [ "$value" \> 9223372036854775807 ]; then
        return 1
    fi
}

valid_mountinfo_field() {
    printf '%s\n' "$1" | awk '
        {
            for (i = 1; i <= length($0); i++) {
                if (substr($0, i, 1) == "\\") {
                    escape = substr($0, i + 1, 3)
                    if (escape != "040" && escape != "011" && escape != "012" && escape != "134") {
                        exit 1
                    }
                    i += 3
                }
            }
        }
    ' >/dev/null 2>&1
}

option_mode() {
    options="$1"
    has_ro=no
    has_rw=no
    case ",$options," in *,ro,*) has_ro=yes ;; esac
    case ",$options," in *,rw,*) has_rw=yes ;; esac
    if [ "$has_ro" = "$has_rw" ]; then
        return 1
    fi
    if [ "$has_ro" = yes ]; then
        printf 'read_only'
    else
        printf 'read_write'
    fi
}

previous=""
count=0
while IFS='|' read -r slot_id target host_device host_inode declared_mode fingerprint; do
    count=$((count + 1))
    if ! printf '%s\n' "$slot_id" | grep -Eq '^[a-z][a-z0-9-]{0,62}$'; then
        fail
    fi
    if [ -n "$previous" ] && { [ "$slot_id" = "$previous" ] || [ "$slot_id" \< "$previous" ]; }; then
        fail
    fi
    previous="$slot_id"
    if [ "$target" != "/srv/aegis/roots/$slot_id" ]; then
        fail
    fi
    valid_uint "$host_device" yes || fail
    valid_uint "$host_inode" no || fail
    case "$declared_mode" in read_only|read_write) ;; *) fail ;; esac
    case "$fingerprint" in *[!0-9a-f]*|"") fail ;; esac
    if [ "${#fingerprint}" -ne 64 ]; then
        fail
    fi

    record="$(awk -v target="$target" '
        length($0) > 16384 { exit 2 }
        NR > 8192 { exit 2 }
        $5 == target {
            count++
            separator = 0
            for (i = 7; i <= NF; i++) if ($i == "-") separator = i
            if (separator == 0 || separator + 3 > NF) exit 2
            printf "%s\t%s\t%s\t%s\t%s\t%s\n", $3, $4, $(separator + 1), $(separator + 2), $6, $(separator + 3)
        }
        END { if (count != 1) exit 1 }
    ' "$mountinfo_snapshot" 2>/dev/null)" || fail
    tab="$(printf '\t')"
    IFS="$tab" read -r major_minor encoded_root filesystem_type mount_source mount_options super_options <<EOF
$record
EOF
    case "$major_minor" in
        *[!0-9:]*|*:*:*|":"|"") fail ;;
    esac
    major="${major_minor%%:*}"
    minor="${major_minor#*:}"
    valid_uint "$major" yes || fail
    valid_uint "$minor" yes || fail
    for field in "$encoded_root" "$filesystem_type" "$mount_source"; do
        [ -n "$field" ] || fail
        [ "${#field}" -le 4096 ] || fail
        valid_mountinfo_field "$field" || fail
    done
    per_mode="$(option_mode "$mount_options")" || fail
    super_mode="$(option_mode "$super_options")" || fail
    if [ "$per_mode" != read_only ] && [ "$super_mode" != read_only ]; then
        fail
    fi
    observed_fingerprint="$({
        printf 'aegis.mount-fingerprint.v1\000'
        printf '%s\000' "$major_minor" "$encoded_root" "$filesystem_type" "$mount_source"
    } | sha256sum 2>/dev/null | awk '{print $1}')" || fail
    if [ "$observed_fingerprint" != "$fingerprint" ]; then
        fail
    fi
done < "$attestation_snapshot"

if [ "$count" -lt 1 ] || [ "$count" -gt 128 ]; then
    fail
fi
