#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/systemd_hardening.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *hardening_dropin =
    "[Service]\n"
    "ProtectSystem=strict\n"
    "ProtectHome=yes\n"
    "NoNewPrivileges=yes\n"
    "PrivateTmp=yes\n";

static void write_dropin_for_service(const char *service, aegis_executor_t *exec,
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

    exec->write_file(conf_path, hardening_dropin, true, exec->ctx);

    char action[384];
    snprintf(action, sizeof(action), "Wrote hardening drop-in: %s", conf_path);
    aegis_result_add_action(res, action);
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
        write_dropin_for_service(cfg->profiles[i], exec, &res);
    }

    /* Reload systemd daemon */
    const char *daemon_reload[] = {"systemctl", "daemon-reload", NULL};
    aegis_exec_result_t r = exec->execute_sudo(daemon_reload, exec->ctx);
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
    return aegis_systemd_hardening_status(exec);
}
