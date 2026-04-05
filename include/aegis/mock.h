#ifndef AEGIS_MOCK_H
#define AEGIS_MOCK_H

#include <aegis/executor.h>
#include <stdbool.h>

#define AEGIS_MOCK_MAX_RESPONSES 32
#define AEGIS_MOCK_MAX_FILES     64

typedef struct {
    char **calls;
    int    call_count;
    int    _call_cap;

    struct { char *path; char *content; } files[AEGIS_MOCK_MAX_FILES];
    int file_count;

    struct {
        char *cmd_prefix;
        int   exit_code;
        char *stdout_str;
    } responses[AEGIS_MOCK_MAX_RESPONSES];
    int response_count;
} aegis_mock_ctx_t;

aegis_executor_t aegis_mock_executor(aegis_mock_ctx_t *ctx);

void aegis_mock_add_response(aegis_mock_ctx_t *ctx, const char *cmd_prefix,
                              int exit_code, const char *stdout_str);
void aegis_mock_add_file(aegis_mock_ctx_t *ctx, const char *path, const char *content);

int         aegis_mock_call_count(const aegis_mock_ctx_t *ctx);
const char *aegis_mock_call_at(const aegis_mock_ctx_t *ctx, int index);
bool        aegis_mock_was_called(const aegis_mock_ctx_t *ctx, const char *substring);

void aegis_mock_ctx_free(aegis_mock_ctx_t *ctx);

#endif
