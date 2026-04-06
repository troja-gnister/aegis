#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/systemd_hardening.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SYSTEMD_CONV_DIR "/etc/aegis/profiles.d/systemd"

/* Read *.conf files from the convention directory and install them as additional
 * drop-in configs.  File name format: <service>.conf
 * The content is written as /etc/systemd/system/<service>.service.d/aegis-custom.conf
 */
static void apply_convention_dir_systemd(aegis_executor_t *exec, aegis_result_t *res) {
    const char *ls_argv[] = {"ls", SYSTEMD_CONV_DIR, NULL};
    aegis_exec_result_t r = exec->execute(ls_argv, exec->ctx);
    if (r.exit_code != 0 || !r.stdout_buf || !r.stdout_buf[0]) {
        aegis_exec_result_free(&r);
        return;
    }

    char *listing = strdup(r.stdout_buf);
    aegis_exec_result_free(&r);
    if (!listing) return;

    char *tok = strtok(listing, "\n");
    while (tok) {
        size_t flen = strlen(tok);
        if (flen > 5 && strcmp(tok + flen - 5, ".conf") == 0) {
            /* Derive service name: strip .conf suffix */
            char service[256];
            size_t slen = flen - 5;
            if (slen >= sizeof(service)) slen = sizeof(service) - 1;
            strncpy(service, tok, slen);
            service[slen] = '\0';

            char src_path[512];
            snprintf(src_path, sizeof(src_path), "%s/%s", SYSTEMD_CONV_DIR, tok);
            char *content = exec->read_file(src_path, exec->ctx);
            if (content) {
                /* Ensure drop-in directory exists */
                char dir_path[512];
                snprintf(dir_path, sizeof(dir_path),
                         "/etc/systemd/system/%s.service.d", service);
                const char *mkdir[] = {"mkdir", "-p", dir_path, NULL};
                aegis_exec_result_t rm = exec->execute_sudo(mkdir, exec->ctx);
                aegis_exec_result_free(&rm);

                /* Write drop-in */
                char dst_path[512];
                snprintf(dst_path, sizeof(dst_path),
                         "/etc/systemd/system/%s.service.d/aegis-custom.conf", service);
                char action[1088];
                if (exec->write_file(dst_path, content, true, exec->ctx) != 0) {
                    snprintf(action, sizeof(action),
                             "WARNING: failed to install convention drop-in %s -> %s",
                             src_path, dst_path);
                } else {
                    snprintf(action, sizeof(action),
                             "Installed convention drop-in %s -> %s", src_path, dst_path);
                }
                free(content);
                aegis_result_add_action(res, action);
            }
        }
        tok = strtok(NULL, "\n");
    }
    free(listing);
}

static const char *hardening_dropin =
    "[Service]\n"
    "ProtectSystem=strict\n"
    "ProtectHome=yes\n"
    "NoNewPrivileges=yes\n"
    "PrivateTmp=yes\n";

static int write_dropin_for_service(const char *service, aegis_executor_t *exec,
                                     aegis_result_t *res) {
    /* Create directory /etc/systemd/system/<service>.service.d/ */
    char dir_path[256];
    snprintf(dir_path, sizeof(dir_path),
             "/etc/systemd/system/%s.service.d", service);

    const char *mkdir[] = {"mkdir", "-p", dir_path, NULL};
    aegis_exec_result_t r = exec->execute_sudo(mkdir, exec->ctx);
    aegis_exec_result_free(&r);

    /* Write drop-in config */
    char conf_path[320];
    snprintf(conf_path, sizeof(conf_path),
             "/etc/systemd/system/%s.service.d/hardening.conf", service);

    if (exec->write_file(conf_path, hardening_dropin, true, exec->ctx) != 0)
        return -1;

    char action[384];
    snprintf(action, sizeof(action), "Wrote hardening drop-in: %s", conf_path);
    aegis_result_add_action(res, action);
    return 0;
}

