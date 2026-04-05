#ifndef AEGIS_MODULE_ARCHAUDIT_H
#define AEGIS_MODULE_ARCHAUDIT_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_archaudit_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_archaudit_status(aegis_executor_t *exec);
aegis_result_t aegis_archaudit_verify(aegis_executor_t *exec);

#endif
