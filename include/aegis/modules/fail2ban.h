#ifndef AEGIS_MODULE_FAIL2BAN_H
#define AEGIS_MODULE_FAIL2BAN_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_fail2ban_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_fail2ban_status(aegis_executor_t *exec);
aegis_result_t aegis_fail2ban_verify(aegis_executor_t *exec);

#endif
