#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/snapper.h>
#include <aegis/mock.h>
#include <aegis/config.h>

static int test_snapper_apply_disabled(void) {
    aegis_snapper_config_t cfg = {.enabled = false, .subvolumes = NULL, .subvolume_count = 0};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_snapper_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_snapper_apply_subvolumes(void) {
    char *vols[] = {"/", "/home"};
    aegis_snapper_config_t cfg = {.enabled = true, .subvolumes = vols, .subvolume_count = 2};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_snapper_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* snapper create-config called for each subvolume */
    TEST_ASSERT(aegis_mock_was_called(&mock, "snapper"));
    /* pacman hook installed */
    TEST_ASSERT(exec.file_exists("/etc/pacman.d/hooks/snap-pac.hook", exec.ctx));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_snapper:\n");
    RUN_TEST(test_snapper_apply_disabled);
    RUN_TEST(test_snapper_apply_subvolumes);
    TEST_REPORT();
}
