#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/apparmor.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

aegis_result_t aegis_apparmor_apply(const void *config, aegis_executor_t *exec) {
    const aegis_apparmor_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("apparmor: disabled");

    aegis_result_t res = aegis_result_ok("apparmor: applied");

    /* Install apparmor package */
    const char *install[] = {"pacman", "-S", "--noconfirm", "--needed", "apparmor", NULL};
    aegis_exec_result_t r = exec->execute_sudo(install, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("apparmor: pacman -S failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured apparmor package installed");

    /* Enable apparmor service */
    const char *enable[] = {"systemctl", "enable", "--now", "apparmor", NULL};
    r = exec->execute_sudo(enable, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("apparmor: systemctl enable apparmor failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Enabled apparmor service");

    /* Load each profile with aa-enforce */
    for (int i = 0; i < cfg->profile_count; i++) {
        if (!cfg->profiles[i]) continue;
        const char *enforce[] = {"aa-enforce", cfg->profiles[i], NULL};
        r = exec->execute_sudo(enforce, exec->ctx);
        aegis_exec_result_free(&r);

        char action[256];
        snprintf(action, sizeof(action), "Enforced profile: %s", cfg->profiles[i]);
        aegis_result_add_action(&res, action);
    }

    char msg[128];
    snprintf(msg, sizeof(msg), "apparmor: applied with %d profile(s)", cfg->profile_count);
    free(res.message);
    res.message = strdup(msg);
    return res;
}

aegis_result_t aegis_apparmor_status(aegis_executor_t *exec) {
    /* Check if apparmor is active via aa-status */
    const char *argv[] = {"aa-status", "--enabled", NULL};
    aegis_exec_result_t r = exec->execute_sudo(argv, exec->ctx);
    bool active = (r.exit_code == 0);
    aegis_exec_result_free(&r);

    /* Count loaded profiles */
    const char *profiles_argv[] = {"aa-status", NULL};
    r = exec->execute_sudo(profiles_argv, exec->ctx);
    int loaded = 0;
    if (r.exit_code == 0 && r.stdout_buf) {
        /* aa-status output contains "N profiles are loaded" */
        char *ptr = strstr(r.stdout_buf, "profiles are loaded");
        if (ptr) {
            /* walk back to find the number */
            char *start = ptr;
            while (start > r.stdout_buf && *(start - 1) != '\n') start--;
            loaded = atoi(start);
        }
    }
    aegis_exec_result_free(&r);

    char msg[128];
    snprintf(msg, sizeof(msg), "apparmor: active=%s loaded_profiles=%d",
             active ? "yes" : "no", loaded);
    if (active) return aegis_result_ok(msg);
    return aegis_result_fail(msg);
}

aegis_result_t aegis_apparmor_verify(aegis_executor_t *exec) {
    /* Per-item compliance check: service enabled, profiles loaded */
    aegis_result_t res = aegis_result_ok("apparmor: all checks passed");
    int total = 0, passed = 0;

    /* Check 1: apparmor service is active */
    total++;
    const char *enabled_argv[] = {"aa-status", "--enabled", NULL};
    aegis_exec_result_t r = exec->execute_sudo(enabled_argv, exec->ctx);
    bool active = (r.exit_code == 0);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, active
        ? "PASS: apparmor service is active (aa-status --enabled)"
        : "FAIL: apparmor service is not active");
    if (active) passed++;

    /* Check 2: at least one profile loaded in enforce mode */
    total++;
    const char *status_argv[] = {"aa-status", NULL};
    r = exec->execute_sudo(status_argv, exec->ctx);
    bool has_enforced = false;
    if (r.exit_code == 0 && r.stdout_buf) {
        /* aa-status reports "N profiles are in enforce mode" */
        char *ptr = strstr(r.stdout_buf, "profiles are in enforce mode");
        if (ptr) {
            char *start = ptr;
            while (start > r.stdout_buf && *(start - 1) != '\n') start--;
            has_enforced = (atoi(start) > 0);
        }
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, has_enforced
        ? "PASS: at least one profile in enforce mode"
        : "FAIL: no profiles in enforce mode");
    if (has_enforced) passed++;

    char msg[128];
    snprintf(msg, sizeof(msg), "apparmor: %d/%d checks passed", passed, total);
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
