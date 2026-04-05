#ifndef AEGIS_MODULE_FIREJAIL_H
#define AEGIS_MODULE_FIREJAIL_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_firejail_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_firejail_status(aegis_executor_t *exec);
aegis_result_t aegis_firejail_verify(aegis_executor_t *exec);

#endif
