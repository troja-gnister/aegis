#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/archaudit.h>
#include <aegis/mock.h>

static int test_archaudit_apply_disabled(void) {
    aegis_archaudit_config_t cfg = {.enabled = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_archaudit_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_archaudit_apply_enabled(void) {
    aegis_archaudit_config_t cfg = {.enabled = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_archaudit_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Verify pacman was called to install arch-audit */
    TEST_ASSERT(aegis_mock_was_called(&mock, "pacman"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "arch-audit"));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_archaudit:\n");
    RUN_TEST(test_archaudit_apply_disabled);
    RUN_TEST(test_archaudit_apply_enabled);
    TEST_REPORT();
}
