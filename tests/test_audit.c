#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/audit.h>
#include <aegis/mock.h>

static int test_audit_apply_disabled(void) {
    aegis_audit_config_t cfg = {.enabled = false, .profile = "standard"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_audit_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_audit_apply_standard(void) {
    aegis_audit_config_t cfg = {.enabled = true, .profile = "standard"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_audit_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(aegis_mock_was_called(&mock, "augenrules"));
    TEST_ASSERT(exec.file_exists("/etc/audit/rules.d/aegis.rules", exec.ctx));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_audit_apply_stig(void) {
    aegis_audit_config_t cfg = {.enabled = true, .profile = "stig"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_audit_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    char *content = exec.read_file("/etc/audit/rules.d/aegis.rules", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "-e 2") != NULL); /* stig-only immutable flag */
    free(content);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_audit:\n");
    RUN_TEST(test_audit_apply_disabled);
    RUN_TEST(test_audit_apply_standard);
    RUN_TEST(test_audit_apply_stig);
    TEST_REPORT();
}
