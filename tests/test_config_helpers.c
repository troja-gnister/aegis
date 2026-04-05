#include "test.h"
#include <aegis/config_helpers.h>
#include <tomlc99/toml.h>

static toml_table_t *load_fixture(void) {
    FILE *fp = fopen("tests/fixtures/test_config.toml", "r");
    if (!fp) { fprintf(stderr, "Cannot open fixture\n"); return NULL; }
    char errbuf[256];
    toml_table_t *root = toml_parse_file(fp, errbuf, sizeof(errbuf));
    fclose(fp);
    return root;
}

static int test_get_bool(void) {
    toml_table_t *root = load_fixture();
    TEST_ASSERT_NOT_NULL(root);
    toml_table_t *tbl = toml_table_in(root, "simple");
    TEST_ASSERT(cfg_get_bool(tbl, "enabled", false) == true);
    TEST_ASSERT(cfg_get_bool(tbl, "missing", true) == true);
    TEST_ASSERT(cfg_get_bool(tbl, "missing", false) == false);
    toml_free(root);
    return 0;
}

static int test_get_string(void) {
    toml_table_t *root = load_fixture();
    TEST_ASSERT_NOT_NULL(root);
    toml_table_t *tbl = toml_table_in(root, "simple");
    char *val = cfg_get_string(tbl, "name", "fallback");
    TEST_ASSERT_STR_EQ(val, "test");
    free(val);
    char *def = cfg_get_string(tbl, "missing", "fallback");
    TEST_ASSERT_STR_EQ(def, "fallback");
    free(def);
    toml_free(root);
    return 0;
}

static int test_get_string_array(void) {
    toml_table_t *root = load_fixture();
    TEST_ASSERT_NOT_NULL(root);
    toml_table_t *tbl = toml_table_in(root, "with_array");
    int count = 0;
    char **items = cfg_get_string_array(tbl, "items", &count);
    TEST_ASSERT_EQ(count, 3);
    TEST_ASSERT_STR_EQ(items[0], "alpha");
    TEST_ASSERT_STR_EQ(items[1], "bravo");
    TEST_ASSERT_STR_EQ(items[2], "charlie");
    cfg_free_string_array(items, count);
    int empty_count = 0;
    char **empty = cfg_get_string_array(tbl, "missing", &empty_count);
    TEST_ASSERT_EQ(empty_count, 0);
    TEST_ASSERT_NULL(empty);
    toml_free(root);
    return 0;
}

static int test_get_bool_null_table(void) {
    TEST_ASSERT(cfg_get_bool(NULL, "key", true) == true);
    TEST_ASSERT(cfg_get_bool(NULL, "key", false) == false);
    return 0;
}

int main(void) {
    printf("test_config_helpers:\n");
    RUN_TEST(test_get_bool);
    RUN_TEST(test_get_string);
    RUN_TEST(test_get_string_array);
    RUN_TEST(test_get_bool_null_table);
    TEST_REPORT();
}
