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
    return aegis_malloc_hardened_status(exec);
}
