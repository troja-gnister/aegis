#ifndef AEGIS_TEST_H
#define AEGIS_TEST_H

#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static int _test_passes = 0;
static int _test_fails = 0;

#define TEST_ASSERT(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "  FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        return 1; \
    } \
} while (0)

#define TEST_ASSERT_EQ(a, b) do { \
    long _a = (long)(a), _b = (long)(b); \
    if (_a != _b) { \
        fprintf(stderr, "  FAIL %s:%d: %ld != %ld\n", __FILE__, __LINE__, _a, _b); \
        return 1; \
    } \
} while (0)

#define TEST_ASSERT_STR_EQ(a, b) do { \
    if (strcmp((a), (b)) != 0) { \
        fprintf(stderr, "  FAIL %s:%d: \"%s\" != \"%s\"\n", __FILE__, __LINE__, (a), (b)); \
        return 1; \
    } \
} while (0)

#define TEST_ASSERT_NULL(a) do { \
    if ((a) != NULL) { \
        fprintf(stderr, "  FAIL %s:%d: expected NULL\n", __FILE__, __LINE__); \
        return 1; \
    } \
} while (0)

#define TEST_ASSERT_NOT_NULL(a) do { \
    if ((a) == NULL) { \
        fprintf(stderr, "  FAIL %s:%d: expected non-NULL\n", __FILE__, __LINE__); \
        return 1; \
    } \
} while (0)

#define RUN_TEST(fn) do { \
    printf("  %s... ", #fn); \
    if (fn() == 0) { \
        printf("OK\n"); \
        _test_passes++; \
    } else { \
        printf("FAIL\n"); \
        _test_fails++; \
    } \
} while (0)

#define TEST_REPORT() do { \
    printf("\n%d passed, %d failed\n", _test_passes, _test_fails); \
    return _test_fails > 0 ? 1 : 0; \
} while (0)

#endif
