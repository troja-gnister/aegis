#define _POSIX_C_SOURCE 200809L
#include <aegis/config.h>
#include <aegis/config_helpers.h>
#include <tomlc99/toml.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

static void parse_sysctl(toml_table_t *tbl, aegis_sysctl_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->profile = cfg_get_string(tbl, "profile", "standard");
}
static void parse_mounts(toml_table_t *tbl, aegis_mounts_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->harden_tmp = cfg_get_bool(tbl, "harden_tmp", true);
    c->harden_dev_shm = cfg_get_bool(tbl, "harden_dev_shm", true);
    c->harden_proc = cfg_get_bool(tbl, "harden_proc", true);
}
static void parse_audit(toml_table_t *tbl, aegis_audit_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->profile = cfg_get_string(tbl, "profile", "standard");
}
static void parse_kernel(toml_table_t *tbl, aegis_kernel_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->lockdown = cfg_get_string(tbl, "lockdown", "integrity");
}
static void parse_apparmor(toml_table_t *tbl, aegis_apparmor_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->profiles = cfg_get_string_array(tbl, "profiles", &c->profile_count);
}
static void parse_firejail(toml_table_t *tbl, aegis_firejail_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->apps = cfg_get_string_array(tbl, "apps", &c->app_count);
    c->aggressive = cfg_get_bool(tbl, "aggressive", false);
}
static void parse_systemd_hardening(toml_table_t *tbl, aegis_systemd_hardening_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->auto_discover = cfg_get_bool(tbl, "auto_discover", false);
    c->profiles = cfg_get_string_array(tbl, "profiles", &c->profile_count);
}
static void parse_usbguard(toml_table_t *tbl, aegis_usbguard_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->default_policy = cfg_get_string(tbl, "default_policy", "block");
}
static void parse_snapper(toml_table_t *tbl, aegis_snapper_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->subvolumes = cfg_get_string_array(tbl, "subvolumes", &c->subvolume_count);
}
static void parse_flatpak_hardening(toml_table_t *tbl, aegis_flatpak_hardening_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->policy = cfg_get_string(tbl, "policy", "strict");
}
static void parse_dns(toml_table_t *tbl, aegis_dns_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->dnssec = cfg_get_bool(tbl, "dnssec", true);
    c->dot = cfg_get_bool(tbl, "dot", true);
}
static void parse_fail2ban(toml_table_t *tbl, aegis_fail2ban_config_t *c) {
    c->enabled = cfg_get_bool(tbl, "enabled", false);
    c->profile = cfg_get_string(tbl, "profile", "standard");
}
static void parse_bool_only(toml_table_t *tbl, bool *enabled) {
    *enabled = cfg_get_bool(tbl, "enabled", false);
}

aegis_config_t *aegis_config_load(const char *path, char *err, size_t errlen) {
    FILE *fp = fopen(path, "r");
    if (!fp) { snprintf(err, errlen, "Cannot open config file: %s", path); return NULL; }
    toml_table_t *root = toml_parse_file(fp, err, errlen);
    fclose(fp);
    if (!root) return NULL;
    aegis_config_t *cfg = calloc(1, sizeof(aegis_config_t));
    parse_sysctl(toml_table_in(root, "sysctl"), &cfg->sysctl);
    parse_mounts(toml_table_in(root, "mounts"), &cfg->mounts);
    parse_audit(toml_table_in(root, "audit"), &cfg->audit);
    parse_kernel(toml_table_in(root, "kernel"), &cfg->kernel);
    parse_apparmor(toml_table_in(root, "apparmor"), &cfg->apparmor);
    parse_firejail(toml_table_in(root, "firejail"), &cfg->firejail);
    parse_systemd_hardening(toml_table_in(root, "systemd_hardening"), &cfg->systemd_hardening);
    parse_usbguard(toml_table_in(root, "usbguard"), &cfg->usbguard);
    parse_snapper(toml_table_in(root, "snapper"), &cfg->snapper);
    parse_bool_only(toml_table_in(root, "secureboot"), &cfg->secureboot.enabled);
    parse_bool_only(toml_table_in(root, "malloc"), &cfg->malloc_hardened.enabled);
    parse_flatpak_hardening(toml_table_in(root, "flatpak_hardening"), &cfg->flatpak_hardening);
    parse_dns(toml_table_in(root, "dns"), &cfg->dns);
    parse_bool_only(toml_table_in(root, "podman_rootless"), &cfg->podman_rootless.enabled);
    parse_bool_only(toml_table_in(root, "dropbear"), &cfg->dropbear.enabled);
    parse_bool_only(toml_table_in(root, "archaudit"), &cfg->archaudit.enabled);
    parse_bool_only(toml_table_in(root, "aide"), &cfg->aide.enabled);
    parse_bool_only(toml_table_in(root, "rkhunter"), &cfg->rkhunter.enabled);
    parse_fail2ban(toml_table_in(root, "fail2ban"), &cfg->fail2ban);
    toml_free(root);
    return cfg;
}

void aegis_config_free(aegis_config_t *cfg) {
    if (!cfg) return;
    free(cfg->sysctl.profile);
    free(cfg->audit.profile);
    free(cfg->kernel.lockdown);
    cfg_free_string_array(cfg->apparmor.profiles, cfg->apparmor.profile_count);
    cfg_free_string_array(cfg->firejail.apps, cfg->firejail.app_count);
    cfg_free_string_array(cfg->systemd_hardening.profiles, cfg->systemd_hardening.profile_count);
    free(cfg->usbguard.default_policy);
    cfg_free_string_array(cfg->snapper.subvolumes, cfg->snapper.subvolume_count);
    free(cfg->flatpak_hardening.policy);
    free(cfg->fail2ban.profile);
    free(cfg);
}
