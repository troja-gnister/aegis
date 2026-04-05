# Aegis

Arch Linux security hardening CLI. Applies, audits, and verifies 19 security modules across kernel, filesystem, network, and application layers.

## Build

```bash
make        # Build aegis binary
make test   # Run all tests
make install
```

Requires: `gcc`, `make`. No external dependencies — TOML parser (`tomlc99`) is vendored.

## Usage

```bash
aegis harden                          # Apply all enabled modules
aegis harden sysctl apparmor          # Apply specific modules only
aegis harden --dry-run                # Preview without applying
aegis status                          # Show current hardening state
aegis verify                          # Compliance check (pass/fail)
aegis list                            # List all modules with priorities
aegis config validate                 # Validate config file
aegis config init                     # Generate default config
```

Global flags: `--dry-run`, `--verbose`, `--config PATH`

Default config: `/etc/aegis/aegis.toml`

## Modules

| Module | Priority | What It Hardens |
|--------|----------|-----------------|
| sysctl | 10 | Kernel parameters (minimal/standard/paranoid profiles) |
| mounts | 10 | /tmp, /dev/shm, /proc (noexec, nosuid, hidepid) |
| audit | 20 | auditd rules (minimal/standard/stig profiles) |
| kernel | 20 | linux-hardened kernel + lockdown mode |
| apparmor | 50 | Application confinement via AppArmor profiles |
| firejail | 60 | App sandboxing via symlinks |
| systemd_hardening | 60 | Service isolation via drop-in configs |
| usbguard | 100 | USB device access control |
| snapper | 100 | BTRFS snapshots with pacman hooks |
| secureboot | 100 | UEFI Secure Boot via sbctl |
| malloc | 100 | Hardened memory allocator (LD_PRELOAD) |
| flatpak_hardening | 100 | Flatpak permission restrictions |
| dns | 100 | DNSSEC + DNS-over-TLS via systemd-resolved |
| podman_rootless | 100 | Rootless container namespace setup |
| dropbear | 100 | Remote LUKS unlock via SSH in initramfs |
| archaudit | 100 | CVE monitoring via arch-audit |
| aide | 100 | File integrity monitoring |
| rkhunter | 100 | Rootkit/backdoor scanning |
| fail2ban | 100 | Intrusion prevention (minimal/standard/strict profiles) |

Modules run in priority order. Foundation modules (10-20) run first, MAC (50) next, then independent modules (100).

## Configuration

```toml
# /etc/aegis/aegis.toml

[sysctl]
enabled = true
profile = "standard"    # minimal | standard | paranoid

[kernel]
enabled = true
lockdown = "integrity"  # integrity | confidentiality

[apparmor]
enabled = true
profiles = ["arch", "apparmord"]

[fail2ban]
enabled = true
profile = "standard"    # minimal | standard | strict
```

See `config/aegis.toml.example` for the full configuration reference.

Complex per-item configuration uses convention directories:
- `/etc/aegis/rules.d/usbguard/` — Custom device rules
- `/etc/aegis/rules.d/fail2ban/` — Custom jail overrides
- `/etc/aegis/rules.d/flatpak/` — Per-app permission overrides
- `/etc/aegis/profiles.d/systemd/` — Custom service hardening profiles

## Architecture

Library + CLI model. Core logic lives in `libaegis.a`, CLI is a thin consumer.

- **Executor abstraction** — All system calls go through an interface (`aegis_executor_t`), enabling dry-run mode and mock-based testing
- **Module interface** — Each module implements `apply`, `status`, and `verify` operations
- **Priority-ordered runner** — Executes modules in dependency-safe order

## Known Limitations / Follow-ups

- [ ] Module `apply` functions do not check return values from `write_file`/`execute_sudo` — failures are silently ignored
- [ ] SystemExecutor has no subprocess timeout (the 120s timeout from the spec is not implemented)
- [ ] Some modules use fixed-size stack buffers for config file generation — should switch to dynamic allocation for large configs
- [ ] `verify` is identical to `status` in all modules — a stricter per-parameter compliance check is planned
- [ ] Convention directories (`/etc/aegis/rules.d/`, `/etc/aegis/profiles.d/`) are not yet read by modules
- [ ] No `--help` / `-h` flag (use `aegis` with no args for usage)

## License

MIT
