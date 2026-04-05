#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/kernel.h>
#include <aegis/mock.h>

static int test_kernel_apply_disabled(void) {
    aegis_kernel_config_t cfg = {.enabled = false, .lockdown = "integrity"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_kernel_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_kernel_apply_integrity(void) {
    aegis_kernel_config_t cfg = {.enabled = true, .lockdown = "integrity"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_mock_add_file(&mock, "/etc/default/grub",
        "GRUB_CMDLINE_LINUX_DEFAULT=\"loglevel=3 quiet\"\n");
    aegis_result_t res = aegis_kernel_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(aegis_mock_was_called(&mock, "pacman"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "grub-mkconfig"));
    /* Verify GRUB config was modified with lockdown param */
    char *grub = exec.read_file("/etc/default/grub", exec.ctx);
    TEST_ASSERT_NOT_NULL(grub);
    TEST_ASSERT(strstr(grub, "lockdown=integrity") != NULL);
    free(grub);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_kernel_apply_confidentiality(void) {
    aegis_kernel_config_t cfg = {.enabled = true, .lockdown = "confidentiality"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_kernel_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(res.action_count >= 3);
    char *grub = exec.read_file("/etc/default/grub", exec.ctx);
    TEST_ASSERT_NOT_NULL(grub);
    TEST_ASSERT(strstr(grub, "lockdown=confidentiality") != NULL);
    free(grub);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_kernel:\n");
    RUN_TEST(test_kernel_apply_disabled);
    RUN_TEST(test_kernel_apply_integrity);
    RUN_TEST(test_kernel_apply_confidentiality);
    TEST_REPORT();
}
