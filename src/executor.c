#define _POSIX_C_SOURCE 200809L
#include <aegis/executor.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <unistd.h>
#include <sys/wait.h>
#include <fcntl.h>
#include <errno.h>
#include <signal.h>

static volatile sig_atomic_t _timed_out = 0;

static void alarm_handler(int sig) {
    (void)sig;
    _timed_out = 1;
}

typedef struct {
    bool dry_run;
} sys_ctx_t;

/* Read all data from an fd into a malloc'd, NUL-terminated buffer. */
static char *read_fd(int fd) {
    size_t cap = 4096;
    size_t len = 0;
    char *buf = malloc(cap);
    if (!buf) return NULL;

    ssize_t n;
    while ((n = read(fd, buf + len, cap - len - 1)) > 0) {
        len += (size_t)n;
        if (len + 1 >= cap) {
            cap *= 2;
            char *tmp = realloc(buf, cap);
            if (!tmp) { free(buf); return NULL; }
            buf = tmp;
        }
    }
    buf[len] = '\0';
    return buf;
}

/* Fork and exec argv[], capturing stdout and stderr. */
static aegis_exec_result_t run_argv(const char **argv) {
    aegis_exec_result_t result = {-1, NULL, NULL};

    int out_pipe[2], err_pipe[2];
    if (pipe(out_pipe) != 0 || pipe(err_pipe) != 0) {
        result.stdout_buf = strdup("");
        result.stderr_buf = strdup("pipe() failed");
        return result;
    }

    pid_t pid = fork();
    if (pid < 0) {
        close(out_pipe[0]); close(out_pipe[1]);
        close(err_pipe[0]); close(err_pipe[1]);
        result.stdout_buf = strdup("");
        result.stderr_buf = strdup("fork() failed");
        return result;
    }

    if (pid == 0) {
        /* Child: put self in its own process group to avoid PID-reuse race */
        setpgid(0, 0);
        close(out_pipe[0]);
        close(err_pipe[0]);
        dup2(out_pipe[1], STDOUT_FILENO);
        dup2(err_pipe[1], STDERR_FILENO);
        close(out_pipe[1]);
        close(err_pipe[1]);
        execvp(argv[0], (char *const *)argv);
        /* exec failed */
        _exit(127);
    }

    /* Parent */
    close(out_pipe[1]);
    close(err_pipe[1]);
    setpgid(pid, pid);  // Ensure pgid is set even if child hasn't run yet

    /* Set timeout BEFORE reading — covers both I/O and waitpid */
    _timed_out = 0;
    struct sigaction sa = {0};
    sa.sa_handler = alarm_handler;
    sa.sa_flags = 0; /* Don't use SA_RESTART — let read/waitpid be interrupted */
    sigaction(SIGALRM, &sa, NULL);
    alarm(120);

    char *out_buf = read_fd(out_pipe[0]);
    close(out_pipe[0]);

    char *err_buf;
    if (_timed_out) {
        err_buf = strdup("");
        close(err_pipe[0]);
    } else {
        err_buf = read_fd(err_pipe[0]);
        close(err_pipe[0]);
    }

    int status;
    pid_t w = waitpid(pid, &status, 0);

    alarm(0); /* Cancel alarm */

    if (_timed_out || w < 0) {
        kill(-pid, SIGKILL); /* Kill entire process group */
        waitpid(pid, &status, WNOHANG); /* Reap without blocking */
        free(out_buf); free(err_buf);
        result.exit_code = -1;
        result.stdout_buf = strdup("");
        result.stderr_buf = strdup("command timed out after 120s");
        return result;
    }

    result.stdout_buf = out_buf ? out_buf : strdup("");
    result.stderr_buf = err_buf ? err_buf : strdup("");
    result.exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : -1;

    return result;
}

static void print_dry_run(const char *prefix, const char **argv) {
    printf("[dry-run]");
    if (prefix) printf(" %s", prefix);
    for (const char **a = argv; *a; a++) printf(" %s", *a);
    printf("\n");
}

static aegis_exec_result_t sys_execute(const char **argv, void *ctx) {
    sys_ctx_t *sctx = ctx;
    if (sctx && sctx->dry_run) {
        print_dry_run(NULL, argv);
        aegis_exec_result_t r = {0, strdup(""), strdup("")};
        return r;
    }
    return run_argv(argv);
}

