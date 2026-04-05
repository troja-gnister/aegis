#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/podman_rootless.h>
#include <aegis/mock.h>

static int test_podman_rootless_apply_disabled(void) {
    aegis_podman_rootless_config_t cfg = {.enabled = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_podman_rootless_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_podman_rootless_apply_enabled(void) {
    aegis_podman_rootless_config_t cfg = {.enabled = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_podman_rootless_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Verify sysctl was called to enable userns */
    TEST_ASSERT(aegis_mock_was_called(&mock, "sysctl"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "kernel.unprivileged_userns_clone=1"));
    /* Verify subuid/subgid configured */
    TEST_ASSERT(exec.file_exists("/etc/subuid", exec.ctx));
    TEST_ASSERT(exec.file_exists("/etc/subgid", exec.ctx));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_podman_rootless:\n");
    RUN_TEST(test_podman_rootless_apply_disabled);
    RUN_TEST(test_podman_rootless_apply_enabled);
    TEST_REPORT();
}
