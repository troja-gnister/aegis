#ifndef AEGIS_MODULE_H
#define AEGIS_MODULE_H

#include <aegis/result.h>
#include <aegis/executor.h>

typedef struct {
    const char     *name;
    int             priority;
    aegis_result_t (*apply)(const void *config, aegis_executor_t *exec);
    aegis_result_t (*status)(aegis_executor_t *exec);
    aegis_result_t (*verify)(aegis_executor_t *exec);
} aegis_module_t;

#endif
