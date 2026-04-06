#define _POSIX_C_SOURCE 200809L
#include <aegis/modules/flatpak_hardening.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define FLATPAK_CONV_DIR "/etc/aegis/rules.d/flatpak"

/* Apply per-app override files from the convention directory.
 * Each *.conf file is expected to specify a flatpak app-id on the first line
 * preceded by "# app:" and subsequent lines are --option flags.
 * Since we can't parse arbitrary INI here, we treat each file as containing
 * flatpak override flags for the app-id found in a comment header.
 *
 * Format of a convention file:
 *   # app: com.example.App
 *   --nofilesystem=home
 *   --nosocket=x11
 */
static void apply_convention_dir_flatpak(aegis_executor_t *exec, aegis_result_t *res) {
    const char *ls_argv[] = {"ls", FLATPAK_CONV_DIR, NULL};
    aegis_exec_result_t r = exec->execute(ls_argv, exec->ctx);
    if (r.exit_code != 0 || !r.stdout_buf || !r.stdout_buf[0]) {
        aegis_exec_result_free(&r);
        return;
    }

    char *listing = strdup(r.stdout_buf);
    aegis_exec_result_free(&r);
    if (!listing) return;

    char *tok = strtok(listing, "\n");
    while (tok) {
        size_t flen = strlen(tok);
        if (flen > 5 && strcmp(tok + flen - 5, ".conf") == 0) {
            char path[512];
            snprintf(path, sizeof(path), "%s/%s", FLATPAK_CONV_DIR, tok);
            char *content = exec->read_file(path, exec->ctx);
            if (content) {
                /* Look for "# app: <app-id>" header */
                const char *app_id = NULL;
                char *app_line = strstr(content, "# app:");
                if (app_line) {
                    app_line += 6;
                    while (*app_line == ' ') app_line++;
                    char *nl = strchr(app_line, '\n');
                    if (nl) *nl = '\0';
                    app_id = app_line;
                }
                if (app_id && app_id[0]) {
                    /* Build flatpak override --user <app-id> command with flags from file */
                    char cmd[1024];
                    snprintf(cmd, sizeof(cmd), "flatpak override --system %s", app_id);
                    /* Each non-comment line is a flag */
                    char *line = strchr(content, '\n');
                    while (line) {
                        line++;
                        if (*line && *line != '#' && *line != '\n') {
                            char *end = strchr(line, '\n');
                            size_t flag_len = end ? (size_t)(end - line) : strlen(line);
                            if (flag_len > 0 && strlen(cmd) + flag_len + 2 < sizeof(cmd)) {
                                strncat(cmd, " ", sizeof(cmd) - strlen(cmd) - 1);
                                strncat(cmd, line, flag_len);
                            }
                        }
                        line = strchr(line, '\n');
                    }
                    aegis_exec_result_t rc = exec->execute_shell(cmd, exec->ctx);
                    aegis_exec_result_free(&rc);
                    char action[1088];
                    snprintf(action, sizeof(action),
                             "Applied convention flatpak override for %s from %s", app_id, path);
                    aegis_result_add_action(res, action);
                }
                free(content);
            }
        }
        tok = strtok(NULL, "\n");
    }
    free(listing);
}

aegis_result_t aegis_flatpak_hardening_apply(const void *config, aegis_executor_t *exec) {
    const aegis_flatpak_hardening_config_t *cfg = config;
    if (!cfg || !cfg->enabled)
        return aegis_result_skip("flatpak_hardening: disabled");

    aegis_result_t res = aegis_result_ok("flatpak_hardening: applied");

    const char *policy = cfg->policy ? cfg->policy : "strict";

    /* strict: restrict filesystem home and ssh-auth socket */
    if (strcmp(policy, "strict") == 0 || strcmp(policy, "lockdown") == 0) {
        const char *strict_fs[] = {"flatpak", "override", "--system",
                                    "--nofilesystem=home", "--nosocket=ssh-auth", NULL};
        aegis_exec_result_t r = exec->execute_sudo(strict_fs, exec->ctx);
        aegis_exec_result_free(&r);
        aegis_result_add_action(&res, "Applied strict filesystem/socket restrictions");
    }

    /* lockdown: add additional restrictions */
    if (strcmp(policy, "lockdown") == 0) {
        const char *lockdown[] = {"flatpak", "override", "--system",
                                   "--nofilesystem=host",
                                   "--nosocket=x11",
                                   "--nosocket=wayland",
                                   "--no-talk-name=org.freedesktop.NetworkManager",
                                   NULL};
        aegis_exec_result_t r = exec->execute_sudo(lockdown, exec->ctx);
        aegis_exec_result_free(&r);
        aegis_result_add_action(&res, "Applied lockdown additional restrictions");
    }

    /* Apply per-app override files from convention directory */
    apply_convention_dir_flatpak(exec, &res);

    char msg[128];
    snprintf(msg, sizeof(msg), "flatpak_hardening: applied %s policy", policy);
    free(res.message);
    res.message = strdup(msg);
    return res;
}

aegis_result_t aegis_flatpak_hardening_status(aegis_executor_t *exec) {
    const char *argv[] = {"flatpak", "override", "--system", "--show", NULL};
    aegis_exec_result_t r = exec->execute(argv, exec->ctx);
    bool ok = (r.exit_code == 0);
    aegis_exec_result_free(&r);

    if (ok) return aegis_result_ok("flatpak_hardening: system overrides present");
    return aegis_result_warn("flatpak_hardening: no system overrides configured");
}

aegis_result_t aegis_flatpak_hardening_verify(aegis_executor_t *exec) {
    /* Per-item compliance: check system overrides for key restrictions */
    aegis_result_t res = aegis_result_ok("flatpak_hardening: all checks passed");
    int total = 0, passed = 0;

    const char *show_argv[] = {"flatpak", "override", "--system", "--show", NULL};
    aegis_exec_result_t r = exec->execute(show_argv, exec->ctx);
    bool ok = (r.exit_code == 0);
    const char *out = (ok && r.stdout_buf) ? r.stdout_buf : "";

    /* Check 1: overrides command succeeds */
    total++;
    aegis_result_add_action(&res, ok
        ? "PASS: flatpak system overrides are configured"
        : "FAIL: flatpak override --system --show failed — no overrides configured");
    if (ok) passed++;

    /* Check 2: nofilesystem=home restriction present */
    total++;
    bool no_home = (strstr(out, "nofilesystem=home") != NULL ||
                    strstr(out, "filesystems=!home") != NULL);
    aegis_result_add_action(&res, no_home
        ? "PASS: home filesystem access restricted"
        : "FAIL: home filesystem restriction not found in overrides");
    if (no_home) passed++;

    /* Check 3: ssh-auth socket restricted */
    total++;
    bool no_ssh = (strstr(out, "nosocket=ssh-auth") != NULL ||
                   strstr(out, "sockets=!ssh-auth") != NULL);
    aegis_result_add_action(&res, no_ssh
        ? "PASS: ssh-auth socket access restricted"
        : "FAIL: ssh-auth socket restriction not found in overrides");
    if (no_ssh) passed++;

    aegis_exec_result_free(&r);

    char msg[128];
    snprintf(msg, sizeof(msg), "flatpak_hardening: %d/%d checks passed", passed, total);
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
