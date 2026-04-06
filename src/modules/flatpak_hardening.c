#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/flatpak_hardening.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

aegis_result_t aegis_flatpak_hardening_apply(const void *config, aegis_executor_t *exec) {
    const aegis_flatpak_hardening_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("flatpak_hardening: disabled");

    aegis_result_t res = aegis_result_ok("flatpak_hardening: applied");

    const char *policy = cfg->policy ? cfg->policy : "strict";

    /* strict: restrict filesystem home and ssh-auth socket */
    if (strcmp(policy, "strict") == 0 || strcmp(policy, "lockdown") == 0) {
        const char *strict_fs[] = {"flatpak", "override", "--system",
                                    "--nofilesystem=home", "--nosocket=ssh-auth", NULL};
        aegis_exec_result_t r = exec->execute_sudo(strict_fs, exec->ctx);
        aegis_exec_result_free(&r);
        aegis_result_add_action(&res, "Applied strict filesystem/socket restrictions");
    }

    /* lockdown: add additional restrictions */
    if (strcmp(policy, "lockdown") == 0) {
        const char *lockdown[] = {"flatpak", "override", "--system",
                                   "--nofilesystem=host",
                                   "--nosocket=x11",
                                   "--nosocket=wayland",
                                   "--no-talk-name=org.freedesktop.NetworkManager",
                                   NULL};
        aegis_exec_result_t r = exec->execute_sudo(lockdown, exec->ctx);
        aegis_exec_result_free(&r);
        aegis_result_add_action(&res, "Applied lockdown additional restrictions");
    }

    char msg[128];
    snprintf(msg, sizeof(msg), "flatpak_hardening: applied %s policy", policy);
    free(res.message);
    res.message = strdup(msg);
    return res;
}

aegis_result_t aegis_flatpak_hardening_status(aegis_executor_t *exec) {
    const char *argv[] = {"flatpak", "override", "--system", "--show", NULL};
    aegis_exec_result_t r = exec->execute(argv, exec->ctx);
    bool ok = (r.exit_code == 0);
    aegis_exec_result_free(&r);

    if (ok) return aegis_result_ok("flatpak_hardening: system overrides present");
    return aegis_result_warn("flatpak_hardening: no system overrides configured");
}

aegis_result_t aegis_flatpak_hardening_verify(aegis_executor_t *exec) {
    /* Per-item compliance: check system overrides for key restrictions */
    aegis_result_t res = aegis_result_ok("flatpak_hardening: all checks passed");
    int total = 0, passed = 0;

    const char *show_argv[] = {"flatpak", "override", "--system", "--show", NULL};
    aegis_exec_result_t r = exec->execute(show_argv, exec->ctx);
    bool ok = (r.exit_code == 0);
    const char *out = (ok && r.stdout_buf) ? r.stdout_buf : "";

    /* Check 1: overrides command succeeds */
    total++;
    aegis_result_add_action(&res, ok
        ? "PASS: flatpak system overrides are configured"
        : "FAIL: flatpak override --system --show failed — no overrides configured");
    if (ok) passed++;

    /* Check 2: nofilesystem=home restriction present */
    total++;
    bool no_home = (strstr(out, "nofilesystem=home") != NULL ||
                    strstr(out, "filesystems=!home") != NULL);
    aegis_result_add_action(&res, no_home
        ? "PASS: home filesystem access restricted"
        : "FAIL: home filesystem restriction not found in overrides");
    if (no_home) passed++;

    /* Check 3: ssh-auth socket restricted */
    total++;
    bool no_ssh = (strstr(out, "nosocket=ssh-auth") != NULL ||
                   strstr(out, "sockets=!ssh-auth") != NULL);
    aegis_result_add_action(&res, no_ssh
        ? "PASS: ssh-auth socket access restricted"
        : "FAIL: ssh-auth socket restriction not found in overrides");
    if (no_ssh) passed++;

    aegis_exec_result_free(&r);

    char msg[128];
    snprintf(msg, sizeof(msg), "flatpak_hardening: %d/%d checks passed", passed, total);
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
