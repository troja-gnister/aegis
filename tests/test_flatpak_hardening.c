#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/flatpak_hardening.h>
#include <aegis/mock.h>

static int test_flatpak_hardening_apply_disabled(void) {
    aegis_flatpak_hardening_config_t cfg = {.enabled = false, .policy = "strict"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_flatpak_hardening_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_flatpak_hardening_apply_strict(void) {
    aegis_flatpak_hardening_config_t cfg = {.enabled = true, .policy = "strict"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_flatpak_hardening_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(aegis_mock_was_called(&mock, "flatpak"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "--nofilesystem=home"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "--nosocket=ssh-auth"));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_flatpak_hardening_apply_lockdown(void) {
    aegis_flatpak_hardening_config_t cfg = {.enabled = true, .policy = "lockdown"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_flatpak_hardening_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(aegis_mock_was_called(&mock, "flatpak"));
    /* lockdown includes both strict and additional restrictions */
    TEST_ASSERT(aegis_mock_was_called(&mock, "--nofilesystem=home"));
    TEST_ASSERT(aegis_mock_was_called(&mock, "--nofilesystem=host"));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_flatpak_hardening:\n");
    RUN_TEST(test_flatpak_hardening_apply_disabled);
    RUN_TEST(test_flatpak_hardening_apply_strict);
    RUN_TEST(test_flatpak_hardening_apply_lockdown);
    TEST_REPORT();
}
