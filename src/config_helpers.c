#define _POSIX_C_SOURCE 200809L
#include <aegis/config_helpers.h>
#include <stdlib.h>
#include <string.h>

bool cfg_get_bool(toml_table_t *tbl, const char *key, bool def) {
    if (!tbl) return def;
    toml_datum_t d = toml_bool_in(tbl, key);
    return d.ok ? (bool)d.u.b : def;
}

char *cfg_get_string(toml_table_t *tbl, const char *key, const char *def) {
    if (!tbl) return strdup(def);
    toml_datum_t d = toml_string_in(tbl, key);
    if (d.ok) return d.u.s;
    return strdup(def);
}

char **cfg_get_string_array(toml_table_t *tbl, const char *key, int *count) {
    *count = 0;
    if (!tbl) return NULL;
    toml_array_t *arr = toml_array_in(tbl, key);
    if (!arr) return NULL;
    int n = toml_array_nelem(arr);
    if (n == 0) return NULL;
    char **out = malloc((size_t)n * sizeof(char *));
    for (int i = 0; i < n; i++) {
        toml_datum_t d = toml_string_at(arr, i);
        out[i] = d.ok ? d.u.s : strdup("");
    }
    *count = n;
    return out;
}

void cfg_free_string_array(char **arr, int count) {
    if (!arr) return;
    for (int i = 0; i < count; i++) free(arr[i]);
    free(arr);
}
