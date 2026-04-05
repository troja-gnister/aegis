#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/dns.h>
#include <aegis/mock.h>

static int test_dns_apply_disabled(void) {
    aegis_dns_config_t cfg = {.enabled = false, .dnssec = true, .dot = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_dns_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_dns_apply_both_enabled(void) {
    aegis_dns_config_t cfg = {.enabled = true, .dnssec = true, .dot = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_dns_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Verify config was written */
    char *content = exec.read_file("/etc/systemd/resolved.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "DNSSEC=yes") != NULL);
    TEST_ASSERT(strstr(content, "DNSOverTLS=yes") != NULL);
    free(content);
    /* Verify systemd-resolved was restarted */
    TEST_ASSERT(aegis_mock_was_called(&mock, "systemd-resolved"));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_dns:\n");
    RUN_TEST(test_dns_apply_disabled);
    RUN_TEST(test_dns_apply_both_enabled);
    TEST_REPORT();
}
