#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/malloc_hardened.h>
#include <aegis/mock.h>

static int test_malloc_hardened_apply_disabled(void) {
    aegis_malloc_config_t cfg = {.enabled = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_malloc_hardened_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_malloc_hardened_apply_enabled(void) {
    aegis_malloc_config_t cfg = {.enabled = true};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_malloc_hardened_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Verify /etc/ld.so.preload was written */
    TEST_ASSERT(exec.file_exists("/etc/ld.so.preload", exec.ctx));
    char *content = exec.read_file("/etc/ld.so.preload", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "libhardened_malloc.so") != NULL);
    free(content);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_malloc_hardened:\n");
    RUN_TEST(test_malloc_hardened_apply_disabled);
    RUN_TEST(test_malloc_hardened_apply_enabled);
    TEST_REPORT();
}
