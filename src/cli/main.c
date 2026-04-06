#define _POSIX_C_SOURCE 200809L
#include <aegis/aegis.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define DEFAULT_CONFIG "/etc/aegis/aegis.toml"
#define VERSION "0.1.0"

typedef enum { CMD_NONE, CMD_HELP, CMD_HARDEN, CMD_STATUS, CMD_VERIFY, CMD_LIST,
               CMD_CONFIG_VALIDATE, CMD_CONFIG_INIT, CMD_VERSION } command_t;

typedef struct {
    command_t   cmd;
    const char *config_path;
    bool        dry_run;
    bool        verbose;
    const char **selected_modules;
    int          selected_count;
} cli_args_t;

static void usage(void) {
    printf(
        "Usage: aegis <command> [options] [modules...]\n\n"
        "Commands:\n"
        "  harden [modules...]   Apply security hardening\n"
        "  status [modules...]   Show current hardening state\n"
        "  verify [modules...]   Compliance check\n"
        "  list                  List all modules\n"
        "  config validate       Validate config file\n"
        "  config init           Generate default config\n"
        "  version               Print version\n\n"
        "Options:\n"
        "  -h, --help            Show this help message and exit\n"
        "  --dry-run             Preview without applying\n"
        "  --verbose             Verbose output\n"
        "  --config PATH         Config file (default: %s)\n",
        DEFAULT_CONFIG);
}

static cli_args_t parse_args(int argc, char **argv) {
    cli_args_t args = {0};
    args.config_path = DEFAULT_CONFIG;

    if (argc < 2) { args.cmd = CMD_NONE; return args; }

    /* Check for help flags before anything else */
    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            args.cmd = CMD_HELP;
            return args;
        }
    }

    int i = 1;
    /* Parse command */
    if (strcmp(argv[i], "harden") == 0)       args.cmd = CMD_HARDEN;
    else if (strcmp(argv[i], "status") == 0)   args.cmd = CMD_STATUS;
    else if (strcmp(argv[i], "verify") == 0)   args.cmd = CMD_VERIFY;
    else if (strcmp(argv[i], "list") == 0)     args.cmd = CMD_LIST;
    else if (strcmp(argv[i], "version") == 0)  { args.cmd = CMD_VERSION; return args; }
    else if (strcmp(argv[i], "config") == 0) {
        if (i + 1 < argc && strcmp(argv[i + 1], "validate") == 0) { args.cmd = CMD_CONFIG_VALIDATE; i++; }
        else if (i + 1 < argc && strcmp(argv[i + 1], "init") == 0) { args.cmd = CMD_CONFIG_INIT; i++; }
        else { args.cmd = CMD_NONE; return args; }
    }
    else { args.cmd = CMD_NONE; return args; }
    i++;

    /* Parse flags and module names */
    const char **modules = NULL;
    int mod_count = 0;

    for (; i < argc; i++) {
        if (strcmp(argv[i], "--dry-run") == 0) { args.dry_run = true; }
        else if (strcmp(argv[i], "--verbose") == 0) { args.verbose = true; }
        else if (strcmp(argv[i], "--config") == 0 && i + 1 < argc) { args.config_path = argv[++i]; }
        else {
            /* Module name */
            modules = realloc(modules, (size_t)(mod_count + 1) * sizeof(char *));
            modules[mod_count++] = argv[i];
        }
    }
    args.selected_modules = modules;
    args.selected_count = mod_count;
    return args;
}

static void print_result(const char *module_name, const aegis_result_t *r, bool verbose) {
    const char *status_str;
    switch (r->status) {
        case AEGIS_OK:   status_str = "\033[32m OK \033[0m"; break;
        case AEGIS_WARN: status_str = "\033[33mWARN\033[0m"; break;
        case AEGIS_FAIL: status_str = "\033[31mFAIL\033[0m"; break;
        case AEGIS_SKIP: status_str = "\033[90mSKIP\033[0m"; break;
        default:         status_str = "????"; break;
    }
    printf("  [%s] %-24s %s\n", status_str, module_name, r->message);
    if (verbose && r->action_count > 0) {
        for (int j = 0; j < r->action_count; j++) {
            printf("         %s\n", r->actions[j]);
        }
    }
}

