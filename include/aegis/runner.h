#ifndef AEGIS_RUNNER_H
#define AEGIS_RUNNER_H

#include <aegis/module.h>
#include <aegis/config.h>

aegis_result_t *aegis_run_all(aegis_module_t *modules, int module_count,
                               const aegis_config_t *cfg, aegis_executor_t *exec,
                               int *out_count);
aegis_result_t *aegis_run_selected(aegis_module_t *modules, int module_count,
                                    const aegis_config_t *cfg, aegis_executor_t *exec,
                                    const char **selected, int selected_count,
                                    int *out_count);
aegis_result_t *aegis_status_all(aegis_module_t *modules, int module_count,
                                  aegis_executor_t *exec, int *out_count);
aegis_result_t *aegis_status_selected(aegis_module_t *modules, int module_count,
                                       aegis_executor_t *exec,
                                       const char **selected, int selected_count,
                                       int *out_count);
aegis_result_t *aegis_verify_all(aegis_module_t *modules, int module_count,
                                  aegis_executor_t *exec, int *out_count);
aegis_result_t *aegis_verify_selected(aegis_module_t *modules, int module_count,
                                       aegis_executor_t *exec,
                                       const char **selected, int selected_count,
                                       int *out_count);
aegis_module_t *aegis_get_modules(int *out_count);

#endif
