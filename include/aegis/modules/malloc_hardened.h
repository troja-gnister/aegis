#ifndef AEGIS_MODULE_MALLOC_HARDENED_H
#define AEGIS_MODULE_MALLOC_HARDENED_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_malloc_hardened_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_malloc_hardened_status(aegis_executor_t *exec);
aegis_result_t aegis_malloc_hardened_verify(aegis_executor_t *exec);

#endif
