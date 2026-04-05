#ifndef AEGIS_MODULE_DNS_H
#define AEGIS_MODULE_DNS_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_dns_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_dns_status(aegis_executor_t *exec);
aegis_result_t aegis_dns_verify(aegis_executor_t *exec);

#endif
