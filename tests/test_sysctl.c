#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/sysctl.h>
#include <aegis/mock.h>
#include <aegis/config.h>

static int test_sysctl_apply_disabled(void) {
    aegis_sysctl_config_t cfg = {.enabled = false, .profile = "standard"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_sysctl_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    TEST_ASSERT_EQ(aegis_mock_call_count(&mock), 0);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_sysctl_apply_standard(void) {
    aegis_sysctl_config_t cfg = {.enabled = true, .profile = "standard"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_sysctl_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(res.action_count > 0);
    /* Should have called sysctl --system to reload */
    TEST_ASSERT(aegis_mock_was_called(&mock, "sysctl --system"));
    /* Should have written the persistent config file */
    TEST_ASSERT(exec.file_exists("/etc/sysctl.d/99-aegis.conf", exec.ctx));

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_sysctl_apply_minimal(void) {
    aegis_sysctl_config_t cfg = {.enabled = true, .profile = "minimal"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_sysctl_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(res.action_count > 0);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_sysctl_apply_paranoid(void) {
    aegis_sysctl_config_t cfg = {.enabled = true, .profile = "paranoid"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_sysctl_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Paranoid should have more actions than minimal */
    TEST_ASSERT(res.action_count > 5);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_sysctl_status(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_mock_add_response(&mock, "sysctl", 0, "kernel.kptr_restrict = 2\n");

    aegis_result_t res = aegis_sysctl_status(&exec);
    TEST_ASSERT(res.status == AEGIS_OK || res.status == AEGIS_WARN);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_sysctl_verify(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_mock_add_response(&mock, "sysctl", 0, "kernel.kptr_restrict = 2\n");

    aegis_result_t res = aegis_sysctl_verify(&exec);
    TEST_ASSERT(res.status == AEGIS_OK || res.status == AEGIS_WARN);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_sysctl:\n");
    RUN_TEST(test_sysctl_apply_disabled);
    RUN_TEST(test_sysctl_apply_standard);
    RUN_TEST(test_sysctl_apply_minimal);
    RUN_TEST(test_sysctl_apply_paranoid);
    RUN_TEST(test_sysctl_status);
    RUN_TEST(test_sysctl_verify);
    TEST_REPORT();
}
