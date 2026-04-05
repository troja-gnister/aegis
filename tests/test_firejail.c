#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/firejail.h>
#include <aegis/mock.h>
#include <aegis/config.h>

static int test_firejail_apply_disabled(void) {
    aegis_firejail_config_t cfg = {.enabled = false, .apps = NULL, .app_count = 0, .aggressive = false};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_firejail_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    TEST_ASSERT_EQ(aegis_mock_call_count(&mock), 0);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_firejail_apply_with_apps(void) {
    char *apps[] = {"firefox", "chromium"};
    aegis_firejail_config_t cfg = {
        .enabled = true,
        .apps = apps,
        .app_count = 2,
        .aggressive = false
    };
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_firejail_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(res.action_count > 0);

    /* Should have called pacman to install firejail */
    TEST_ASSERT(aegis_mock_was_called(&mock, "pacman"));
    /* Should have created symlinks via ln -sf */
    TEST_ASSERT(aegis_mock_was_called(&mock, "ln"));

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_firejail_apply_symlink_paths(void) {
    char *apps[] = {"firefox"};
    aegis_firejail_config_t cfg = {
        .enabled = true,
        .apps = apps,
        .app_count = 1,
        .aggressive = false
    };
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_firejail_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);

    /* Verify the symlink action mentions the correct path */
    bool found_symlink = false;
    for (int i = 0; i < res.action_count; i++) {
        if (strstr(res.actions[i], "/usr/local/bin/firefox") != NULL) {
            found_symlink = true;
            break;
        }
    }
    TEST_ASSERT(found_symlink);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_firejail_status_not_installed(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    /* /usr/bin/firejail does NOT exist in mock by default */

    aegis_result_t res = aegis_firejail_status(&exec);
    TEST_ASSERT_EQ(res.status, AEGIS_FAIL);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_firejail_status_installed(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_mock_add_file(&mock, "/usr/bin/firejail", "");

    aegis_result_t res = aegis_firejail_status(&exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_firejail:\n");
    RUN_TEST(test_firejail_apply_disabled);
    RUN_TEST(test_firejail_apply_with_apps);
    RUN_TEST(test_firejail_apply_symlink_paths);
    RUN_TEST(test_firejail_status_not_installed);
    RUN_TEST(test_firejail_status_installed);
    TEST_REPORT();
}
