#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/systemd_hardening.h>
#include <aegis/mock.h>
#include <aegis/config.h>

static int test_systemd_hardening_apply_disabled(void) {
    aegis_systemd_hardening_config_t cfg = {
        .enabled = false,
        .auto_discover = false,
        .profiles = NULL,
        .profile_count = 0
    };
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_systemd_hardening_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    TEST_ASSERT_EQ(aegis_mock_call_count(&mock), 0);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_systemd_hardening_apply_ssh(void) {
    char *services[] = {"ssh"};
    aegis_systemd_hardening_config_t cfg = {
        .enabled = true,
        .auto_discover = false,
        .profiles = services,
        .profile_count = 1
    };
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_systemd_hardening_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(res.action_count > 0);

    /* Verify daemon-reload was called */
    TEST_ASSERT(aegis_mock_was_called(&mock, "daemon-reload"));

    /* Verify the drop-in config was written to the correct path */
    char *content = exec.read_file(
        "/etc/systemd/system/ssh.service.d/hardening.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "ProtectSystem=strict") != NULL);
    TEST_ASSERT(strstr(content, "NoNewPrivileges=yes") != NULL);
    free(content);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_systemd_hardening_apply_auto_discover(void) {
    aegis_systemd_hardening_config_t cfg = {
        .enabled = true,
        .auto_discover = true,
        .profiles = NULL,
        .profile_count = 0
    };
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_systemd_hardening_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* Should have called systemd-analyze security for discovery */
    TEST_ASSERT(aegis_mock_was_called(&mock, "systemd-analyze"));

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_systemd_hardening_apply_dropin_content(void) {
    char *services[] = {"nginx", "postgresql"};
    aegis_systemd_hardening_config_t cfg = {
        .enabled = true,
        .auto_discover = false,
        .profiles = services,
        .profile_count = 2
    };
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    aegis_result_t res = aegis_systemd_hardening_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);

    /* Verify drop-in for nginx */
    char *nginx_conf = exec.read_file(
        "/etc/systemd/system/nginx.service.d/hardening.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(nginx_conf);
    TEST_ASSERT(strstr(nginx_conf, "PrivateTmp=yes") != NULL);
    TEST_ASSERT(strstr(nginx_conf, "ProtectHome=yes") != NULL);
    free(nginx_conf);

    /* Verify drop-in for postgresql */
    char *pg_conf = exec.read_file(
        "/etc/systemd/system/postgresql.service.d/hardening.conf", exec.ctx);
    TEST_ASSERT_NOT_NULL(pg_conf);
    free(pg_conf);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_systemd_hardening_status(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_mock_add_response(&mock, "systemd-analyze", 0,
        "NAME       EXPOSURE\nsshd.service 7.2 MEDIUM\n");

    aegis_result_t res = aegis_systemd_hardening_status(&exec);
    TEST_ASSERT(res.status == AEGIS_OK || res.status == AEGIS_WARN);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_systemd_hardening:\n");
    RUN_TEST(test_systemd_hardening_apply_disabled);
    RUN_TEST(test_systemd_hardening_apply_ssh);
    RUN_TEST(test_systemd_hardening_apply_auto_discover);
    RUN_TEST(test_systemd_hardening_apply_dropin_content);
    RUN_TEST(test_systemd_hardening_status);
    TEST_REPORT();
}
