#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/apparmor.h>
#include <aegis/mock.h>
#include <aegis/config.h>

static int test_apparmor_apply_disabled(void) {
    aegis_apparmor_config_t cfg = {.enabled = false, .profiles = NULL, .profile_count = 0};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_apparmor_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    TEST_ASSERT_EQ(aegis_mock_call_count(&mock), 0);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_apparmor_apply_with_profiles(void) {
    char *profiles[] = {"arch", "apparmord"};
    aegis_apparmor_config_t cfg = {
        .enabled = true,
        .profiles = profiles,
        .profile_count = 2
    };
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_apparmor_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(res.action_count > 0);

    /* Should have called pacman to install apparmor */
    TEST_ASSERT(aegis_mock_was_called(&mock, "pacman"));
    /* Should have called aa-enforce for each profile */
    TEST_ASSERT(aegis_mock_was_called(&mock, "aa-enforce"));

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_apparmor_apply_no_profiles(void) {
    aegis_apparmor_config_t cfg = {.enabled = true, .profiles = NULL, .profile_count = 0};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_apparmor_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Should still install and enable service even with no profiles */
    TEST_ASSERT(aegis_mock_was_called(&mock, "pacman"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "systemctl"));

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_apparmor_status(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_mock_add_response(&mock, "aa-status", 0,
        "apparmor module is loaded.\n34 profiles are loaded.\n");

    aegis_result_t res = aegis_apparmor_status(&exec);
    TEST_ASSERT(res.status == AEGIS_OK || res.status == AEGIS_FAIL);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_apparmor_verify(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_mock_add_response(&mock, "aa-status", 0,
        "apparmor module is loaded.\n34 profiles are loaded.\n");

    aegis_result_t res = aegis_apparmor_verify(&exec);
    /* verify now returns OK / WARN / FAIL depending on per-item pass rate */
    TEST_ASSERT(res.status == AEGIS_OK || res.status == AEGIS_WARN || res.status == AEGIS_FAIL);
    /* verify must produce per-item actions */
    TEST_ASSERT(res.action_count > 0);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_apparmor:\n");
    RUN_TEST(test_apparmor_apply_disabled);
    RUN_TEST(test_apparmor_apply_with_profiles);
    RUN_TEST(test_apparmor_apply_no_profiles);
    RUN_TEST(test_apparmor_status);
    RUN_TEST(test_apparmor_verify);
    TEST_REPORT();
}
