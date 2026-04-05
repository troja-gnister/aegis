#ifndef AEGIS_MODULE_RKHUNTER_H
#define AEGIS_MODULE_RKHUNTER_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_rkhunter_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_rkhunter_status(aegis_executor_t *exec);
aegis_result_t aegis_rkhunter_verify(aegis_executor_t *exec);

#endif
