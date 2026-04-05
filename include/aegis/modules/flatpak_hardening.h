#ifndef AEGIS_MODULE_FLATPAK_HARDENING_H
#define AEGIS_MODULE_FLATPAK_HARDENING_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_flatpak_hardening_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_flatpak_hardening_status(aegis_executor_t *exec);
aegis_result_t aegis_flatpak_hardening_verify(aegis_executor_t *exec);

#endif
