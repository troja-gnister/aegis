#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/rkhunter.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define RKHUNTER_DB "/var/lib/rkhunter/db/rkhunter.dat"

aegis_result_t aegis_rkhunter_apply(const void *config, aegis_executor_t *exec) {
    const aegis_rkhunter_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("rkhunter: disabled");

    aegis_result_t res = aegis_result_ok("rkhunter: applied");

    /* Install rkhunter */
    const char *install[] = {"pacman", "-S", "--noconfirm", "--needed", "rkhunter", NULL};
    aegis_exec_result_t r = exec->execute_sudo(install, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured rkhunter package installed");

    /* Update rkhunter data files */
    const char *update_argv[] = {"rkhunter", "--update", NULL};
    r = exec->execute_sudo(update_argv, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Updated rkhunter data files with --update");

    /* Update system file properties database */
    const char *propupd_argv[] = {"rkhunter", "--propupd", NULL};
    r = exec->execute_sudo(propupd_argv, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Updated rkhunter file properties database with --propupd");

    return res;
}

aegis_result_t aegis_rkhunter_status(aegis_executor_t *exec) {
    /* Check if rkhunter binary exists */
    const char *which_argv[] = {"which", "rkhunter", NULL};
    aegis_exec_result_t r = exec->execute(which_argv, exec->ctx);
    bool installed = (r.exit_code == 0);
    aegis_exec_result_free(&r);

    bool db_exists = exec->file_exists(RKHUNTER_DB, exec->ctx);

    char msg[128];
    snprintf(msg, sizeof(msg), "rkhunter: installed=%s database=%s",
             installed ? "yes" : "no", db_exists ? "present" : "missing");
    if (installed && db_exists) return aegis_result_ok(msg);
    if (installed || db_exists) return aegis_result_warn(msg);
    return aegis_result_fail(msg);
}

aegis_result_t aegis_rkhunter_verify(aegis_executor_t *exec) {
    bool db_exists = exec->file_exists(RKHUNTER_DB, exec->ctx);
    if (!db_exists) return aegis_result_fail("rkhunter: database missing, run apply first");

    const char *argv[] = {"rkhunter", "--check", "--skip-keypress", NULL};
    aegis_exec_result_t r = exec->execute_sudo(argv, exec->ctx);
    int exit_code = r.exit_code;
    aegis_exec_result_free(&r);

    char msg[128];
    snprintf(msg, sizeof(msg), "rkhunter: check exit_code=%d%s",
             exit_code, exit_code == 0 ? " (clean)" : " (warnings)");
    if (exit_code == 0) return aegis_result_ok(msg);
    return aegis_result_warn(msg);
}
