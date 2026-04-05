#include "test.h"
#include <aegis/result.h>

static int test_result_ok(void) {
    aegis_result_t res = aegis_result_ok("applied sysctl");
    TEST_ASSERT_EQ(res.status, AEGIS_OK);
    TEST_ASSERT_STR_EQ(res.message, "applied sysctl");
    TEST_ASSERT_EQ(res.action_count, 0);
    TEST_ASSERT_NULL(res.actions);
    aegis_result_free(&res);
    return 0;
}

static int test_result_fail(void) {
    aegis_result_t res = aegis_result_fail("sysctl failed");
    TEST_ASSERT_EQ(res.status, AEGIS_FAIL);
    TEST_ASSERT_STR_EQ(res.message, "sysctl failed");
    aegis_result_free(&res);
    return 0;
}

static int test_result_skip(void) {
    aegis_result_t res = aegis_result_skip("sysctl disabled");
    TEST_ASSERT_EQ(res.status, AEGIS_SKIP);
    aegis_result_free(&res);
    return 0;
}

static int test_result_add_action(void) {
    aegis_result_t res = aegis_result_ok("done");
    aegis_result_add_action(&res, "Set kernel.kptr_restrict = 2");
    aegis_result_add_action(&res, "Wrote /etc/sysctl.d/99-aegis.conf");
    TEST_ASSERT_EQ(res.action_count, 2);
    TEST_ASSERT_STR_EQ(res.actions[0], "Set kernel.kptr_restrict = 2");
    TEST_ASSERT_STR_EQ(res.actions[1], "Wrote /etc/sysctl.d/99-aegis.conf");
    aegis_result_free(&res);
    return 0;
}

int main(void) {
    printf("test_result:\n");
    RUN_TEST(test_result_ok);
    RUN_TEST(test_result_fail);
    RUN_TEST(test_result_skip);
    RUN_TEST(test_result_add_action);
    TEST_REPORT();
}
