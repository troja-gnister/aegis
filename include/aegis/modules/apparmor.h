#ifndef AEGIS_MODULE_APPARMOR_H
#define AEGIS_MODULE_APPARMOR_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_apparmor_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_apparmor_status(aegis_executor_t *exec);
aegis_result_t aegis_apparmor_verify(aegis_executor_t *exec);

#endif
