#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/aide.h>
#include <aegis/mock.h>

static int test_aide_apply_disabled(void) {
    aegis_aide_config_t cfg = {.enabled = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_aide_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_aide_apply_enabled(void) {
    aegis_aide_config_t cfg = {.enabled = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_aide_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Verify aide --init was called */
    TEST_ASSERT(aegis_mock_was_called(&mock, "aide"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "--init"));
    /* Verify aide.conf was written */
    char *content = exec.read_file("/etc/aide.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    free(content);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_aide:\n");
    RUN_TEST(test_aide_apply_disabled);
    RUN_TEST(test_aide_apply_enabled);
    TEST_REPORT();
}
