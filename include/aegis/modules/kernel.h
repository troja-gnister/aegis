#ifndef AEGIS_MODULE_KERNEL_H
#define AEGIS_MODULE_KERNEL_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_kernel_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_kernel_status(aegis_executor_t *exec);
aegis_result_t aegis_kernel_verify(aegis_executor_t *exec);

#endif
