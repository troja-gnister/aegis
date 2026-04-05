#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/runner.h>
#include <aegis/module.h>
#include <aegis/mock.h>
#include <aegis/config.h>

static aegis_result_t fake_apply_ok(const void *cfg, aegis_executor_t *exec) {
    (void)cfg; (void)exec;
    return aegis_result_ok("fake applied");
}
static aegis_result_t fake_status_ok(aegis_executor_t *exec) {
    (void)exec;
    return aegis_result_ok("fake status");
}
static aegis_result_t fake_verify_ok(aegis_executor_t *exec) {
    (void)exec;
    return aegis_result_ok("fake verify");
}

static int test_runner_runs_in_priority_order(void) {
    aegis_module_t modules[] = {
        {"beta",  50, fake_apply_ok, fake_status_ok, fake_verify_ok},
        {"alpha", 10, fake_apply_ok, fake_status_ok, fake_verify_ok},
        {"gamma", 100, fake_apply_ok, fake_status_ok, fake_verify_ok},
    };
    FILE *fp = fopen("/tmp/aegis_runner_test.toml", "w");
    fprintf(fp, "# empty\n");
    fclose(fp);
    char err[256];
    aegis_config_t *cfg = aegis_config_load("/tmp/aegis_runner_test.toml", err, sizeof(err));
    TEST_ASSERT_NOT_NULL(cfg);
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    int count = 0;
    aegis_result_t *results = aegis_run_all(modules, 3, cfg, &exec, &count);
    TEST_ASSERT_EQ(count, 3);
    for (int i = 0; i < count; i++) {
        TEST_ASSERT_EQ(results[i].status, AEGIS_OK);
        aegis_result_free(&results[i]);
    }
    free(results);
    aegis_config_free(cfg);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_runner_selected_modules(void) {
    aegis_module_t modules[] = {
        {"alpha", 10, fake_apply_ok, fake_status_ok, fake_verify_ok},
        {"beta",  50, fake_apply_ok, fake_status_ok, fake_verify_ok},
        {"gamma", 100, fake_apply_ok, fake_status_ok, fake_verify_ok},
    };
    FILE *fp = fopen("/tmp/aegis_runner_test2.toml", "w");
    fprintf(fp, "# empty\n");
    fclose(fp);
    char err[256];
    aegis_config_t *cfg = aegis_config_load("/tmp/aegis_runner_test2.toml", err, sizeof(err));
    TEST_ASSERT_NOT_NULL(cfg);
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    const char *selected[] = {"gamma", "alpha"};
    int count = 0;
    aegis_result_t *results = aegis_run_selected(modules, 3, cfg, &exec, selected, 2, &count);
    TEST_ASSERT_EQ(count, 2);
    for (int i = 0; i < count; i++) {
        TEST_ASSERT_EQ(results[i].status, AEGIS_OK);
        aegis_result_free(&results[i]);
    }
    free(results);
    aegis_config_free(cfg);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_runner:\n");
    RUN_TEST(test_runner_runs_in_priority_order);
    RUN_TEST(test_runner_selected_modules);
    TEST_REPORT();
}
