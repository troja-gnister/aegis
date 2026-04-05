#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/secureboot.h>
#include <aegis/mock.h>

static int test_secureboot_apply_disabled(void) {
    aegis_secureboot_config_t cfg = {.enabled = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_secureboot_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_secureboot_apply_enabled(void) {
    aegis_secureboot_config_t cfg = {.enabled = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_secureboot_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Verify sbctl was called for create-keys, enroll-keys and sign-all */
    TEST_ASSERT(aegis_mock_was_called(&mock, "sbctl"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "create-keys"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "enroll-keys"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "sign-all"));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_secureboot:\n");
    RUN_TEST(test_secureboot_apply_disabled);
    RUN_TEST(test_secureboot_apply_enabled);
    TEST_REPORT();
}
