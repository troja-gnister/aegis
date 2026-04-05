#ifndef AEGIS_MODULE_SNAPPER_H
#define AEGIS_MODULE_SNAPPER_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_snapper_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_snapper_status(aegis_executor_t *exec);
aegis_result_t aegis_snapper_verify(aegis_executor_t *exec);

#endif
