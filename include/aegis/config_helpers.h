#ifndef AEGIS_CONFIG_HELPERS_H
#define AEGIS_CONFIG_HELPERS_H

#include <stdbool.h>
#include <tomlc99/toml.h>

bool   cfg_get_bool(toml_table_t *tbl, const char *key, bool def);
char  *cfg_get_string(toml_table_t *tbl, const char *key, const char *def);
char **cfg_get_string_array(toml_table_t *tbl, const char *key, int *count);
void   cfg_free_string_array(char **arr, int count);

#endif
