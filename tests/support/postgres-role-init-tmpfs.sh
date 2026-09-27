#!/usr/bin/env bash
# Test-only preparation for the direct base-image role/SQL fixture. The caller
# sources the production metadata validators, after inspecting the exact owned
# container before start. This file is never packaged in an application image.
# Its single fixed empty root deliberately has no production source-secret set.
aegis_prepare_role_init_tmpfs() (
    set -Eeuo pipefail
    export LC_ALL=C
    trap aegis_pg_refuse HUP INT TERM
    [[ $(id -u) == 0 && $(id -g) == 0 ]] || aegis_pg_refuse
    local metadata before current ancestor device inode bits uid gid mode size
    local -A parents=()
    metadata=$(aegis_pg_mount_snapshot) || aegis_pg_refuse
    for ancestor in / /run; do
        current=$(aegis_pg_stat "$ancestor") || aegis_pg_refuse
        if [[ $ancestor == / ]]; then
            # O6 measured rootless Podman's read-only overlay root at 0555, not
            # 0755 (Docker); the same widening R2 applied to the production helper.
            aegis_pg_require_node "$current" 16384 0:0 755 555
        else
            aegis_pg_require_node "$current" 16384 0:0 755
        fi
        parents[$ancestor]=$current
    done
    before=$(aegis_pg_stat /run/secrets) || aegis_pg_refuse
    IFS=: read -r device inode bits uid gid mode size <<< "$before"
    [[ $uid:$gid == 0:0 || $uid:$gid == 70:70 ]] || aegis_pg_refuse
    aegis_pg_require_node "$before" 16384 "$uid:$gid" 700
    aegis_pg_require_tmpfs "$metadata" /run/secrets "$before"
    aegis_pg_require_empty /run/secrets
    for ancestor in / /run; do
        current=$(aegis_pg_stat "$ancestor") || aegis_pg_refuse
        [[ $current == "${parents[$ancestor]}" ]] || aegis_pg_refuse
    done
    current=$(aegis_pg_mount_snapshot) || aegis_pg_refuse
    [[ $current == "$metadata" ]] || aegis_pg_refuse
    current=$(aegis_pg_stat /run/secrets) || aegis_pg_refuse
    [[ $current == "$before" ]] || aegis_pg_refuse
    aegis_pg_require_empty /run/secrets
    if [[ $uid:$gid == 0:0 ]]; then
        chown 70:70 -- /run/secrets 2>/dev/null || aegis_pg_refuse
        before="$device:$inode:$bits:70:70:700:$size"
        current=$(aegis_pg_stat /run/secrets) || aegis_pg_refuse
        [[ $current == "$before" ]] || aegis_pg_refuse
        chmod 0700 -- /run/secrets 2>/dev/null || aegis_pg_refuse
    fi
    current=$(aegis_pg_mount_snapshot) || aegis_pg_refuse
    [[ $current == "$metadata" ]] || aegis_pg_refuse
    current=$(aegis_pg_stat /run/secrets) || aegis_pg_refuse
    [[ $current == "$before" ]] || aegis_pg_refuse
    aegis_pg_require_empty /run/secrets
)