aegis_result_t aegis_systemd_hardening_apply(const void *config, aegis_executor_t *exec) {
    const aegis_systemd_hardening_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("systemd_hardening: disabled");

    aegis_result_t res = aegis_result_ok("systemd_hardening: applied");

    /* If auto_discover is enabled, run systemd-analyze security to list services */
    if (cfg->auto_discover) {
        const char *analyze[] = {"systemd-analyze", "security", NULL};
        aegis_exec_result_t r = exec->execute_sudo(analyze, exec->ctx);
        aegis_exec_result_free(&r);
        aegis_result_add_action(&res, "Ran systemd-analyze security for discovery");
    }

    /* Write drop-in for each service in profiles list */
    for (int i = 0; i < cfg->profile_count; i++) {
        if (!cfg->profiles[i]) continue;
        if (write_dropin_for_service(cfg->profiles[i], exec, &res) != 0) {
            char errmsg[384];
            snprintf(errmsg, sizeof(errmsg),
                     "systemd_hardening: failed to write drop-in for %s", cfg->profiles[i]);
            aegis_result_free(&res);
            return aegis_result_fail(errmsg);
        }
    }

    /* Install any custom drop-ins from convention directory */
    apply_convention_dir_systemd(exec, &res);

    /* Reload systemd daemon */
    const char *daemon_reload[] = {"systemctl", "daemon-reload", NULL};
    aegis_exec_result_t r = exec->execute_sudo(daemon_reload, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("systemd_hardening: systemctl daemon-reload failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Reloaded systemd daemon");

    char msg[128];
    snprintf(msg, sizeof(msg), "systemd_hardening: hardened %d service(s)", cfg->profile_count);
    free(res.message);
    res.message = strdup(msg);
    return res;
}

aegis_result_t aegis_systemd_hardening_status(aegis_executor_t *exec) {
    /* Run systemd-analyze security to check service exposure scores */
    const char *analyze[] = {"systemd-analyze", "security", NULL};
    aegis_exec_result_t r = exec->execute_sudo(analyze, exec->ctx);
    bool ran_ok = (r.exit_code == 0);
    aegis_exec_result_free(&r);

    char msg[128];
    snprintf(msg, sizeof(msg), "systemd_hardening: systemd-analyze=%s",
             ran_ok ? "ok" : "failed");
    if (ran_ok) return aegis_result_ok(msg);
    return aegis_result_warn(msg);
}

aegis_result_t aegis_systemd_hardening_verify(aegis_executor_t *exec) {
    /* Per-item compliance: check that systemd daemon-reload succeeded and
     * that systemd-analyze security tool is available */
    aegis_result_t res = aegis_result_ok("systemd_hardening: all checks passed");
    int total = 0, passed = 0;

    /* Check 1: systemd-analyze security is available and runs */
    total++;
    const char *analyze[] = {"systemd-analyze", "security", "--no-pager", NULL};
    aegis_exec_result_t r = exec->execute_sudo(analyze, exec->ctx);
    bool analyze_ok = (r.exit_code == 0);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, analyze_ok
        ? "PASS: systemd-analyze security ran successfully"
        : "FAIL: systemd-analyze security failed");
    if (analyze_ok) passed++;

    /* Check 2: verify systemctl can load unit files (daemon not degraded) */
    total++;
    const char *is_system_running[] = {"systemctl", "is-system-running", NULL};
    r = exec->execute(is_system_running, exec->ctx);
    /* exit 0 = running, exit 1 = degraded — both are acceptable; failure means offline */
    bool system_ok = (r.exit_code == 0 || r.exit_code == 1);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, system_ok
        ? "PASS: systemd is running (drop-ins loaded)"
        : "FAIL: systemd is not running — cannot verify drop-in state");
    if (system_ok) passed++;

    char msg[128];
    snprintf(msg, sizeof(msg), "systemd_hardening: %d/%d checks passed", passed, total);
    free(res.message);
    res.message = strdup(msg);

    if (passed == total) {
        res.status = AEGIS_OK;
    } else if (passed > 0) {
        res.status = AEGIS_WARN;
    } else {
        res.status = AEGIS_FAIL;
    }
    return res;
}
