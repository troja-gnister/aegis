#!/usr/bin/env bash
# Sourced by the fixed PostgreSQL bootstrap. Mountinfo proves effective state;
# canonical configuration and engine inspection must separately prove provenance.
# In particular, an operator-substituted host tmpfs-root bind is not distinguishable
# from every private tmpfs using only the container's mount namespace.

aegis_pg_refuse() {
    printf '%s\n' 'database secret staging refused' >&2
    exit 1
}

aegis_pg_mount_snapshot() {
    local metadata
    # The sentinel retains the final newline through command substitution. One
    # extra byte detects overflow; fullblock avoids treating a short procfs read
    # as a complete document. Neither input nor diagnostics contain secret bytes.
    metadata=$(dd if=/proc/self/mountinfo bs=1048577 count=1 iflag=fullblock 2>/dev/null && printf '.') || return 1
    [[ ${#metadata} -le 1048577 && $metadata == *$'\n.' ]] || return 1
    metadata=${metadata%.}
    printf '%s' "$metadata" | awk '
        function bad() { invalid = 1; exit 1 }
        {
            if (NR > 4096 || length($0) > 8192 || NF < 10) bad()
            if ($1 !~ /^[0-9]+$/ || $2 !~ /^[0-9]+$/ ||
                $3 !~ /^[0-9]+:[0-9]+$/ || $4 !~ /^\// || $5 !~ /^\//) bad()
            if (ids[$1]++ || points[$5]++) bad()
            separator = 0
            for (i = 7; i <= NF; i++) {
                if ($i == "-") { if (separator) bad(); separator = i }
            }
            if (!separator || separator + 3 != NF) bad()
            if ($6 !~ /^(ro|rw)(,|$)/ || $(separator + 3) !~ /^(ro|rw)(,|$)/) bad()
            for (i = 7; i < separator; i++) {
                if ($i !~ /^(shared|master|propagate_from):[0-9]+$/ && $i != "unbindable") bad()
            }
            print
        }
        END { if (invalid || NR == 0) exit 1 }
    ' || return 1
}

aegis_pg_stat() {
    local metadata
    metadata=$(stat -c '%d:%i:%f:%u:%g:%a:%s' "$1" 2>/dev/null) || return 1
    [[ $metadata =~ ^([0-9]{1,18}):([0-9]{1,18}):([0-9a-f]{1,8}):([0-9]{1,10}):([0-9]{1,10}):([0-7]{3,4}):([0-9]{1,18})$ ]] || return 1
    printf '%s\n' "$metadata"
}

aegis_pg_require_node() {
    local metadata=$1 kind=$2 owner=$3 mode=$4 alt_mode=${5-}
    local device inode bits uid gid actual_mode size
    IFS=: read -r device inode bits uid gid actual_mode size <<< "$metadata"
    [[ $((16#$bits & 0170000)) -eq $kind && $uid:$gid == "$owner" ]] || aegis_pg_refuse
    [[ $actual_mode == "$mode" || ( -n $alt_mode && $actual_mode == "$alt_mode" ) ]] || aegis_pg_refuse
}

aegis_pg_require_tmpfs() {
    local metadata=$1 target=$2 node=$3 size device
    case "$target" in
        /run/aegis-source-secrets|/run/secrets) size=65536 ;;
        /run/postgresql|/tmp) size=16777216 ;;
        *) aegis_pg_refuse ;;
    esac
    device=${node%%:*}
    # Linux dev_t encoding, derived from container stat, not mount uid/gid options.
    device="$(((device >> 8 & 4095) | (device >> 32 & 4294963200))):$(((device & 255) | (device >> 12 & 4294967040)))"
    printf '%s\n' "$metadata" | awk -v target="$target" -v size="$size" -v device="$device" '
        function has(options, name) { return index("," options ",", "," name ",") != 0 }
        function bytes(value, suffix, scale) {
            if (value !~ /^[0-9]+[kKmM]?$/) return -1
            suffix = substr(value, length(value), 1)
            scale = (suffix == "k" || suffix == "K") ? 1024 : ((suffix == "m" || suffix == "M") ? 1048576 : 1)
            return (value + 0) * scale
        }
        $5 == target {
            found++
            separator = 7
            while ($separator != "-" && separator <= NF) separator++
            if ($3 != device || $4 != "/" || $(separator + 1) != "tmpfs" ||
                $(separator + 2) != "tmpfs" || separator != 7 ||
                !has($6, "rw") || has($6, "ro") || !has($6, "nosuid") ||
                !has($6, "nodev") || !has($6, "noexec") ||
                has($6, "suid") || has($6, "dev") || has($6, "exec")) invalid = 1
            count = split($(separator + 3), options, ",")
            sizes = 0
            for (i = 1; i <= count; i++) {
                if (options[i] ~ /^size=/) {
                    sizes++
                    if (bytes(substr(options[i], 6)) != size) invalid = 1
                }
            }
            if (sizes != 1 || !has($(separator + 3), "rw")) invalid = 1
        }
        index($5, target "/") == 1 && target != "/run/aegis-source-secrets" { invalid = 1 }
        END { exit(found == 1 && !invalid ? 0 : 1) }
    ' || aegis_pg_refuse
}

aegis_pg_require_empty() {
    local entry
    entry=$(find "$1" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null) || aegis_pg_refuse
    [[ -z $entry ]] || aegis_pg_refuse
}

aegis_pg_require_socket_alias() {
    local target
    target=$(readlink /var/run 2>/dev/null && printf '.') || aegis_pg_refuse
    [[ $target == $'../run\n.' ]] || aegis_pg_refuse
}

aegis_pg_require_source_layout() {
    local metadata=$1
    # Check directory entries separately from mount records: a stale unmounted
    # file must also refuse startup. Only six fixed paths may produce output.
    find /run/aegis-source-secrets -mindepth 1 -maxdepth 1 -print 2>/dev/null | awk '
        BEGIN {
            split("postgres_superuser_password db_migrator_password db_web_password db_operations_password db_indexer_password db_media_password", names, " ")
            for (i in names) expected["/run/aegis-source-secrets/" names[i]] = 1
        }
        { if (NR > 6 || !($0 in expected) || seen[$0]++) { invalid = 1; exit 1 } }
        END { exit(!invalid && NR == 6 ? 0 : 1) }
    ' || aegis_pg_refuse
    printf '%s\n' "$metadata" | awk '
        BEGIN {
            split("postgres_superuser_password db_migrator_password db_web_password db_operations_password db_indexer_password db_media_password", names, " ")
            for (i in names) expected["/run/aegis-source-secrets/" names[i]] = 1
        }
        index($5, "/run/aegis-source-secrets/") == 1 {
            if (!($5 in expected) || index("," $6 ",", ",ro,") == 0 ||
                index("," $6 ",", ",rw,") != 0 || $7 != "-") invalid = 1
            found++
        }
        END { exit(!invalid && found == 6 ? 0 : 1) }
    ' || aegis_pg_refuse
}

aegis_prepare_postgres_tmpfs() (
    set -Eeuo pipefail
    export LC_ALL=C
    trap aegis_pg_refuse HUP INT TERM
    [[ $(id -u) == 0 && $(id -g) == 0 ]] || aegis_pg_refuse
    local metadata target node uid gid mode bits device inode size current
    local -a directories=(/run/aegis-source-secrets /run/secrets /run/postgresql /tmp)
    local -a names=(postgres_superuser_password db_migrator_password db_web_password db_operations_password db_indexer_password db_media_password)
    local -a paths=(/ /var /run /var/run "${directories[@]}")
    local -A before=() expected=()
    for target in "${names[@]}"; do paths+=("/run/aegis-source-secrets/$target"); done
    metadata=$(aegis_pg_mount_snapshot) || aegis_pg_refuse
    for target in "${paths[@]}"; do
        node=$(aegis_pg_stat "$target") || aegis_pg_refuse
        before[$target]=$node
        expected[$target]=$node
    done
    # Rootless Podman's read-only overlay root is measured at 0555; Docker's is 0755.
    for target in /; do
        aegis_pg_require_node "${before[$target]}" 16384 0:0 755 555
    done
    for target in /var /run; do
        aegis_pg_require_node "${before[$target]}" 16384 0:0 755
    done
    aegis_pg_require_node "${before[/var/run]}" 40960 0:0 777
    aegis_pg_require_socket_alias
    for target in "${directories[@]}"; do
        node=${before[$target]}
        IFS=: read -r device inode bits uid gid mode size <<< "$node"
        case "$target" in
            /run/aegis-source-secrets) aegis_pg_require_node "$node" 16384 0:0 700 ;;
            *)
                [[ $uid:$gid == 0:0 || $uid:$gid == 70:70 ]] || aegis_pg_refuse
                case "$target" in /run/secrets) mode=700 ;; /run/postgresql) mode=775 ;; /tmp) mode=1777 ;; esac
                aegis_pg_require_node "$node" 16384 "$uid:$gid" "$mode"
                aegis_pg_require_empty "$target"
                ;;
        esac
        aegis_pg_require_tmpfs "$metadata" "$target" "$node"
    done
    aegis_pg_require_source_layout "$metadata"
    for target in "${names[@]}"; do
        node=${before[/run/aegis-source-secrets/$target]}
        IFS=: read -r device inode bits uid gid mode size <<< "$node"
        [[ $((16#$bits & 0170000)) -eq 32768 && $size -ge 1 && $size -le 4096 ]] || aegis_pg_refuse
        test -r "/run/aegis-source-secrets/$target" || aegis_pg_refuse
        # Each regular source inode must be the exact read-only mount observed.
        device="$(((device >> 8 & 4095) | (device >> 32 & 4294963200))):$(((device & 255) | (device >> 12 & 4294967040)))"
        printf '%s\n' "$metadata" | awk -v path="/run/aegis-source-secrets/$target" -v device="$device" '
            $5 == path { found++; if ($3 != device) invalid = 1 }
            END { exit(found == 1 && !invalid ? 0 : 1) }
        ' || aegis_pg_refuse
    done

    # Full validation above precedes every possible mutation. Recheck the whole
    # recorded set before each directory and after the final preparation. Inputs
    # are never ownership targets; any drift stops this attempt without repair.
    for target in /run/secrets /run/postgresql /tmp final; do
        current=$(aegis_pg_mount_snapshot) || aegis_pg_refuse
        [[ $current == "$metadata" ]] || aegis_pg_refuse
        for node in "${paths[@]}"; do
            current=$(aegis_pg_stat "$node") || aegis_pg_refuse
            [[ $current == "${expected[$node]}" ]] || aegis_pg_refuse
        done
        aegis_pg_require_socket_alias
        aegis_pg_require_source_layout "$metadata"
        for node in /run/secrets /run/postgresql /tmp; do aegis_pg_require_empty "$node"; done
        [[ $target != final ]] || break
        node=${expected[$target]}
        IFS=: read -r device inode bits uid gid mode size <<< "$node"
        [[ $uid:$gid == 0:0 ]] || continue
        case "$target" in /run/secrets) mode=0700 ;; /run/postgresql) mode=0775 ;; /tmp) mode=1777 ;; esac
        current=$(aegis_pg_stat "$target") || aegis_pg_refuse
        [[ $current == "${expected[$target]}" ]] || aegis_pg_refuse
        current=$(aegis_pg_mount_snapshot) || aegis_pg_refuse
        [[ $current == "$metadata" ]] || aegis_pg_refuse
        chown 70:70 -- "$target" 2>/dev/null || aegis_pg_refuse
        current=$(aegis_pg_stat "$target") || aegis_pg_refuse
        expected[$target]="$device:$inode:$bits:70:70:${mode#0}:$size"
        [[ $current == "${expected[$target]}" ]] || aegis_pg_refuse
        chmod "$mode" -- "$target" 2>/dev/null || aegis_pg_refuse
        current=$(aegis_pg_stat "$target") || aegis_pg_refuse
        [[ $current == "${expected[$target]}" ]] || aegis_pg_refuse
    done
)
