#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/usbguard.h>
#include <aegis/mock.h>

static int test_usbguard_apply_disabled(void) {
    aegis_usbguard_config_t cfg = {.enabled = false, .default_policy = "block"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_usbguard_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_usbguard_apply_block_policy(void) {
    aegis_usbguard_config_t cfg = {.enabled = true, .default_policy = "block"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_usbguard_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(aegis_mock_was_called(&mock, "usbguard"));
    /* Verify daemon config has block policy */
    char *content = exec.read_file("/etc/usbguard/usbguard-daemon.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "ImplicitPolicyTarget=block") != NULL);
    free(content);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_usbguard:\n");
    RUN_TEST(test_usbguard_apply_disabled);
    RUN_TEST(test_usbguard_apply_block_policy);
    TEST_REPORT();
}
