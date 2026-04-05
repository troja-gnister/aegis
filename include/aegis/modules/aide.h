#ifndef AEGIS_MODULE_AIDE_H
#define AEGIS_MODULE_AIDE_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_aide_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_aide_status(aegis_executor_t *exec);
aegis_result_t aegis_aide_verify(aegis_executor_t *exec);

#endif
