#ifndef AEGIS_MODULE_PODMAN_ROOTLESS_H
#define AEGIS_MODULE_PODMAN_ROOTLESS_H

#include <aegis/result.h>
#include <aegis/executor.h>
#include <aegis/config.h>

aegis_result_t aegis_podman_rootless_apply(const void *config, aegis_executor_t *exec);
aegis_result_t aegis_podman_rootless_status(aegis_executor_t *exec);
aegis_result_t aegis_podman_rootless_verify(aegis_executor_t *exec);

#endif
