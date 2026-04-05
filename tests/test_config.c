#include "test.h"
#include <aegis/config.h>

static int test_config_load(void) {
    char err[256] = {0};
    aegis_config_t *cfg = aegis_config_load("tests/fixtures/test_config.toml", err, sizeof(err));
    TEST_ASSERT_NOT_NULL(cfg);
    TEST_ASSERT(cfg->sysctl.enabled);
    TEST_ASSERT_STR_EQ(cfg->sysctl.profile, "standard");
    TEST_ASSERT(!cfg->kernel.enabled);
    TEST_ASSERT_STR_EQ(cfg->kernel.lockdown, "integrity");
    TEST_ASSERT(cfg->mounts.enabled);
    TEST_ASSERT(cfg->mounts.harden_tmp);
    TEST_ASSERT(!cfg->mounts.harden_proc);
    TEST_ASSERT(cfg->audit.enabled);
    TEST_ASSERT_STR_EQ(cfg->audit.profile, "stig");
    TEST_ASSERT(cfg->apparmor.enabled);
    TEST_ASSERT_EQ(cfg->apparmor.profile_count, 2);
    TEST_ASSERT_STR_EQ(cfg->apparmor.profiles[0], "arch");
    TEST_ASSERT(cfg->firejail.enabled);
    TEST_ASSERT_EQ(cfg->firejail.app_count, 2);
    TEST_ASSERT(!cfg->firejail.aggressive);
    TEST_ASSERT(cfg->systemd_hardening.enabled);
    TEST_ASSERT(cfg->systemd_hardening.auto_discover);
    TEST_ASSERT(!cfg->usbguard.enabled);
    TEST_ASSERT_STR_EQ(cfg->usbguard.default_policy, "block");
    TEST_ASSERT(cfg->snapper.enabled);
    TEST_ASSERT_EQ(cfg->snapper.subvolume_count, 2);
    TEST_ASSERT(!cfg->secureboot.enabled);
    TEST_ASSERT(cfg->malloc_hardened.enabled);
    TEST_ASSERT(cfg->flatpak_hardening.enabled);
    TEST_ASSERT_STR_EQ(cfg->flatpak_hardening.policy, "strict");
    TEST_ASSERT(cfg->dns.enabled);
    TEST_ASSERT(cfg->dns.dnssec);
    TEST_ASSERT(cfg->dns.dot);
    TEST_ASSERT(cfg->podman_rootless.enabled);
    TEST_ASSERT(!cfg->dropbear.enabled);
    TEST_ASSERT(cfg->archaudit.enabled);
    TEST_ASSERT(!cfg->aide.enabled);
    TEST_ASSERT(cfg->rkhunter.enabled);
    TEST_ASSERT(cfg->fail2ban.enabled);
    TEST_ASSERT_STR_EQ(cfg->fail2ban.profile, "standard");
    aegis_config_free(cfg);
    return 0;
}

static int test_config_load_missing_file(void) {
    char err[256] = {0};
    aegis_config_t *cfg = aegis_config_load("/nonexistent.toml", err, sizeof(err));
    TEST_ASSERT_NULL(cfg);
    TEST_ASSERT(strlen(err) > 0);
    return 0;
}

static int test_config_defaults(void) {
    FILE *fp = fopen("/tmp/aegis_empty.toml", "w");
    fprintf(fp, "# empty\n");
    fclose(fp);
    char err[256] = {0};
    aegis_config_t *cfg = aegis_config_load("/tmp/aegis_empty.toml", err, sizeof(err));
    TEST_ASSERT_NOT_NULL(cfg);
    TEST_ASSERT(!cfg->sysctl.enabled);
    TEST_ASSERT_STR_EQ(cfg->sysctl.profile, "standard");
    TEST_ASSERT(!cfg->fail2ban.enabled);
    TEST_ASSERT_STR_EQ(cfg->fail2ban.profile, "standard");
    aegis_config_free(cfg);
    return 0;
}

int main(void) {
    printf("test_config:\n");
    RUN_TEST(test_config_load);
    RUN_TEST(test_config_load_missing_file);
    RUN_TEST(test_config_defaults);
    TEST_REPORT();
}
