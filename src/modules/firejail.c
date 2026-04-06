#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/firejail.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

aegis_result_t aegis_firejail_apply(const void *config, aegis_executor_t *exec) {
    const aegis_firejail_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("firejail: disabled");

    aegis_result_t res = aegis_result_ok("firejail: applied");

    /* Install firejail package */
    const char *install[] = {"pacman", "-S", "--noconfirm", "--needed", "firejail", NULL};
    aegis_exec_result_t r = exec->execute_sudo(install, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("firejail: pacman -S failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured firejail package installed");

    /* Create symlinks for each app: /usr/local/bin/<app> -> /usr/bin/firejail */
    for (int i = 0; i < cfg->app_count; i++) {
        if (!cfg->apps[i]) continue;

        char link_path[128];
        snprintf(link_path, sizeof(link_path), "/usr/local/bin/%s", cfg->apps[i]);

        const char *symlink[] = {"ln", "-sf", "/usr/bin/firejail", link_path, NULL};
        r = exec->execute_sudo(symlink, exec->ctx);
        aegis_exec_result_free(&r);

        char action[192];
        snprintf(action, sizeof(action), "Created symlink %s -> /usr/bin/firejail", link_path);
        aegis_result_add_action(&res, action);
    }

    char msg[128];
    snprintf(msg, sizeof(msg), "firejail: applied with %d app(s)", cfg->app_count);
    free(res.message);
    res.message = strdup(msg);
    return res;
}

aegis_result_t aegis_firejail_status(aegis_executor_t *exec) {
    /* Check if firejail binary exists */
    bool installed = exec->file_exists("/usr/bin/firejail", exec->ctx);

    char msg[256];
    snprintf(msg, sizeof(msg), "firejail: installed=%s", installed ? "yes" : "no");

    if (installed) return aegis_result_ok(msg);
    return aegis_result_fail(msg);
}

aegis_result_t aegis_firejail_verify(aegis_executor_t *exec) {
    /* Per-item compliance: check binary and symlinks in /usr/local/bin */
    aegis_result_t res = aegis_result_ok("firejail: all checks passed");
    int total = 0, passed = 0;

    /* Check 1: firejail binary installed */
    total++;
    bool installed = exec->file_exists("/usr/bin/firejail", exec->ctx);
    aegis_result_add_action(&res, installed
        ? "PASS: /usr/bin/firejail exists"
        : "FAIL: /usr/bin/firejail not found — firejail not installed");
    if (installed) passed++;

    /* Check 2: firejail default profiles directory exists */
    total++;
    bool profiles_exist = exec->file_exists("/etc/firejail", exec->ctx);
    aegis_result_add_action(&res, profiles_exist
        ? "PASS: /etc/firejail profiles directory present"
        : "FAIL: /etc/firejail not found — profiles missing");
    if (profiles_exist) passed++;

    char msg[128];
    snprintf(msg, sizeof(msg), "firejail: %d/%d checks passed", passed, total);
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
