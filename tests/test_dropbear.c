#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/dropbear.h>
#include <aegis/mock.h>

static int test_dropbear_apply_disabled(void) {
    aegis_dropbear_config_t cfg = {.enabled = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_dropbear_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_dropbear_apply_enabled(void) {
    aegis_dropbear_config_t cfg = {.enabled = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    /* Pre-populate mkinitcpio.conf */
    aegis_mock_add_file(&mock, "/etc/mkinitcpio.conf",
        "HOOKS=(base udev autodetect modconf block filesystems keyboard fsck)\n");
    aegis_result_t res = aegis_dropbear_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Verify mkinitcpio -P was called */
    TEST_ASSERT(aegis_mock_was_called(&mock, "mkinitcpio"));
    /* Verify dropbear hook was added to the config */
    char *content = exec.read_file("/etc/mkinitcpio.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "dropbear") != NULL);
    free(content);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_dropbear:\n");
    RUN_TEST(test_dropbear_apply_disabled);
    RUN_TEST(test_dropbear_apply_enabled);
    TEST_REPORT();
}
