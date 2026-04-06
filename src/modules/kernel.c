#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/kernel.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

aegis_result_t aegis_kernel_apply(const void *config, aegis_executor_t *exec) {
    const aegis_kernel_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("kernel: disabled");

    aegis_result_t res = aegis_result_ok("kernel: applied");

    /* Install linux-hardened */
    const char *install[] = {"pacman", "-S", "--noconfirm", "--needed", "linux-hardened", NULL};
    aegis_exec_result_t r = exec->execute_sudo(install, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("kernel: pacman -S linux-hardened failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured linux-hardened installed");

    /* Set kernel lockdown mode in GRUB */
    char param[128];
    snprintf(param, sizeof(param),
             "GRUB_CMDLINE_LINUX_DEFAULT=\"loglevel=3 quiet lockdown=%s\"",
             cfg->lockdown ? cfg->lockdown : "integrity");

    /* Read current grub config, replace or append lockdown param */
    char *grub = exec->read_file("/etc/default/grub", exec->ctx);
    size_t grub_len  = grub ? strlen(grub) : 0;
    size_t param_len = strlen(param);
    /* Allocate enough for grub + param + separators + NUL */
    size_t new_cap = grub_len + param_len + 4;
    char *new_grub = malloc(new_cap);
    if (!new_grub) {
        free(grub);
        aegis_result_free(&res);
        return aegis_result_fail("kernel: malloc failed");
    }
    new_grub[0] = '\0';
    if (grub) {
        /* Simple approach: replace the GRUB_CMDLINE_LINUX_DEFAULT line */
        char *line = strstr(grub, "GRUB_CMDLINE_LINUX_DEFAULT");
        if (line) {
            size_t pre_len = (size_t)(line - grub);
            memcpy(new_grub, grub, pre_len);
            new_grub[pre_len] = '\0';
            strcat(new_grub, param);
            strcat(new_grub, "\n");
            char *next_line = strchr(line, '\n');
            if (next_line) strcat(new_grub, next_line + 1);
        } else {
            snprintf(new_grub, new_cap, "%s\n%s\n", grub, param);
        }
        free(grub);
    } else {
        snprintf(new_grub, new_cap, "%s\n", param);
    }

    if (exec->write_file("/etc/default/grub", new_grub, true, exec->ctx) != 0) {
        free(new_grub);
        aegis_result_free(&res);
        return aegis_result_fail("kernel: failed to write /etc/default/grub");
    }
    free(new_grub);
    aegis_result_add_action(&res, "Set kernel lockdown in GRUB config");

    /* Regenerate GRUB config */
    const char *mkconfig[] = {"grub-mkconfig", "-o", "/boot/grub/grub.cfg", NULL};
    r = exec->execute_sudo(mkconfig, exec->ctx);
    if (r.exit_code != 0) {
        aegis_result_free(&res);
        aegis_exec_result_free(&r);
        return aegis_result_fail("kernel: grub-mkconfig failed");
    }
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Regenerated GRUB config");

    char msg[128];
    snprintf(msg, sizeof(msg), "kernel: linux-hardened with lockdown=%s",
             cfg->lockdown ? cfg->lockdown : "integrity");
    free(res.message);
    res.message = strdup(msg);
    return res;
}

aegis_result_t aegis_kernel_status(aegis_executor_t *exec) {
    /* Check running kernel */
    const char *uname[] = {"uname", "-r", NULL};
    aegis_exec_result_t r = exec->execute(uname, exec->ctx);
    bool hardened = (r.stdout_buf && strstr(r.stdout_buf, "hardened"));
    aegis_exec_result_free(&r);

    /* Check lockdown mode */
    char *lockdown = exec->read_file("/sys/kernel/security/lockdown", exec->ctx);
    bool locked = (lockdown && (strstr(lockdown, "[integrity]") || strstr(lockdown, "[confidentiality]")));
    free(lockdown);

    char msg[128];
    snprintf(msg, sizeof(msg), "kernel: hardened=%s lockdown=%s",
             hardened ? "yes" : "no", locked ? "active" : "none");
    if (hardened && locked) return aegis_result_ok(msg);
    if (hardened || locked) return aegis_result_warn(msg);
    return aegis_result_fail(msg);
}

aegis_result_t aegis_kernel_verify(aegis_executor_t *exec) {
    return aegis_kernel_status(exec);
}