int main(int argc, char **argv) {
    cli_args_t args = parse_args(argc, argv);

    if (args.cmd == CMD_NONE) { usage(); return 1; }
    if (args.cmd == CMD_HELP) { usage(); return 0; }
    if (args.cmd == CMD_VERSION) { printf("aegis %s\n", VERSION); return 0; }

    /* Load config for most commands */
    char err[512] = {0};
    aegis_config_t *cfg = NULL;
    if (args.cmd != CMD_CONFIG_INIT && args.cmd != CMD_LIST) {
        cfg = aegis_config_load(args.config_path, err, sizeof(err));
        if (!cfg && args.cmd != CMD_CONFIG_VALIDATE) {
            fprintf(stderr, "Error: %s\n", err);
            free((void *)args.selected_modules);
            return 1;
        }
        if (args.cmd == CMD_CONFIG_VALIDATE) {
            if (cfg) {
                printf("Config OK: %s\n", args.config_path);
                aegis_config_free(cfg);
            } else {
                fprintf(stderr, "Config error: %s\n", err);
            }
            free((void *)args.selected_modules);
            return cfg ? 0 : 1;
        }
    }

    if (args.cmd == CMD_CONFIG_INIT) {
        /* Locate the example config: prefer installed path, fall back to source tree */
        const char *example_paths[] = {
            "/usr/share/aegis/aegis.toml.example",
            "config/aegis.toml.example",
            NULL
        };
        const char *src_path = NULL;
        for (int i = 0; example_paths[i]; i++) {
            FILE *probe = fopen(example_paths[i], "r");
            if (probe) { fclose(probe); src_path = example_paths[i]; break; }
        }
        if (!src_path) {
            fprintf(stderr, "Error: cannot find aegis.toml.example\n");
            free((void *)args.selected_modules);
            return 1;
        }
        /* Read the example config */
        FILE *src = fopen(src_path, "r");
        if (!src) {
            fprintf(stderr, "Error: cannot open %s: ", src_path);
            perror(NULL);
            free((void *)args.selected_modules);
            return 1;
        }
        fseek(src, 0, SEEK_END);
        long len = ftell(src);
        rewind(src);
        char *buf = malloc((size_t)len + 1);
        if (!buf) { fclose(src); free((void *)args.selected_modules); return 1; }
        size_t n = fread(buf, 1, (size_t)len, src);
        buf[n] = '\0';
        fclose(src);
        /* Write to destination path */
        FILE *dst = fopen(args.config_path, "w");
        if (!dst) {
            fprintf(stderr, "Error: cannot write %s: ", args.config_path);
            perror(NULL);
            free(buf);
            free((void *)args.selected_modules);
            return 1;
        }
        fputs(buf, dst);
        fclose(dst);
        free(buf);
        printf("Config written to %s\n", args.config_path);
        free((void *)args.selected_modules);
        return 0;
    }

    /* Get module registry */
    int module_count = 0;
    aegis_module_t *modules = aegis_get_modules(&module_count);

    if (args.cmd == CMD_LIST) {
        printf("Modules (%d):\n", module_count);
        for (int i = 0; i < module_count; i++) {
            printf("  %-24s priority=%d\n", modules[i].name, modules[i].priority);
        }
        aegis_config_free(cfg);
        free((void *)args.selected_modules);
        return 0;
    }

    /* Create executor */
    aegis_executor_t exec = aegis_system_executor(args.dry_run);

    if (args.dry_run) printf("=== DRY RUN ===\n\n");

    int result_count = 0;
    aegis_result_t *results = NULL;

    switch (args.cmd) {
        case CMD_HARDEN:
            if (args.selected_count > 0) {
                results = aegis_run_selected(modules, module_count, cfg, &exec,
                                              args.selected_modules, args.selected_count,
                                              &result_count);
            } else {
                results = aegis_run_all(modules, module_count, cfg, &exec, &result_count);
            }
            break;
        case CMD_STATUS:
            if (args.selected_count > 0) {
                results = aegis_status_selected(modules, module_count, &exec,
                                                 args.selected_modules, args.selected_count,
                                                 &result_count);
            } else {
                results = aegis_status_all(modules, module_count, &exec, &result_count);
            }
            break;
        case CMD_VERIFY:
            if (args.selected_count > 0) {
                results = aegis_verify_selected(modules, module_count, &exec,
                                                 args.selected_modules, args.selected_count,
                                                 &result_count);
            } else {
                results = aegis_verify_all(modules, module_count, &exec, &result_count);
            }
            break;
        default:
            break;
    }

    /* Print results */
    if (results) {
        int ok = 0, warn = 0, fail = 0, skip = 0;
        for (int i = 0; i < result_count; i++) {
            /* Extract module name from message prefix (format: "name: ...") */
            char name_buf[64] = "unknown";
            if (results[i].message) {
                const char *colon = strchr(results[i].message, ':');
                if (colon) {
                    size_t len = (size_t)(colon - results[i].message);
                    if (len < sizeof(name_buf)) {
                        memcpy(name_buf, results[i].message, len);
                        name_buf[len] = '\0';
                    }
                }
            }
            print_result(name_buf, &results[i], args.verbose);
            switch (results[i].status) {
                case AEGIS_OK: ok++; break;
                case AEGIS_WARN: warn++; break;
                case AEGIS_FAIL: fail++; break;
                case AEGIS_SKIP: skip++; break;
            }
            aegis_result_free(&results[i]);
        }
        printf("\n%d ok, %d warn, %d fail, %d skip\n", ok, warn, fail, skip);
        free(results);
    }

    free(exec.ctx); /* Free SystemExecutor context */
    aegis_config_free(cfg);
    free((void *)args.selected_modules);
    return 0;
}
