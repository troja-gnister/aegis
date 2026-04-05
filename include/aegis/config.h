#ifndef AEGIS_CONFIG_H
#define AEGIS_CONFIG_H

#include <stdbool.h>
#include <stddef.h>

typedef struct { bool enabled; char *profile; }                                  aegis_sysctl_config_t;
typedef struct { bool enabled; bool harden_tmp; bool harden_dev_shm; bool harden_proc; } aegis_mounts_config_t;
typedef struct { bool enabled; char *profile; }                                  aegis_audit_config_t;
typedef struct { bool enabled; char *lockdown; }                                 aegis_kernel_config_t;
typedef struct { bool enabled; char **profiles; int profile_count; }             aegis_apparmor_config_t;
typedef struct { bool enabled; char **apps; int app_count; bool aggressive; }    aegis_firejail_config_t;
typedef struct { bool enabled; bool auto_discover; char **profiles; int profile_count; } aegis_systemd_hardening_config_t;
typedef struct { bool enabled; char *default_policy; }                           aegis_usbguard_config_t;
typedef struct { bool enabled; char **subvolumes; int subvolume_count; }         aegis_snapper_config_t;
typedef struct { bool enabled; }                                                 aegis_secureboot_config_t;
typedef struct { bool enabled; }                                                 aegis_malloc_config_t;
typedef struct { bool enabled; char *policy; }                                   aegis_flatpak_hardening_config_t;
typedef struct { bool enabled; bool dnssec; bool dot; }                          aegis_dns_config_t;
typedef struct { bool enabled; }                                                 aegis_podman_rootless_config_t;
typedef struct { bool enabled; }                                                 aegis_dropbear_config_t;
typedef struct { bool enabled; }                                                 aegis_archaudit_config_t;
typedef struct { bool enabled; }                                                 aegis_aide_config_t;
typedef struct { bool enabled; }                                                 aegis_rkhunter_config_t;
typedef struct { bool enabled; char *profile; }                                  aegis_fail2ban_config_t;

typedef struct {
    aegis_sysctl_config_t              sysctl;
    aegis_mounts_config_t              mounts;
    aegis_audit_config_t               audit;
    aegis_kernel_config_t              kernel;
    aegis_apparmor_config_t            apparmor;
    aegis_firejail_config_t            firejail;
    aegis_systemd_hardening_config_t   systemd_hardening;
    aegis_usbguard_config_t            usbguard;
    aegis_snapper_config_t             snapper;
    aegis_secureboot_config_t          secureboot;
    aegis_malloc_config_t              malloc_hardened;
    aegis_flatpak_hardening_config_t   flatpak_hardening;
    aegis_dns_config_t                 dns;
    aegis_podman_rootless_config_t     podman_rootless;
    aegis_dropbear_config_t            dropbear;
    aegis_archaudit_config_t           archaudit;
    aegis_aide_config_t                aide;
    aegis_rkhunter_config_t            rkhunter;
    aegis_fail2ban_config_t            fail2ban;
} aegis_config_t;

aegis_config_t *aegis_config_load(const char *path, char *err, size_t errlen);
void            aegis_config_free(aegis_config_t *cfg);

#endif
