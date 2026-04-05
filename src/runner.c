#include <aegis/runner.h>
#include <stdlib.h>
#include <string.h>

static int cmp_priority(const void *a, const void *b) {
    return ((const aegis_module_t *)a)->priority - ((const aegis_module_t *)b)->priority;
}

static const void *config_for_module(const aegis_config_t *cfg, const char *name) {
    if (strcmp(name, "sysctl") == 0) return &cfg->sysctl;
    if (strcmp(name, "mounts") == 0) return &cfg->mounts;
    if (strcmp(name, "audit") == 0) return &cfg->audit;
    if (strcmp(name, "kernel") == 0) return &cfg->kernel;
    if (strcmp(name, "apparmor") == 0) return &cfg->apparmor;
    if (strcmp(name, "firejail") == 0) return &cfg->firejail;
    if (strcmp(name, "systemd_hardening") == 0) return &cfg->systemd_hardening;
    if (strcmp(name, "usbguard") == 0) return &cfg->usbguard;
    if (strcmp(name, "snapper") == 0) return &cfg->snapper;
    if (strcmp(name, "secureboot") == 0) return (const void *)&cfg->secureboot;
    if (strcmp(name, "malloc") == 0) return (const void *)&cfg->malloc_hardened;
    if (strcmp(name, "flatpak_hardening") == 0) return &cfg->flatpak_hardening;
    if (strcmp(name, "dns") == 0) return &cfg->dns;
    if (strcmp(name, "podman_rootless") == 0) return (const void *)&cfg->podman_rootless;
    if (strcmp(name, "dropbear") == 0) return (const void *)&cfg->dropbear;
    if (strcmp(name, "archaudit") == 0) return (const void *)&cfg->archaudit;
    if (strcmp(name, "aide") == 0) return (const void *)&cfg->aide;
    if (strcmp(name, "rkhunter") == 0) return (const void *)&cfg->rkhunter;
    if (strcmp(name, "fail2ban") == 0) return &cfg->fail2ban;
    return NULL;
}

static bool is_selected(const char *name, const char **selected, int count) {
    for (int i = 0; i < count; i++) {
        if (strcmp(name, selected[i]) == 0) return true;
    }
    return false;
}

aegis_result_t *aegis_run_all(aegis_module_t *modules, int module_count,
                               const aegis_config_t *cfg, aegis_executor_t *exec,
                               int *out_count) {
    aegis_module_t *sorted = malloc((size_t)module_count * sizeof(aegis_module_t));
    memcpy(sorted, modules, (size_t)module_count * sizeof(aegis_module_t));
    qsort(sorted, (size_t)module_count, sizeof(aegis_module_t), cmp_priority);
    aegis_result_t *results = malloc((size_t)module_count * sizeof(aegis_result_t));
    *out_count = module_count;
    for (int i = 0; i < module_count; i++) {
        const void *mcfg = config_for_module(cfg, sorted[i].name);
        results[i] = sorted[i].apply(mcfg, exec);
    }
    free(sorted);
    return results;
}

aegis_result_t *aegis_run_selected(aegis_module_t *modules, int module_count,
                                    const aegis_config_t *cfg, aegis_executor_t *exec,
                                    const char **selected, int selected_count,
                                    int *out_count) {
    aegis_module_t *sorted = malloc((size_t)module_count * sizeof(aegis_module_t));
    memcpy(sorted, modules, (size_t)module_count * sizeof(aegis_module_t));
    qsort(sorted, (size_t)module_count, sizeof(aegis_module_t), cmp_priority);
    aegis_result_t *results = malloc((size_t)selected_count * sizeof(aegis_result_t));
    int count = 0;
    for (int i = 0; i < module_count; i++) {
        if (!is_selected(sorted[i].name, selected, selected_count)) continue;
        const void *mcfg = config_for_module(cfg, sorted[i].name);
        results[count++] = sorted[i].apply(mcfg, exec);
    }
    free(sorted);
    *out_count = count;
    return results;
}

aegis_result_t *aegis_status_all(aegis_module_t *modules, int module_count,
                                  aegis_executor_t *exec, int *out_count) {
    aegis_result_t *results = malloc((size_t)module_count * sizeof(aegis_result_t));
    *out_count = module_count;
    for (int i = 0; i < module_count; i++) results[i] = modules[i].status(exec);
    return results;
}

aegis_result_t *aegis_status_selected(aegis_module_t *modules, int module_count,
                                       aegis_executor_t *exec,
                                       const char **selected, int selected_count,
                                       int *out_count) {
    aegis_result_t *results = malloc((size_t)selected_count * sizeof(aegis_result_t));
    int count = 0;
    for (int i = 0; i < module_count; i++) {
        if (!is_selected(modules[i].name, selected, selected_count)) continue;
        results[count++] = modules[i].status(exec);
    }
    *out_count = count;
    return results;
}

aegis_result_t *aegis_verify_all(aegis_module_t *modules, int module_count,
                                  aegis_executor_t *exec, int *out_count) {
    aegis_result_t *results = malloc((size_t)module_count * sizeof(aegis_result_t));
    *out_count = module_count;
    for (int i = 0; i < module_count; i++) results[i] = modules[i].verify(exec);
    return results;
}

aegis_result_t *aegis_verify_selected(aegis_module_t *modules, int module_count,
                                       aegis_executor_t *exec,
                                       const char **selected, int selected_count,
                                       int *out_count) {
    aegis_result_t *results = malloc((size_t)selected_count * sizeof(aegis_result_t));
    int count = 0;
    for (int i = 0; i < module_count; i++) {
        if (!is_selected(modules[i].name, selected, selected_count)) continue;
        results[count++] = modules[i].verify(exec);
    }
    *out_count = count;
    return results;
}

/* Placeholder — populated as modules are implemented */
aegis_module_t *aegis_get_modules(int *out_count) {
    *out_count = 0;
    return NULL;
}
