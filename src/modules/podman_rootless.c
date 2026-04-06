#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/podman_rootless.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SUBUID_PATH "/etc/subuid"
#define SUBGID_PATH "/etc/subgid"
#define SUBID_ENTRY "containers:100000:65536\n"

aegis_result_t aegis_podman_rootless_apply(const void *config, aegis_executor_t *exec) {
    const aegis_podman_rootless_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("podman_rootless: disabled");

    aegis_result_t res = aegis_result_ok("podman_rootless: applied");

    /* Enable unprivileged user namespaces via sysctl */
    const char *sysctl_argv[] = {"sysctl", "-w",
                                  "kernel.unprivileged_userns_clone=1", NULL};
    aegis_exec_result_t r = exec->execute_sudo(sysctl_argv, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("podman_rootless: sysctl -w failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Set kernel.unprivileged_userns_clone=1");

    /* Also persist via sysctl.d */
    if (exec->write_file("/etc/sysctl.d/99-podman.conf",
                         "kernel.unprivileged_userns_clone = 1\n", true, exec->ctx) != 0) {
        aegis_result_free(&res);
        return aegis_result_fail("podman_rootless: failed to write /etc/sysctl.d/99-podman.conf");
    }
    aegis_result_add_action(&res, "Wrote /etc/sysctl.d/99-podman.conf");

    /* Configure /etc/subuid */
    if (exec->write_file(SUBUID_PATH, SUBID_ENTRY, true, exec->ctx) != 0) {
        aegis_result_free(&res);
        return aegis_result_fail("podman_rootless: failed to write " SUBUID_PATH);
    }
    aegis_result_add_action(&res, "Configured " SUBUID_PATH " for user namespaces");

    /* Configure /etc/subgid */
    if (exec->write_file(SUBGID_PATH, SUBID_ENTRY, true, exec->ctx) != 0) {
        aegis_result_free(&res);
        return aegis_result_fail("podman_rootless: failed to write " SUBGID_PATH);
    }
    aegis_result_add_action(&res, "Configured " SUBGID_PATH " for user namespaces");

    return res;
}

aegis_result_t aegis_podman_rootless_status(aegis_executor_t *exec) {
    const char *argv[] = {"sysctl", "kernel.unprivileged_userns_clone", NULL};
    aegis_exec_result_t r = exec->execute(argv, exec->ctx);
    bool userns_ok = false;
    if (r.exit_code == 0 && r.stdout_buf) {
        char *eq = strstr(r.stdout_buf, "= ");
        if (eq && strncmp(eq + 2, "1", 1) == 0) userns_ok = true;
    }
    aegis_exec_result_free(&r);

    bool subuid_exists = exec->file_exists(SUBUID_PATH, exec->ctx);
    bool subgid_exists = exec->file_exists(SUBGID_PATH, exec->ctx);

    char msg[128];
    snprintf(msg, sizeof(msg), "podman_rootless: userns=%s subuid=%s subgid=%s",
             userns_ok ? "enabled" : "disabled",
             subuid_exists ? "present" : "missing",
             subgid_exists ? "present" : "missing");
    if (userns_ok && subuid_exists && subgid_exists) return aegis_result_ok(msg);
    return aegis_result_warn(msg);
}

aegis_result_t aegis_podman_rootless_verify(aegis_executor_t *exec) {
    /* Per-item compliance: userns enabled, subuid/subgid configured */
    aegis_result_t res = aegis_result_ok("podman_rootless: all checks passed");
    int total = 0, passed = 0;

    /* Check 1: kernel.unprivileged_userns_clone = 1 */
    total++;
    const char *sysctl_argv[] = {"sysctl", "kernel.unprivileged_userns_clone", NULL};
    aegis_exec_result_t r = exec->execute(sysctl_argv, exec->ctx);
    bool userns_ok = false;
    if (r.exit_code == 0 && r.stdout_buf) {
        char *eq = strstr(r.stdout_buf, "= ");
        if (eq && strncmp(eq + 2, "1", 1) == 0) userns_ok = true;
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, userns_ok
        ? "PASS: kernel.unprivileged_userns_clone = 1"
        : "FAIL: kernel.unprivileged_userns_clone != 1 (expected 1)");
    if (userns_ok) passed++;

    /* Check 2: /etc/subuid exists and contains containers entry */
    total++;
    char *subuid = exec->read_file(SUBUID_PATH, exec->ctx);
    bool subuid_ok = (subuid && strstr(subuid, "containers") != NULL);
    free(subuid);
    aegis_result_add_action(&res, subuid_ok
        ? "PASS: " SUBUID_PATH " contains containers entry"
        : "FAIL: " SUBUID_PATH " missing or no containers entry");
    if (subuid_ok) passed++;

    /* Check 3: /etc/subgid exists and contains containers entry */
    total++;
    char *subgid = exec->read_file(SUBGID_PATH, exec->ctx);
    bool subgid_ok = (subgid && strstr(subgid, "containers") != NULL);
    free(subgid);
    aegis_result_add_action(&res, subgid_ok
        ? "PASS: " SUBGID_PATH " contains containers entry"
        : "FAIL: " SUBGID_PATH " missing or no containers entry");
    if (subgid_ok) passed++;

    char msg[128];
    snprintf(msg, sizeof(msg), "podman_rootless: %d/%d checks passed", passed, total);
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
