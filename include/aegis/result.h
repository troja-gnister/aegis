#ifndef AEGIS_RESULT_H
#define AEGIS_RESULT_H

#include <stdbool.h>

typedef enum {
    AEGIS_OK,
    AEGIS_WARN,
    AEGIS_FAIL,
    AEGIS_SKIP
} aegis_status_t;

typedef struct {
    aegis_status_t status;
    char          *message;
    char         **actions;
    int            action_count;
    int            _action_cap;  /* internal capacity */
} aegis_result_t;

aegis_result_t aegis_result_ok(const char *msg);
aegis_result_t aegis_result_warn(const char *msg);
aegis_result_t aegis_result_fail(const char *msg);
aegis_result_t aegis_result_skip(const char *msg);
void           aegis_result_add_action(aegis_result_t *res, const char *action);
void           aegis_result_free(aegis_result_t *res);

#endif
