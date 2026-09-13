# Phase 1 verification checkpoint

**Acceptance remains open.** All automated suites below passed, but final review found a remaining nested-mount boundary issue. Phase 1 and its feature rows are not Verified. This report records the tested revision, not a production-release certification.

## Build identity

| Field | Captured value |
| --- | --- |
| Tested and reviewed commit | `bff72f4927dadcd36b2d68db9bd77b59da4aad3b` |
| Evidence captured at | `2026-09-13T16:27:14Z` |
| Checkout | Fresh detached worktree; no copied environment, secrets, or generated mounts; Git status empty before installs and after all three suites |
| Host | macOS 26.6.2, build 25G83; Darwin arm64; Apple M4 Max, 14 CPUs, 36 GiB RAM |
| Docker | Client/server 29.3.1; Compose 5.1.1; engine aarch64, 14 CPUs, 37,769,228,288 bytes memory |
| Python | Host 3.13.13; container and Linux CI 3.13.15 |
| Dependencies | uv 0.12.8; Django 5.2.17; PostgreSQL 18.6; Node.js 24.20.0; Vitest 4.1.11; Vite 8.2.2; frozen Python/npm locks |
| Test deployment release | `phase1-e2e` |
| Migrated schema identity | `sha256:bb98af7d1456e07331d42a6a6558e260d5e2857c492383a2f96985f365a0580b` |

The schema/release pair was read from the successful disposable migration's structured output. The exact-head [Linux CI run](https://github.com/troja-gnister/aegis/actions/runs/34767752653) passed all four jobs on Ubuntu 24.04.

Locally inspected image identities after the final browser run, not published release tags:

| Logical image | Inspected SHA-256 identity |
| --- | --- |
| Backend, including the observed migration container | `25a09434d17611826c1e4ff8f603eab2591ce0d153a8d74e7d6aa43ccb6e61a5` |
| Gateway | `f8d678ae5bcfa760c0ab7a1d886874d3c69edf5e939a0c29f2eb00d6fcbce679` |
| PostgreSQL | `81a6ec8da0015af0bdfd7bbe695f9e5113e62b0572969bd8bd15e62a6b3e1cfd` |

The immutable build inputs are recorded in [images.lock](../../deploy/images.lock):

| Input | Version | SHA-256 digest |
| --- | --- | --- |
| Python | 3.13.15-slim-trixie | `881d80734ee05dca6f7f42dcb080975652a53c7eda9ba1f03bb8da31aa6a6ec2` |
| uv | 0.12.8 | `d1cbaeadc234fe19c0d93daabcf5e98738cd93c6d1dd4918ef6aa30735feb23a` |
| Node | 24.20.0-bookworm-slim | `ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e` |
| Nginx unprivileged | 1.30.4-alpine | `45ce1e2e699234253d1def7baa96218a5d00b498d1ba0cbb1a17b6bdf73d1351` |
| PostgreSQL | 18.6-alpine | `d3e1620b530c944afa6e887d22eb899824da68e19c52024bf98f5220c88a65b2` |
| Caddy | 2.11.4-alpine | `5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648` |

## Commands and results

With uv 0.12.8 and Node 24.20.0 selected on `PATH`, these commands ran sequentially from the fresh checkout. Durations were captured with `/usr/bin/time -p`; they include setup/build work and are not product performance benchmarks.

| Exact command | Result | Captured tests and duration |
| --- | --- | --- |
| `/usr/bin/time -p make verify` | Exit 0 | 575 backend tests in 42.48 s; 43 frontend tests in 1.67 s; total 78.20 s |
| `/usr/bin/time -p make verify-compose` | Exit 0 | 206 deployment tests passed, 8 qualified Docker Desktop skips, in 165.70 s; total 181.46 s |
| `/usr/bin/time -p make test-e2e` | Exit 0 | Eight browser cases passed; credential-diagnostic checks passed in both browsers; total 44.33 s |
| `git diff --check` | Exit 0 | No whitespace errors |
| `git status --short` | Exit 0 | Empty after verification |

`make verify` also passed locked dependency checks, Ruff, mypy across 153 source files, Django system checks, migration-drift checks, ESLint, TypeScript, and the production frontend build. npm reported zero audit vulnerabilities, alongside development-dependency deprecation notices for ESLint 9.39.5 and whatwg-encoding 3.1.1; this is not a comprehensive dependency security audit.

The eight local skips are only the mode-0000/0111 synthetic-root cases where Docker Desktop rejects the host bind before the application attester can execute. All four mode-0444 role cases execute locally. Linux CI passed **all 214 deployment tests without these skips**, in 109.65 s. Its backend passed 575 tests in 42.34 s, frontend passed 43 tests in 3.59 s, and all eight browser cases passed.

Browser projects are `mobile-chromium` and `mobile-webkit`, four cases each, using the locked Playwright package. The default viewport is 390 × 844 with touch/mobile emulation; assertions also exercise width 320, minimum 44 × 44 controls, keyboard activation, reduced motion, and dark color-scheme behavior.

## Acceptance gate

