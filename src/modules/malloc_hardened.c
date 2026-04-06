#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/malloc_hardened.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define PRELOAD_PATH "/etc/ld.so.preload"
#define PRELOAD_CONTENT "libhardened_malloc.so\n"

aegis_result_t aegis_malloc_hardened_apply(const void *config, aegis_executor_t *exec) {
    const aegis_malloc_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("malloc_hardened: disabled");

    aegis_result_t res = aegis_result_ok("malloc_hardened: applied");

    /* Install hardened_malloc */
    const char *install[] = {"pacman", "-S", "--noconfirm", "--needed", "hardened_malloc", NULL};
    aegis_exec_result_t r = exec->execute_sudo(install, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("malloc_hardened: pacman -S hardened_malloc failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured hardened_malloc package installed");

    /* Write /etc/ld.so.preload */
    if (exec->write_file(PRELOAD_PATH, PRELOAD_CONTENT, true, exec->ctx) != 0) {
        aegis_result_free(&res);
        return aegis_result_fail("malloc_hardened: failed to write " PRELOAD_PATH);
    }
    aegis_result_add_action(&res, "Wrote " PRELOAD_PATH " with libhardened_malloc.so");

    return res;
}

aegis_result_t aegis_malloc_hardened_status(aegis_executor_t *exec) {
    bool preload_exists = exec->file_exists(PRELOAD_PATH, exec->ctx);
    if (!preload_exists)
        return aegis_result_fail("malloc_hardened: " PRELOAD_PATH " missing");

    char *content = exec->read_file(PRELOAD_PATH, exec->ctx);
    bool configured = content && strstr(content, "libhardened_malloc.so") != NULL;
    free(content);

    if (configured) return aegis_result_ok("malloc_hardened: libhardened_malloc.so preloaded");
    return aegis_result_warn("malloc_hardened: " PRELOAD_PATH " exists but libhardened_malloc.so not listed");
}

aegis_result_t aegis_malloc_hardened_verify(aegis_executor_t *exec) {
    /* Per-item compliance: ld.so.preload exists and contains libhardened_malloc.so */
    aegis_result_t res = aegis_result_ok("malloc_hardened: all checks passed");
    int total = 0, passed = 0;

    /* Check 1: preload file exists */
    total++;
    bool preload_exists = exec->file_exists(PRELOAD_PATH, exec->ctx);
    aegis_result_add_action(&res, preload_exists
        ? "PASS: " PRELOAD_PATH " present"
        : "FAIL: " PRELOAD_PATH " missing — hardened_malloc not configured");
    if (preload_exists) passed++;

    /* Check 2: libhardened_malloc.so listed in preload file */
    total++;
    char *content = preload_exists ? exec->read_file(PRELOAD_PATH, exec->ctx) : NULL;
    bool configured = (content && strstr(content, "libhardened_malloc.so") != NULL);
    free(content);
    aegis_result_add_action(&res, configured
        ? "PASS: libhardened_malloc.so listed in " PRELOAD_PATH
        : "FAIL: libhardened_malloc.so not found in " PRELOAD_PATH);
    if (configured) passed++;

    char msg[128];
    snprintf(msg, sizeof(msg), "malloc_hardened: %d/%d checks passed", passed, total);
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
