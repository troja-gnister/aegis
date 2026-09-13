# Phase 1 verification

**Phase 1 acceptance passed.** All ten foundation gates, the three fresh-checkout verification suites, and exact-head Linux CI passed. Independent scoped review accepted the final nested-mount restriction with no remaining findings. This verifies the secure-platform foundation at the revision below, not the complete drive/media product or a production release.

## Build identity

| Field | Captured value |
| --- | --- |
| Tested and reviewed commit | `6b37148c6ba6b4e7a5f986f9ed935b528896000a` |
| Evidence captured at | `2026-09-13T17:45:42Z` |
| Checkout | Fresh detached worktree; no copied environment, secrets, or generated mounts; Git status empty before installs and after all three suites |
| Host | macOS 26.6.2, build 25G83; Darwin arm64; Apple M4 Max, 14 CPUs, 36 GiB RAM |
| Docker | Client/server 29.3.1; Compose 5.1.1; engine aarch64, 14 CPUs, 37,769,228,288 bytes memory |
| Python | Host 3.13.13; container and Linux CI 3.13.15 |
| Dependencies | uv 0.12.8; Django 5.2.17; PostgreSQL 18.6; Node.js 24.20.0; Vitest 4.1.11; Vite 8.2.2; frozen Python/npm locks |
| Test deployment release | `phase1-e2e` |
| Migrated schema identity | `sha256:bb98af7d1456e07331d42a6a6558e260d5e2857c492383a2f96985f365a0580b` |

The schema/release pair was read from the successful disposable migration's structured output. The exact-head [Linux CI run](https://github.com/troja-gnister/aegis/actions/runs/34771701746) passed all four jobs on Ubuntu 24.04. The subsequent documentation-only acceptance commit does not change the tested executable sources or locks.

Locally inspected image identities after the final browser run, not published release tags:

| Logical image | Inspected SHA-256 identity |
| --- | --- |
| Backend, including the observed migration container | `cd571a07bc8dd973f28db3f8ef4fb5b289b9474109020c7cd4d132fb862c61e2` |
| Gateway | `6e01bfab018d2735a3ecb68c8cc414390aa8b4ce7842b5722c5b29cbf7cfb08e` |
| PostgreSQL | `ca4f70cbf68780835af07eda6f73de611e484ad48e9d02adebbc0ac682c42518` |

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
| `/usr/bin/time -p make verify` | Exit 0 | 620 backend tests in 43.12 s; 43 frontend tests in 1.72 s; total 80.81 s |
| `/usr/bin/time -p make verify-compose` | Exit 0 | 227 deployment tests passed, 8 qualified Docker Desktop skips, in 183.82 s; total 570.40 s |
| `/usr/bin/time -p make test-e2e` | Exit 0 | Eight browser cases passed; credential-diagnostic checks passed in both browsers; total 42.28 s |
| `git diff --check` | Exit 0 | No whitespace errors |
| `git status --short` | Exit 0 | Empty after verification |

`make verify` also passed locked dependency checks, Ruff, mypy across 156 source files, Django system checks, migration-drift checks, ESLint, TypeScript, and the production frontend build. npm reported zero audit vulnerabilities, alongside development-dependency deprecation notices for ESLint 9.39.5 and whatwg-encoding 3.1.1; this is not a comprehensive dependency security audit.

The eight local skips are only the mode-0000/0111 synthetic-root cases where Docker Desktop rejects the host bind before the application attester can execute. All four mode-0444 role cases and all 21 new nested-mount deployment cases execute locally. Linux CI passed **all 235 deployment tests without skips**, in 137.40 s. Its backend passed 620 tests in 56.39 s, frontend passed 43 tests in 3.17 s, and all eight browser cases passed.

Browser projects are `mobile-chromium` and `mobile-webkit`, four cases each, using the locked Playwright package. The default viewport is 390 × 844 with touch/mobile emulation; assertions also exercise width 320, minimum 44 × 44 controls, keyboard activation, reduced motion, and dark color-scheme behavior.

## Acceptance gate

