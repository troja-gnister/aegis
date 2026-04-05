#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/mounts.h>
#include <aegis/mock.h>

static int test_mounts_apply_disabled(void) {
    aegis_mounts_config_t cfg = {.enabled = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_mounts_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_mounts_apply_all(void) {
    aegis_mounts_config_t cfg = {.enabled = true, .harden_tmp = true, .harden_dev_shm = true, .harden_proc = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_mounts_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(res.action_count >= 3);
    /* Should write systemd tmp.mount and fstab entries */
    TEST_ASSERT(exec.file_exists("/etc/systemd/system/tmp.mount", exec.ctx) ||
                aegis_mock_was_called(&mock, "mount"));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_mounts_apply_partial(void) {
    aegis_mounts_config_t cfg = {.enabled = true, .harden_tmp = true, .harden_dev_shm = false, .harden_proc = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_mounts_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(res.action_count >= 1);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_mounts:\n");
    RUN_TEST(test_mounts_apply_disabled);
    RUN_TEST(test_mounts_apply_all);
    RUN_TEST(test_mounts_apply_partial);
    TEST_REPORT();
}
