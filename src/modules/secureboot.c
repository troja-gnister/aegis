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
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("secureboot: pacman -S sbctl failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured sbctl package installed");

    /* Create keys */
    const char *create_keys[] = {"sbctl", "create-keys", NULL};
    r = exec->execute_sudo(create_keys, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("secureboot: sbctl create-keys failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Created secure boot keys");

    /* Enroll keys with Microsoft certificates */
    const char *enroll[] = {"sbctl", "enroll-keys", "--microsoft", NULL};
    r = exec->execute_sudo(enroll, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("secureboot: sbctl enroll-keys failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Enrolled keys with --microsoft");

    /* Sign all registered files */
    const char *sign_all[] = {"sbctl", "sign-all", NULL};
    r = exec->execute_sudo(sign_all, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("secureboot: sbctl sign-all failed");
    }
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
    /* Per-item compliance: sbctl status, keys present, files signed */
    aegis_result_t res = aegis_result_ok("secureboot: all checks passed");
    int total = 0, passed = 0;

    /* Check 1: sbctl status succeeds */
    total++;
    const char *status_argv[] = {"sbctl", "status", NULL};
    aegis_exec_result_t r = exec->execute_sudo(status_argv, exec->ctx);
    bool status_ok = (r.exit_code == 0);
    bool setup_mode = (r.stdout_buf && strstr(r.stdout_buf, "Installed"));
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, status_ok
        ? "PASS: sbctl status returned successfully"
        : "FAIL: sbctl status failed — sbctl not configured or secure boot not enabled");
    if (status_ok) passed++;

    /* Check 2: sbctl keys are installed */
    total++;
    aegis_result_add_action(&res, setup_mode
        ? "PASS: sbctl reports keys installed"
        : "FAIL: sbctl keys not installed");
    if (setup_mode) passed++;

    /* Check 3: verify all registered files are signed */
    total++;
    const char *verify_argv[] = {"sbctl", "verify", NULL};
    r = exec->execute_sudo(verify_argv, exec->ctx);
    bool signed_ok = (r.exit_code == 0);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, signed_ok
        ? "PASS: all registered EFI files are signed (sbctl verify)"
        : "FAIL: some EFI files not signed — run sbctl sign-all");
    if (signed_ok) passed++;

    char msg[128];
    snprintf(msg, sizeof(msg), "secureboot: %d/%d checks passed", passed, total);
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
