#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/audit.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *rules_minimal =
    "# aegis audit rules — minimal\n"
    "-w /etc/passwd -p wa -k identity\n"
    "-w /etc/shadow -p wa -k identity\n"
    "-w /etc/group -p wa -k identity\n";

static const char *rules_standard =
    "# aegis audit rules — standard\n"
    "-w /etc/passwd -p wa -k identity\n"
    "-w /etc/shadow -p wa -k identity\n"
    "-w /etc/group -p wa -k identity\n"
    "-w /etc/sudoers -p wa -k sudo_changes\n"
    "-w /etc/sudoers.d/ -p wa -k sudo_changes\n"
    "-a always,exit -F arch=b64 -S execve -k exec\n"
    "-w /var/log/faillog -p wa -k logins\n"
    "-w /var/log/lastlog -p wa -k logins\n";

static const char *rules_stig =
    "# aegis audit rules — stig\n"
    "-w /etc/passwd -p wa -k identity\n"
    "-w /etc/shadow -p wa -k identity\n"
    "-w /etc/group -p wa -k identity\n"
    "-w /etc/gshadow -p wa -k identity\n"
    "-w /etc/sudoers -p wa -k sudo_changes\n"
    "-w /etc/sudoers.d/ -p wa -k sudo_changes\n"
    "-a always,exit -F arch=b64 -S execve -k exec\n"
    "-a always,exit -F arch=b64 -S adjtimex,settimeofday -k time_change\n"
    "-a always,exit -F arch=b64 -S sethostname,setdomainname -k system_locale\n"
    "-w /var/log/faillog -p wa -k logins\n"
    "-w /var/log/lastlog -p wa -k logins\n"
    "-w /sbin/insmod -p x -k modules\n"
    "-w /sbin/rmmod -p x -k modules\n"
    "-w /sbin/modprobe -p x -k modules\n"
    "-a always,exit -F arch=b64 -S init_module,delete_module -k modules\n"
    "-e 2\n";

static const char *get_rules(const char *profile) {
    if (profile && strcmp(profile, "minimal") == 0) return rules_minimal;
    if (profile && strcmp(profile, "stig") == 0)    return rules_stig;
    return rules_standard;
}

aegis_result_t aegis_audit_apply(const void *config, aegis_executor_t *exec) {
    const aegis_audit_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("audit: disabled");

    aegis_result_t res = aegis_result_ok("audit: applied");

    /* Install auditd */
    const char *install[] = {"pacman", "-S", "--noconfirm", "--needed", "audit", NULL};
    aegis_exec_result_t r = exec->execute_sudo(install, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Ensured audit package installed");

    /* Write rules */
    const char *rules = get_rules(cfg->profile);
    exec->write_file("/etc/audit/rules.d/aegis.rules", rules, true, exec->ctx);
    aegis_result_add_action(&res, "Wrote /etc/audit/rules.d/aegis.rules");

    /* Load rules */
    const char *load[] = {"augenrules", "--load", NULL};
    r = exec->execute_sudo(load, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Loaded audit rules");

    /* Enable service */
    const char *enable[] = {"systemctl", "enable", "--now", "auditd", NULL};
    r = exec->execute_sudo(enable, exec->ctx);
    aegis_exec_result_free(&r);
    aegis_result_add_action(&res, "Enabled auditd service");

    char msg[128];
    snprintf(msg, sizeof(msg), "audit: applied %s profile", cfg->profile ? cfg->profile : "standard");
    free(res.message);
    res.message = strdup(msg);
    return res;
}

aegis_result_t aegis_audit_status(aegis_executor_t *exec) {
    const char *argv[] = {"systemctl", "is-active", "auditd", NULL};
    aegis_exec_result_t r = exec->execute(argv, exec->ctx);
    bool active = (r.exit_code == 0);
    aegis_exec_result_free(&r);

    bool rules_exist = exec->file_exists("/etc/audit/rules.d/aegis.rules", exec->ctx);

    char msg[128];
    snprintf(msg, sizeof(msg), "audit: service=%s rules=%s",
             active ? "active" : "inactive", rules_exist ? "present" : "missing");
    if (active && rules_exist) return aegis_result_ok(msg);
    if (active || rules_exist) return aegis_result_warn(msg);
    return aegis_result_fail(msg);
}

aegis_result_t aegis_audit_verify(aegis_executor_t *exec) {
    return aegis_audit_status(exec);
}