| Gate | Result | Evidence |
| --- | --- | --- |
| 1. File-secret administrator bootstrap | Passed | [Bootstrap command tests](../../backend/tests/integration/identity/test_bootstrap_command.py), [bootstrap service tests](../../backend/tests/integration/identity/test_bootstrap_admin.py), and the actual one-off bootstrap in [browser setup](../../scripts/test-e2e.sh) |
| 2. CSRF, credential login, refresh, revocable sessions | Passed | [Authentication integration](../../backend/tests/integration/identity/test_auth_api.py), [shared admin policy](../../backend/tests/integration/identity/test_admin_auth_policy.py), [mobile journeys](../../frontend/e2e/phase1.spec.ts) |
| 3. Direct/group grants expose only active authorized roots | Passed | [Root selectors](../../backend/tests/integration/roots/test_selectors.py), [root API](../../backend/tests/integration/roots/test_root_api.py), Alice direct-grant and Bob group-grant browser cases |
| 4. Platform superuser has no implicit product roots | Passed | Root API test and ungranted-administrator browser case |
| 5. Anonymous, cross-root, stale-epoch, protected-location rejection | Passed | Authentication/root API tests, [authorization locking](../../backend/tests/integration/operations/test_authorization_locking.py), [gateway tests](../../tests/deployment/test_gateway_http.py), browser protected-URL cases |
| 6. Deployment-declared, alias-safe, consistent original mounts | Passed | [Leaf-root policy tests](../../backend/tests/unit/aegisctl/test_leaf_mount_policy.py), [native host topology tests](../../backend/tests/unit/aegisctl/test_darwin_topology.py), [nested-mount Docker tests](../../tests/deployment/test_nested_mounts.py), and [existing mount/access tests](../../tests/deployment/test_rendered_mounts.py); scoped review accepted the restriction below |
| 7. Heartbeats and immutable, role-scoped worker primitives | Passed | [Actual database-role tests](../../tests/deployment/test_database_roles.py), [immutable migrations](../../backend/tests/integration/operations/test_database_immutability_migrations.py), [leases](../../backend/tests/integration/operations/test_leases.py), healthy workers in the browser stack |
| 8. Append-only authentication, administration, and grant audit | Passed | [Audit integration](../../backend/tests/integration/audit), [root services](../../backend/tests/integration/roots/test_services.py), shared admin-policy tests, actual-role write denials |
| 9. Dark mobile shell and private logout/history restoration | Passed | [Frontend auth tests](../../frontend/src/features/auth), mobile Chromium/WebKit journeys |
| 10. Clean locked backend/frontend/browser/deployment/migration checks | Passed | The commands and exact-head Linux jobs above; no tracked/untracked output drift |

## Nested-mount safety resolution

The earlier checkpoint at `bff72f4` left one shared original-artifact/root-alias issue open: an external directory mounted inside an original root could make an apparently external artifact destination or separately declared root refer to original content. The user approved rejecting this topology on September 13, 2026. Implementation `2aa0c8f` and operator guidance `6b37148` close that boundary; the prior administrative authentication/password-route fixes, effective root-access checks, and safe timeout reporting remain accepted.

**Supported original roots contain no descendant filesystem mounts.** The root itself may be a mountpoint and ordinary subdirectories are allowed. Declare non-overlapping leaf roots separately; declaring a containing root alongside its nested mount is not a workaround. Read-only and writable descendant mounts are both rejected. Full nested-mount support remains deferred.

Host preflight and independent artifact-output guards reject nested-mount roots before generated writes. Linux uses bounded mount metadata; macOS uses native mount metadata and file-descriptor path forms to account for Data-volume firmlink aliases. Unavailable, ambiguous, or unsupported host topology fails closed, without enumerating original contents. The container observer retains descendant mount records, and backend/gateway attestation rejects them while preserving identity, read-only, and effective-access checks. Operators must keep mount topology stable during setup and rerun preflight/render and recreate affected containers after changes.

Regression work added 45 mount-unit tests and 21 actual-Docker deployment cases. Failing pre-fix tests demonstrated the unsafe paths before the implementation was changed. Independent review repeated both original non-writing reproductions: both now reject, with zero artifact-writer calls in the output probe. Review found no remaining Critical, Important, or Minor findings in the amendment and accepted its specification and implementation. No user files or host mounts were modified by these probes.

## Scope and limitations

- This is foundation acceptance: authentication, administration, granted root cards, durable job primitives, health, and deployment boundaries. File indexing/browsing, uploads/versions, media/document viewers and editors, and optional local models are still planned. There is no original-file editing/deletion API or job; all deployed original binds remain read-only and web receives no originals.
- No million-entry/50,000-child performance certification was performed. Test timings above are not the [performance contract](../../README.md#performance-contract).
- Mobile browser emulation is not certification on physical iPhones or Android devices. A synthetic persisted `pageshow` exercises restoration handling; it does not prove a real browser back-forward-cache restoration occurred.
- TLS tests use a disposable local CA. Public ACME issuance and internet-facing release hardening were not exercised.
- Web, processing, and database roles use internal networks with outbound-denial checks. The ingress gateway joins an external edge network and **can make outbound connections while holding read-only originals**; the optional public TLS front also requires ACME egress. Stronger gateway outbound isolation needs additional front-proxy or host-firewall design and validation.
- Access checks establish root readability/traversability at attestation time, not readability of every descendant or immunity to later operator topology changes.
- Native macOS behavior was exercised on Apple silicon. Intel-specific API selection has regression coverage but was not tested on physical Intel Mac hardware. This is not a guarantee against privileged concurrent mount or symlink changes.
- Original-root tests used tracked empty browser fixtures and disposable synthetic trees, never user libraries. Cleanup removed only owned temporary test resources, including the four disposable browser-test volumes; their synthetic data was not retained. The existing application database volume was not mounted, deleted, or recreated, and its creation identity remained unchanged.

See the [development guide](../development.md), [operation runbook](../operations/phase-1-deployment.md), and [task ledger](../superpowers/plans/2026-08-31-phase-1-secure-platform-foundation.md#implementation-status) for the reproducible workflow and remaining work.
