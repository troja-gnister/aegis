#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/fail2ban.h>
#include <aegis/mock.h>

static int test_fail2ban_apply_disabled(void) {
    aegis_fail2ban_config_t cfg = {.enabled = false, .profile = "standard"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_fail2ban_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_fail2ban_apply_standard_profile(void) {
    aegis_fail2ban_config_t cfg = {.enabled = true, .profile = "standard"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_fail2ban_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(aegis_mock_was_called(&mock, "fail2ban"));
    /* Verify jail config was written with recidive jail */
    char *content = exec.read_file("/etc/fail2ban/jail.d/aegis.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "[sshd]") != NULL);
    TEST_ASSERT(strstr(content, "[recidive]") != NULL);
    free(content);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_fail2ban_apply_strict_profile(void) {
    aegis_fail2ban_config_t cfg = {.enabled = true, .profile = "strict"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_fail2ban_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Verify jail config uses lower maxretry (3 not 5) */
    char *content = exec.read_file("/etc/fail2ban/jail.d/aegis.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "maxretry = 3") != NULL);
    TEST_ASSERT(strstr(content, "bantime = 86400") != NULL);
    free(content);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_fail2ban:\n");
    RUN_TEST(test_fail2ban_apply_disabled);
    RUN_TEST(test_fail2ban_apply_standard_profile);
    RUN_TEST(test_fail2ban_apply_strict_profile);
    TEST_REPORT();
}
