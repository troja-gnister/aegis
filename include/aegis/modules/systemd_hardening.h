#ifndef AEGIS_MODULE_SYSTEMD_HARDENING_H
#define AEGIS_MODULE_SYSTEMD_HARDENING_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_systemd_hardening_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_systemd_hardening_status(aegis_executor_t *exec);
aegis_result_t aegis_systemd_hardening_verify(aegis_executor_t *exec);

#endif