| Gate | Result | Evidence |
| --- | --- | --- |
| 1. File-secret administrator bootstrap | Passed | [Bootstrap command tests](../../backend/tests/integration/identity/test_bootstrap_command.py), [bootstrap service tests](../../backend/tests/integration/identity/test_bootstrap_admin.py), and the actual one-off bootstrap in [browser setup](../../scripts/test-e2e.sh) |
| 2. CSRF, credential login, refresh, revocable sessions | Passed | [Authentication integration](../../backend/tests/integration/identity/test_auth_api.py), [shared admin policy](../../backend/tests/integration/identity/test_admin_auth_policy.py), [mobile journeys](../../frontend/e2e/phase1.spec.ts) |
| 3. Direct/group grants expose only active authorized roots | Passed | [Root selectors](../../backend/tests/integration/roots/test_selectors.py), [root API](../../backend/tests/integration/roots/test_root_api.py), Alice direct-grant and Bob group-grant browser cases |
| 4. Platform superuser has no implicit product roots | Passed | Root API test and ungranted-administrator browser case |
| 5. Anonymous, cross-root, stale-epoch, protected-location rejection | Passed | Authentication/root API tests, [authorization locking](../../backend/tests/integration/operations/test_authorization_locking.py), [gateway tests](../../tests/deployment/test_gateway_http.py), browser protected-URL cases |
| 6. Deployment-declared, alias-safe, consistent original mounts | **Open** | [Mount regressions](../../backend/tests/unit/aegisctl), [real Docker mount tests](../../tests/deployment/test_rendered_mounts.py) pass, but the descendant-mount topology below is not covered and fails independent review |
| 7. Heartbeats and immutable, role-scoped worker primitives | Passed | [Actual database-role tests](../../tests/deployment/test_database_roles.py), [immutable migrations](../../backend/tests/integration/operations/test_database_immutability_migrations.py), [leases](../../backend/tests/integration/operations/test_leases.py), healthy workers in the browser stack |
| 8. Append-only authentication, administration, and grant audit | Passed | [Audit integration](../../backend/tests/integration/audit), [root services](../../backend/tests/integration/roots/test_services.py), shared admin-policy tests, actual-role write denials |
| 9. Dark mobile shell and private logout/history restoration | Passed | [Frontend auth tests](../../frontend/src/features/auth), mobile Chromium/WebKit journeys |
| 10. Clean locked backend/frontend/browser/deployment/migration checks | Passed | The commands and exact-head Linux jobs above; no tracked/untracked output drift |

## Remaining safety gate

Final scoped review accepted the administrative authentication/password-route fixes, effective root-access checks, and safe timeout reporting. It identified one remaining Important issue shared by original-artifact protection and physical root alias detection: the host checks consider each root's selected mount, but not descendant mounts below it.

For example, if an external directory is also bind-mounted inside an original root, an artifact destination in that external directory can name the same bytes as a file visible inside the original. The current host output guard can accept that destination. Root preflight can likewise accept two roots sharing content through that nested mount. Non-writing probes with synthetic mount records confirmed both paths; the artifact writer was intercepted before any filesystem write. No actual user files or host mounts were modified.

**Do not use host preflight/render with original roots containing nested mounts until this boundary is resolved.** The deployed original bind declarations remain read-only and web receives no originals; there is no current original-file editing/deletion API or job. However, host setup tools run with the invoking operator's authority, so container mount restrictions do not remove this host-tool risk.

The proposed bounded resolution is to reject nested-mount roots and require separately declared mount slots until their complete topology can be handled. Supporting descendant mount identities now is the alternative. The policy choice and implementation remain open; documentation alone does not close this gate.

## Scope and limitations

- This is a foundation checkpoint: authentication, administration, granted root cards, durable job primitives, health, and deployment boundaries. File indexing/browsing, uploads/versions, media/document viewers and editors, and optional local models are still planned.
- No million-entry/50,000-child performance certification was performed. Test timings above are not the [performance contract](../../README.md#performance-contract).
- Mobile browser emulation is not certification on physical iPhones or Android devices. A synthetic persisted `pageshow` exercises restoration handling; it does not prove a real browser back-forward-cache restoration occurred.
- TLS tests use a disposable local CA. Public ACME issuance and internet-facing release hardening were not exercised.
- Web, processing, and database roles use internal networks with outbound-denial checks. The ingress gateway joins an external edge network and **can make outbound connections while holding read-only originals**; the optional public TLS front also requires ACME egress. Stronger gateway outbound isolation needs additional front-proxy or host-firewall design and validation.
- Access checks establish root readability/traversability at attestation time, not readability of every descendant or immunity to later operator topology changes.
- Original-root tests used tracked empty browser fixtures and disposable synthetic trees, never user libraries. Cleanup removed only owned temporary test resources, including the four disposable browser-test volumes; their synthetic data was not retained. The existing application database volume was not mounted, deleted, or recreated, and its creation identity remained unchanged.

See the [development guide](../development.md), [operation runbook](../operations/phase-1-deployment.md), and [task ledger](../superpowers/plans/2026-08-31-phase-1-secure-platform-foundation.md#implementation-status) for the reproducible workflow and remaining work.
