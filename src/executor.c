#define _POSIX_C_SOURCE 200809L
#include <aegis/executor.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* Stub — real implementation in Task 22 */
static aegis_exec_result_t sys_execute(const char **argv, void *ctx) {
    (void)ctx;
    fprintf(stderr, "STUB: would execute:");
    for (const char **a = argv; *a; a++) fprintf(stderr, " %s", *a);
    fprintf(stderr, "\n");
    aegis_exec_result_t r = {0, strdup(""), strdup("")};
    return r;
}

static aegis_exec_result_t sys_execute_sudo(const char **argv, void *ctx) {
    (void)ctx;
    fprintf(stderr, "STUB: would sudo:");
    for (const char **a = argv; *a; a++) fprintf(stderr, " %s", *a);
    fprintf(stderr, "\n");
    aegis_exec_result_t r = {0, strdup(""), strdup("")};
    return r;
}

static aegis_exec_result_t sys_execute_shell(const char *cmd, void *ctx) {
    (void)ctx;
    fprintf(stderr, "STUB: would shell: %s\n", cmd);
    aegis_exec_result_t r = {0, strdup(""), strdup("")};
    return r;
}

static bool sys_file_exists(const char *path, void *ctx) {
    (void)ctx;
    FILE *fp = fopen(path, "r");
    if (fp) { fclose(fp); return true; }
    return false;
}

static char *sys_read_file(const char *path, void *ctx) {
    (void)ctx;
    FILE *fp = fopen(path, "r");
    if (!fp) return NULL;
    fseek(fp, 0, SEEK_END);
    long len = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    char *buf = malloc((size_t)len + 1);
    fread(buf, 1, (size_t)len, fp);
    buf[len] = '\0';
    fclose(fp);
    return buf;
}

static int sys_write_file(const char *path, const char *content, bool sudo, void *ctx) {
    (void)sudo; (void)ctx;
    FILE *fp = fopen(path, "w");
    if (!fp) return -1;
    fputs(content, fp);
    fclose(fp);
    return 0;
}

aegis_executor_t aegis_system_executor(bool dry_run) {
    aegis_executor_t e = {0};
    e.execute       = sys_execute;
    e.execute_sudo  = sys_execute_sudo;
    e.execute_shell = sys_execute_shell;
    e.file_exists   = sys_file_exists;
    e.read_file     = sys_read_file;
    e.write_file    = sys_write_file;
    e.dry_run       = dry_run;
    e.ctx           = NULL;
    return e;
}
