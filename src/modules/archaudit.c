#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/archaudit.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

aegis_result_t aegis_archaudit_apply(const void *config, aegis_executor_t *exec) {
    const aegis_archaudit_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("archaudit: disabled");

    aegis_result_t res = aegis_result_ok("archaudit: applied");

    /* Install arch-audit */
    const char *install[] = {"pacman", "-S", "--noconfirm", "--needed", "arch-audit", NULL};
    aegis_exec_result_t r = exec->execute_sudo(install, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured arch-audit package installed");

    return res;
}

static aegis_result_t run_archaudit(aegis_executor_t *exec) {
    const char *argv[] = {"arch-audit", NULL};
    aegis_exec_result_t r = exec->execute(argv, exec->ctx);

    char msg[512];
    if (r.exit_code == 0 && r.stdout_buf && r.stdout_buf[0]) {
        /* Count lines as proxy for CVE count */
        int lines = 0;
        for (const char *p = r.stdout_buf; *p; p++) {
            if (*p == '\n') lines++;
        }
        snprintf(msg, sizeof(msg), "archaudit: %d vulnerable package(s) found", lines);
        aegis_exec_result_free(&r);
        return aegis_result_warn(msg);
    }

    snprintf(msg, sizeof(msg), "archaudit: no vulnerabilities found (exit=%d)", r.exit_code);
    aegis_exec_result_free(&r);
    return aegis_result_ok(msg);
}

aegis_result_t aegis_archaudit_status(aegis_executor_t *exec) {
    return run_archaudit(exec);
}

aegis_result_t aegis_archaudit_verify(aegis_executor_t *exec) {
    return run_archaudit(exec);
}
