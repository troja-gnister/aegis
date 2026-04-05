#ifndef AEGIS_MODULE_SECUREBOOT_H
#define AEGIS_MODULE_SECUREBOOT_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_secureboot_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_secureboot_status(aegis_executor_t *exec);
aegis_result_t aegis_secureboot_verify(aegis_executor_t *exec);

#endif
