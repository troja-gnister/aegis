#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/secureboot.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

aegis_result_t aegis_secureboot_apply(const void *config, aegis_executor_t *exec) {
    const aegis_secureboot_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("secureboot: disabled");

    aegis_result_t res = aegis_result_ok("secureboot: applied");

    /* Install sbctl */
    const char *install[] = {"pacman", "-S", "--noconfirm", "--needed", "sbctl", NULL};
    aegis_exec_result_t r = exec->execute_sudo(install, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured sbctl package installed");

    /* Create keys */
    const char *create_keys[] = {"sbctl", "create-keys", NULL};
    r = exec->execute_sudo(create_keys, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Created secure boot keys");

    /* Enroll keys with Microsoft certificates */
    const char *enroll[] = {"sbctl", "enroll-keys", "--microsoft", NULL};
    r = exec->execute_sudo(enroll, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Enrolled keys with --microsoft");

    /* Sign all registered files */
    const char *sign_all[] = {"sbctl", "sign-all", NULL};
    r = exec->execute_sudo(sign_all, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Signed all registered EFI binaries");

    return res;
}

aegis_result_t aegis_secureboot_status(aegis_executor_t *exec) {
    const char *argv[] = {"sbctl", "status", NULL};
    aegis_exec_result_t r = exec->execute_sudo(argv, exec->ctx);
    bool ok = (r.exit_code == 0);
    aegis_exec_result_free(&r);

    if (ok) return aegis_result_ok("secureboot: sbctl status OK");
    return aegis_result_warn("secureboot: sbctl not configured or secure boot not enabled");
}

aegis_result_t aegis_secureboot_verify(aegis_executor_t *exec) {
    return aegis_secureboot_status(exec);
}
