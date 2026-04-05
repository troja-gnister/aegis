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
    return aegis_flatpak_hardening_status(exec);
}
