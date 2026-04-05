#define _POSIX_C_SOURCE 200809L
#include <aegis/result.h>
#include <stdlib.h>
#include <string.h>

static aegis_result_t make_result(aegis_status_t status, const char *msg) {
    aegis_result_t res = {0};
    res.status = status;
    res.message = strdup(msg);
    return res;
}

aegis_result_t aegis_result_ok(const char *msg)   { return make_result(AEGIS_OK, msg); }
aegis_result_t aegis_result_warn(const char *msg)  { return make_result(AEGIS_WARN, msg); }
aegis_result_t aegis_result_fail(const char *msg)  { return make_result(AEGIS_FAIL, msg); }
aegis_result_t aegis_result_skip(const char *msg)  { return make_result(AEGIS_SKIP, msg); }

void aegis_result_add_action(aegis_result_t *res, const char *action) {
    if (res->action_count >= res->_action_cap) {
        int new_cap = res->_action_cap == 0 ? 4 : res->_action_cap * 2;
        res->actions = realloc(res->actions, (size_t)new_cap * sizeof(char *));
        res->_action_cap = new_cap;
    }
    res->actions[res->action_count++] = strdup(action);
}

void aegis_result_free(aegis_result_t *res) {
    free(res->message);
    for (int i = 0; i < res->action_count; i++) {
        free(res->actions[i]);
    }
    free(res->actions);
    res->message = NULL;
    res->actions = NULL;
    res->action_count = 0;
    res->_action_cap = 0;
}
