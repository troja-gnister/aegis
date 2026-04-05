#include "test.h"
#include <aegis/executor.h>
#include <aegis/mock.h>

static int test_mock_records_execute(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    const char *argv[] = {"sysctl", "-w", "kernel.kptr_restrict=2", NULL};
    aegis_exec_result_t r = exec.execute(argv, exec.ctx);

    TEST_ASSERT_EQ(r.exit_code, 0);
    TEST_ASSERT_EQ(aegis_mock_call_count(&mock), 1);
    TEST_ASSERT(aegis_mock_was_called(&mock, "sysctl"));

    aegis_exec_result_free(&r);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_mock_records_sudo(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    const char *argv[] = {"pacman", "-S", "sbctl", NULL};
    aegis_exec_result_t r = exec.execute_sudo(argv, exec.ctx);

    TEST_ASSERT_EQ(r.exit_code, 0);
    TEST_ASSERT(aegis_mock_was_called(&mock, "[sudo] pacman"));

    aegis_exec_result_free(&r);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_mock_write_and_read_file(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    exec.write_file("/etc/test.conf", "key=value\n", false, exec.ctx);

    TEST_ASSERT(exec.file_exists("/etc/test.conf", exec.ctx));
    char *content = exec.read_file("/etc/test.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT_STR_EQ(content, "key=value\n");
    free(content);

    TEST_ASSERT(!exec.file_exists("/nonexistent", exec.ctx));

    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_mock_preconfigured_response(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_mock_add_response(&mock, "sysctl", 0, "kernel.kptr_restrict = 2\n");

    const char *argv[] = {"sysctl", "kernel.kptr_restrict", NULL};
    aegis_exec_result_t r = exec.execute(argv, exec.ctx);

    TEST_ASSERT_EQ(r.exit_code, 0);
    TEST_ASSERT_STR_EQ(r.stdout_buf, "kernel.kptr_restrict = 2\n");

    aegis_exec_result_free(&r);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_mock_dry_run(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    exec.dry_run = true;

    TEST_ASSERT(exec.dry_run);

    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_mock:\n");
    RUN_TEST(test_mock_records_execute);
    RUN_TEST(test_mock_records_sudo);
    RUN_TEST(test_mock_write_and_read_file);
    RUN_TEST(test_mock_preconfigured_response);
    RUN_TEST(test_mock_dry_run);
    TEST_REPORT();
}
