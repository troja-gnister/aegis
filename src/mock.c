#define _POSIX_C_SOURCE 200809L
#include <aegis/mock.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

static void record_call(aegis_mock_ctx_t *m, const char *prefix, const char **argv) {
    size_t len = prefix ? strlen(prefix) + 1 : 0;
    for (const char **a = argv; *a; a++) len += strlen(*a) + 1;

    char *buf = malloc(len + 1);
    buf[0] = '\0';
    if (prefix) { strcat(buf, prefix); strcat(buf, " "); }
    for (const char **a = argv; *a; a++) {
        strcat(buf, *a);
        if (*(a + 1)) strcat(buf, " ");
    }

    if (m->call_count >= m->_call_cap) {
        m->_call_cap = m->_call_cap == 0 ? 16 : m->_call_cap * 2;
        m->calls = realloc(m->calls, (size_t)m->_call_cap * sizeof(char *));
    }
    m->calls[m->call_count++] = buf;
}

static aegis_exec_result_t find_response(aegis_mock_ctx_t *m, const char **argv) {
    aegis_exec_result_t r = {0, strdup(""), strdup("")};
    if (!argv || !argv[0]) return r;

    for (int i = 0; i < m->response_count; i++) {
        if (strncmp(argv[0], m->responses[i].cmd_prefix,
                    strlen(m->responses[i].cmd_prefix)) == 0) {
            r.exit_code = m->responses[i].exit_code;
            free(r.stdout_buf);
            r.stdout_buf = strdup(m->responses[i].stdout_str);
            break;
        }
    }
    return r;
}

static aegis_exec_result_t mock_execute(const char **argv, void *ctx) {
    aegis_mock_ctx_t *m = ctx;
    record_call(m, NULL, argv);
    return find_response(m, argv);
}

static aegis_exec_result_t mock_execute_sudo(const char **argv, void *ctx) {
    aegis_mock_ctx_t *m = ctx;
    record_call(m, "[sudo]", argv);
    return find_response(m, argv);
}

static aegis_exec_result_t mock_execute_shell(const char *cmd, void *ctx) {
    aegis_mock_ctx_t *m = ctx;
    const char *argv[] = {cmd, NULL};
    record_call(m, "[shell]", argv);
    return find_response(m, argv);
}

static bool mock_file_exists(const char *path, void *ctx) {
    aegis_mock_ctx_t *m = ctx;
    for (int i = 0; i < m->file_count; i++) {
        if (strcmp(m->files[i].path, path) == 0) return true;
    }
    return false;
}

static char *mock_read_file(const char *path, void *ctx) {
    aegis_mock_ctx_t *m = ctx;
    for (int i = 0; i < m->file_count; i++) {
        if (strcmp(m->files[i].path, path) == 0)
            return strdup(m->files[i].content);
    }
    return NULL;
}

static int mock_write_file(const char *path, const char *content, bool sudo, void *ctx) {
    (void)sudo;
    aegis_mock_ctx_t *m = ctx;
    for (int i = 0; i < m->file_count; i++) {
        if (strcmp(m->files[i].path, path) == 0) {
            free(m->files[i].content);
            m->files[i].content = strdup(content);
            return 0;
        }
    }
    if (m->file_count >= AEGIS_MOCK_MAX_FILES) return -1;
    m->files[m->file_count].path = strdup(path);
    m->files[m->file_count].content = strdup(content);
    m->file_count++;
    return 0;
}

aegis_executor_t aegis_mock_executor(aegis_mock_ctx_t *ctx) {
    aegis_executor_t e = {0};
    e.execute       = mock_execute;
    e.execute_sudo  = mock_execute_sudo;
    e.execute_shell = mock_execute_shell;
    e.file_exists   = mock_file_exists;
    e.read_file     = mock_read_file;
    e.write_file    = mock_write_file;
    e.dry_run       = false;
    e.ctx           = ctx;
    return e;
}

void aegis_mock_add_response(aegis_mock_ctx_t *ctx, const char *cmd_prefix,
                              int exit_code, const char *stdout_str) {
    if (ctx->response_count >= AEGIS_MOCK_MAX_RESPONSES) return;
    int i = ctx->response_count++;
    ctx->responses[i].cmd_prefix = strdup(cmd_prefix);
    ctx->responses[i].exit_code  = exit_code;
    ctx->responses[i].stdout_str = strdup(stdout_str);
}

void aegis_mock_add_file(aegis_mock_ctx_t *ctx, const char *path, const char *content) {
    mock_write_file(path, content, false, ctx);
}

int aegis_mock_call_count(const aegis_mock_ctx_t *ctx) { return ctx->call_count; }

const char *aegis_mock_call_at(const aegis_mock_ctx_t *ctx, int index) {
    if (index < 0 || index >= ctx->call_count) return NULL;
    return ctx->calls[index];
}

bool aegis_mock_was_called(const aegis_mock_ctx_t *ctx, const char *substring) {
    for (int i = 0; i < ctx->call_count; i++) {
        if (strstr(ctx->calls[i], substring)) return true;
    }
    return false;
}

void aegis_mock_ctx_free(aegis_mock_ctx_t *ctx) {
    for (int i = 0; i < ctx->call_count; i++) free(ctx->calls[i]);
    free(ctx->calls);
    for (int i = 0; i < ctx->file_count; i++) {
        free(ctx->files[i].path);
        free(ctx->files[i].content);
    }
    for (int i = 0; i < ctx->response_count; i++) {
        free(ctx->responses[i].cmd_prefix);
        free(ctx->responses[i].stdout_str);
    }
    memset(ctx, 0, sizeof(*ctx));
}

void aegis_exec_result_free(aegis_exec_result_t *r) {
    free(r->stdout_buf);
    free(r->stderr_buf);
    r->stdout_buf = NULL;
    r->stderr_buf = NULL;
}