static aegis_exec_result_t sys_execute_sudo(const char **argv, void *ctx) {
    sys_ctx_t *sctx = ctx;
    if (sctx && sctx->dry_run) {
        print_dry_run("sudo", argv);
        aegis_exec_result_t r = {0, strdup(""), strdup("")};
        return r;
    }

    /* Count argv length */
    int argc = 0;
    while (argv[argc]) argc++;

    /* Build sudo + original argv */
    const char **sudo_argv = malloc((size_t)(argc + 2) * sizeof(char *));
    if (!sudo_argv) {
        aegis_exec_result_t r = {-1, strdup(""), strdup("malloc failed")};
        return r;
    }
    sudo_argv[0] = "sudo";
    for (int i = 0; i < argc; i++) sudo_argv[i + 1] = argv[i];
    sudo_argv[argc + 1] = NULL;

    aegis_exec_result_t result = run_argv(sudo_argv);
    free(sudo_argv);
    return result;
}

static aegis_exec_result_t sys_execute_shell(const char *cmd, void *ctx) {
    sys_ctx_t *sctx = ctx;
    if (sctx && sctx->dry_run) {
        printf("[dry-run] sh -c %s\n", cmd);
        aegis_exec_result_t r = {0, strdup(""), strdup("")};
        return r;
    }
    const char *argv[] = {"sh", "-c", cmd, NULL};
    return run_argv(argv);
}

static bool sys_file_exists(const char *path, void *ctx) {
    (void)ctx;
    return access(path, F_OK) == 0;
}

static char *sys_read_file(const char *path, void *ctx) {
    (void)ctx;
    FILE *fp = fopen(path, "r");
    if (!fp) return NULL;
    if (fseek(fp, 0, SEEK_END) != 0) { fclose(fp); return NULL; }
    long len = ftell(fp);
    if (len < 0) { fclose(fp); return NULL; }
    rewind(fp);
    char *buf = malloc((size_t)len + 1);
    if (!buf) { fclose(fp); return NULL; }
    size_t n = fread(buf, 1, (size_t)len, fp);
    buf[n] = '\0';
    fclose(fp);
    return buf;
}

static int sys_write_file(const char *path, const char *content, bool sudo, void *ctx) {
    sys_ctx_t *sctx = ctx;
    if (sctx && sctx->dry_run) {
        printf("[dry-run] write_file %s%s (%zu bytes)\n",
               sudo ? "[sudo] " : "", path, strlen(content));
        return 0;
    }
    if (sudo) {
        /* Use sudo tee to write as root */
        int in_pipe[2];
        if (pipe(in_pipe) != 0) return -1;

        pid_t pid = fork();
        if (pid < 0) { close(in_pipe[0]); close(in_pipe[1]); return -1; }

        if (pid == 0) {
            /* Child: sudo tee <path> */
            close(in_pipe[1]);
            dup2(in_pipe[0], STDIN_FILENO);
            close(in_pipe[0]);
            /* Suppress tee stdout */
            int devnull = open("/dev/null", O_WRONLY);
            if (devnull >= 0) { dup2(devnull, STDOUT_FILENO); close(devnull); }
            execlp("sudo", "sudo", "tee", path, (char *)NULL);
            _exit(127);
        }

        /* Parent: write content to pipe */
        close(in_pipe[0]);
        size_t len = strlen(content);
        size_t written = 0;
        while (written < len) {
            ssize_t n = write(in_pipe[1], content + written, len - written);
            if (n <= 0) break;
            written += (size_t)n;
        }
        close(in_pipe[1]);

        int status = 0;
        waitpid(pid, &status, 0);
        return (WIFEXITED(status) && WEXITSTATUS(status) == 0) ? 0 : -1;
    }

    /* Non-sudo: direct write */
    FILE *fp = fopen(path, "w");
    if (!fp) return -1;
    fputs(content, fp);
    fclose(fp);
    return 0;
}

aegis_executor_t aegis_system_executor(bool dry_run) {
    sys_ctx_t *sctx = malloc(sizeof(sys_ctx_t));
    if (sctx) sctx->dry_run = dry_run;

    aegis_executor_t e = {0};
    e.execute       = sys_execute;
    e.execute_sudo  = sys_execute_sudo;
    e.execute_shell = sys_execute_shell;
    e.file_exists   = sys_file_exists;
    e.read_file     = sys_read_file;
    e.write_file    = sys_write_file;
    e.dry_run       = dry_run;
    e.ctx           = sctx;
    return e;
}

/* aegis_exec_result_free is defined in mock.c (shared utility) */
