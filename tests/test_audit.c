#define _POSIX_C_SOURCE 200809L
#include "test.h"
#include <aegis/modules/audit.h>
#include <aegis/mock.h>

static int test_audit_apply_disabled(void) {
    aegis_audit_config_t cfg = {.enabled = false, .profile = "standard"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_audit_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_audit_apply_standard(void) {
    aegis_audit_config_t cfg = {.enabled = true, .profile = "standard"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_audit_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT(aegis_mock_was_called(&mock, "augenrules"));
    TEST_ASSERT(exec.file_exists("/etc/audit/rules.d/aegis.rules", exec.ctx));
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_audit_apply_stig(void) {
    aegis_audit_config_t cfg = {.enabled = true, .profile = "stig"};
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);
    aegis_result_t res = aegis_audit_apply(&cfg, &exec);
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    char *content = exec.read_file("/etc/audit/rules.d/aegis.rules", exec.ctx);
    TEST_ASSERT_NOT_NULL(content);
    TEST_ASSERT(strstr(content, "-e 2") != NULL); /* stig-only immutable flag */
    free(content);
    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_audit_verify_rules_loaded(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    /* Mock auditctl -l returning a set of rules that includes our expected ones */
    aegis_mock_add_response(&mock, "auditctl",  0,
        "-w /etc/passwd -p wa -k identity\n"
        "-w /etc/shadow -p wa -k identity\n"
        "-w /etc/group -p wa -k identity\n"
        "-w /etc/sudoers -p wa -k sudo_changes\n"
        "-a always,exit -F arch=b64 -S execve -k exec\n");
    /* systemctl is-active auditd -> active */
    aegis_mock_add_response(&mock, "systemctl", 0, "active\n");

    aegis_result_t res = aegis_audit_verify(&exec);
    /* All 5 rules + service = 6 checks, should be OK */
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    /* All actions should report PASS */
    bool found_fail = false;
    for (int i = 0; i < res.action_count; i++) {
        if (strncmp(res.actions[i], "FAIL:", 5) == 0) { found_fail = true; break; }
    }
    TEST_ASSERT(!found_fail);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

static int test_audit_verify_rules_missing(void) {
    aegis_mock_ctx_t mock = {0};
    aegis_executor_t exec = aegis_mock_executor(&mock);

    /* auditctl -l returns empty (no rules loaded) */
    aegis_mock_add_response(&mock, "auditctl",  0, "");
    /* service not active */
    aegis_mock_add_response(&mock, "systemctl", 1, "inactive\n");

    aegis_result_t res = aegis_audit_verify(&exec);
    /* No rules + inactive service -> FAIL */
    TEST_ASSERT_EQ(res.status, AEGIS_FAIL);
    /* Should have per-item FAIL actions */
    bool found_fail = false;
    for (int i = 0; i < res.action_count; i++) {
        if (strncmp(res.actions[i], "FAIL:", 5) == 0) { found_fail = true; break; }
    }
    TEST_ASSERT(found_fail);

    aegis_result_free(&res);
    aegis_mock_ctx_free(&mock);
    return 0;
}

int main(void) {
    printf("test_audit:\n");
    RUN_TEST(test_audit_apply_disabled);
    RUN_TEST(test_audit_apply_standard);
    RUN_TEST(test_audit_apply_stig);
    RUN_TEST(test_audit_verify_rules_loaded);
    RUN_TEST(test_audit_verify_rules_missing);
    TEST_REPORT();
}
