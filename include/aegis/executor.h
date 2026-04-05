#ifndef AEGIS_EXECUTOR_H
#define AEGIS_EXECUTOR_H

#include <stdbool.h>

typedef struct {
    int   exit_code;
    char *stdout_buf;
    char *stderr_buf;
} aegis_exec_result_t;

void aegis_exec_result_free(aegis_exec_result_t *r);

typedef struct aegis_executor {
    aegis_exec_result_t (*execute)(const char **argv, void *ctx);
    aegis_exec_result_t (*execute_sudo)(const char **argv, void *ctx);
    aegis_exec_result_t (*execute_shell)(const char *cmd, void *ctx);
    bool (*file_exists)(const char *path, void *ctx);
    char *(*read_file)(const char *path, void *ctx);
    int  (*write_file)(const char *path, const char *content, bool sudo, void *ctx);
    bool  dry_run;
    void *ctx;
} aegis_executor_t;

aegis_executor_t aegis_system_executor(bool dry_run);

#endif
