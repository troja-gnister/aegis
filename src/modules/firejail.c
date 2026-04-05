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
    return aegis_firejail_status(exec);
}
