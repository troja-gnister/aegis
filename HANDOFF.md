# Aegis continuation handoff — September 27, 2026

Resume in `/var/home/troja/Dev/aegis` on **`main`**. The Linux/Podman PostgreSQL correction (not the prerequisite's full acceptance) is committed, verified, and pushed to `origin/main`; full prerequisite acceptance is still open. Docker CI for the pushed head has not yet been inspected. The next first actions are, in order: **inspect the CI run for the pushed head**, then **Caddy application configuration**, then **indexer test-fixture compatibility**, then full gates (`make verify`, `make verify-compose`, `make test-e2e`) and Docker CI resolution, then **Task 13**. **18 tasks: 12 accepted, 6 remaining. Task 13 has not started.** Do not restart Tasks 1–12 or expand this package into the full rewrite.

## First actions in a new session

1. Read this file and `docs/development-handoff.md`. Check `git status --short --branch`, `git remote -v`, and recent history before editing. Fetch origin; confirm local `main` matches pushed `origin/main` at the documentation commit on top of `1bb14e4`, not diverged. Never reset, force-push, auto-stash, or discard local work. Investigate any additional changes beyond that pushed state.
2. The PostgreSQL prerequisite below is complete and independently reviewed; do not redispatch its correction. Verify the committed source against the manifests named in `progress.md` before building on it.
3. Inspect the CI run for the pushed head (the documentation commit on top of `1bb14e4`) and record its result; do not assume a result before it is observed.
4. Continue the prerequisite sequence: Caddy application configuration, then indexer test-fixture compatibility, then full gates and Docker CI resolution, then Task 13. Keep one source implementer active at a time; use independent specification/code-quality review and scoped correction review, as before.

Authoritative documents:

- [Public development handoff](docs/development-handoff.md)
- [README](README.md) and [development guide](docs/development.md)
- [Phase 2A.1 implementation plan and task ledger](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md)
- [Indexed-browser design](docs/superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md)
- [Phase 2 delivery design and ledger](docs/superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md)
- [Platform architecture and benchmark requirements](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md)
- [Linux/Podman evidence and limitations](docs/verification/linux-podman-prerequisite.md)

Follow applicable `AGENTS.md` instructions if present. None was found during the existing work. Read each task's complete requirements before implementation; this handoff does not replace them.

## Git checkpoint

`1bb14e4` (the independently reviewed PostgreSQL private-tmpfs correction, 10 files, staged hashes matching the final reviewed manifest `34e4de97c3f96334c734e935283155d04e5cf6a7f6ebf97b245a5c7e92d99b62` recorded in `progress.md`) and the following documentation commit are pushed to `origin/main`, on top of the prior pushed documentation checkpoint `6e276345a5fee5e75243510c74cf1f43badd8445` (records the September 24 pause handoff). Docker CI for this pushed head has not yet been inspected.

| Revision | Meaning |
| --- | --- |
| `1bb14e4d3d5e3ad8a8cda6e8c0d72fa927901dba` | Pushed to `origin/main` (with the following documentation commit): corrected PostgreSQL private-tmpfs prerequisite (10 files, D1 + R2–R5). Combined selection: 318 passed, 0 skipped. |
| `6e276345a5fee5e75243510c74cf1f43badd8445` | Prior pushed documentation checkpoint, now the parent of the pushed head above. |
| `f618bae3524befcffb2b907ff482be7ce0879330` | Prior pushed documentation checkpoint; records Podman merge/private-tmpfs evidence. |
| `520cf1f5bb9d5126953d33b0ce37751080ef7db8` | Latest committed application source before the PostgreSQL correction: Podman overlay contributes only the mask option, preserving base NNP. |
| `71da40ed34bbe05990093afbf12925df40462ccf` | Guarded duplicate-mask compatibility, with reviewed identity/behavior checks and focused actual mount evidence. |
| `163c02563d95de8e4e3930ae68377e946feef3b8` | Explicit local-engine/verification tooling; latest complete local `make verify` evidence. |
| `a82db75d9949170e1d22acfed0f08050f067edf2` | Last accepted Task 12 application change. |

`1bb14e4` and this documentation commit are pushed to `origin/main`; inspect the CI run for the pushed head once it is available. The complete correction chain (D1 review, diagnostic execution, R2–R5 briefs/reviews/manifests) is recorded chronologically in `.superpowers/sdd/2026-09-14-phase-2a-indexed-browser/progress.md`; do not redispatch it.

## PostgreSQL prerequisite: diagnosed and corrected

Diagnostic-quoting finding D1 (Compose serialization of the bootstrap diagnostic) is corrected and independently reviewed: the assembled bootstrap program is encoded for Compose exactly once, and the pre-start check compares both the rendered Compose form and the inspected literal entrypoint vector. A resource-free check using the actual selected provider's `config --format json` render was added.

The corrected diagnostic then ran in a container and located the original bootstrap `exit 1` at the helper's `/` ancestor check. An independently reviewed survey measured rootless Podman's read-only overlay root as owned `0:0` with mode `555`; the other 55 validation guards passed. Four independently reviewed corrections followed:

- **R2** accepts mode `755` (Docker) or `555` (measured rootless Podman) for `/` only, in the production helper.
- **R4** declares the canonical PostgreSQL socket tmpfs at `/run/postgresql` instead of the `/var/run` alias, because Podman's inspection drops a declaration made under that alias once the container has started.
- **R5** sets the live test's data tmpfs to Docker's default mode `1777` explicitly, because Podman applies Compose's `mode=0` literally.
- **R3** applies the same `/` rule to the direct role-init test wrapper, and gives the environment-probe fixture a data tmpfs, an `@integration` marker, and the production `DAC_OVERRIDE` capability.

The combined PostgreSQL selection passed **318 tests, 0 skipped, in 523 seconds** under rootless Podman 5.8.7, crun 1.28, enforcing SELinux; Ruff and mypy (228 sources) passed. The ten candidate files are committed as `1bb14e4` (`fix: prepare PostgreSQL private tmpfs for rootless Podman`); `1bb14e4` and the following documentation commit are pushed to `origin/main` (previously at `6e27634`).

Still open, before this prerequisite is fully accepted:

- A resource-free `tests/deployment -m "not integration"` run under Podman shows pre-existing failures that also reproduce on committed `6e27634`: a stale Docker-only no-new-privileges assertion for core services in `test_compose.py`, and four `test_rendered_mounts.py` mask/observer refusals. There are also failures in the still-unimplemented Caddy and indexer slices. None of these are PostgreSQL regressions.
- Docker CI for the pushed head has not yet been inspected.
- Full gates (`make verify`, `make verify-compose`, `make test-e2e`) have not run on this source.
- Full Podman application compatibility is not accepted.

Full chronological evidence, review reports, and manifests remain in `.superpowers/sdd/2026-09-14-phase-2a-indexed-browser/progress.md` and the files it names. Do not redispatch this correction.

## Subsequent prerequisite slices

**Caddy application configuration is unimplemented.** Read `podman-caddy-tmpfs-design.md` and `podman-caddy-implementation-brief.md`. A prepared implementation agent may be resumable as `/root/podman_caddy_configuration`; it received no implementation GO.

Isolated diagnostics already passed two native and two Compose generations with pinned image `ebbf5210d94567392591a0a07d61279a8be8c9041091c1713427ffa23e0f842b` (Linux amd64, Caddy 2.11.4), exact UID/GID 10001, absent file capabilities, zero kernel capabilities, NNP, empty distinct 16 MiB private mounts, 0700/1777 modes, protective flags, tiny write/read, empty recreation and the unchanged full strict mount parser. Fresh mask checks and exact cleanup passed. These probes used no server, host binds, secrets, network or published ports; OOM score remained inherited 100. They do not establish TLS, canonical configuration or general resource enforcement.

The intended narrow Podman overlay uses `!override` only for Caddy/Caddy-local's private `/config` and `/tmp` mounts, adding `U,notmpcopyup` there. Keep Docker declarations unchanged, durable `/data` unchanged, and the `520cf1f` mask-only overlay contribution. Precisely validate the before/after service and every target/option/size/mode/user; no global `tmpfs` comparison exemption. Verify actual provider profile rendering, server/autosave, local TLS and certificate durability after recreation. `U` is for these newly owned private tmpfs mounts only, never originals or bind mounts.

**Indexer test-fixture compatibility is unimplemented.** Read `podman-indexer-tmpfs-design.md`, `podman-indexer-cleanup-diagnosis.md` and `podman-indexer-tmpfs-implementation-brief.md` after Caddy is verified. Four native coordination test mounts need precise Podman options with Docker strings unchanged. Dynamic-user and 501:20 positive fixtures require actual metadata/coordination evidence; a deliberately misowned 0:0 fixture must remain misowned without `U`. Caddy's 10001 evidence does not prove these users work.

A separate concrete fake-runner reproduction found that four existing test cleanup fragments could accept and delete a same-name/same-label replacement. Fix these touched fixtures before executing them: record immutable image IID and workload CID at creation, validate the entire owned scope before the first deletion, refuse replacements/unknown creation/tag rebinding, remove only recorded identities, and confirm absence. Preserve diagnostics and the original failure on cleanup refusal. This is a newly evidenced test-fixture safety correction, not a reopening of accepted Task 7. Preserve actual orphan reaping, lease/heartbeat/replacement, read-only originals and source manifests; do not substitute an init process-name check.

After the prerequisite slices, run resource-using gates sequentially:

```bash
make verify
make verify-compose
make test-e2e
git diff --check
```

Docker CI failures must also be resolved. No prerequisite is accepted based only on fake tests, isolated probes, skipped security assertions or successful rendering.

## Host, locked tools and restart requirements

Observed host: Fedora Silverblue **44.20260922.0**, Intel i7-1355U (10 cores / 12 threads), about 30 GiB RAM, Btrfs. The host was rebooted on September 27, 2026; the booted kernel is now **7.2.7-200.fc44.x86_64** (previously 7.2.6-200.fc44.x86_64). Only the kernel has been reconfirmed since the reboot — recheck the distribution build, free space, and inode capacity before large-fixture work. `/tmp` is a 16 GiB tmpfs and is unsuitable for the million-entry database/source workload. This computer is **not the calibrated N100 reference host**.

Rootless Podman **5.8.7**, crun **1.28**, cgroup v2/systemd, netavark **1.17.2**, aardvark **1.17.1**, pasta; SELinux is **Enforcing**. Rootless container UID 0 maps to host UID 1000; subordinate IDs start at 524288. The selected external provider is Docker Compose **2.39.4**, SHA-256 `7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4`. Native engine commands explicitly use `podman --remote=false`; provider API 1.44 versus Podman API 5.8.7 is recorded valid behavior. Revalidate current versions and the guarded policy before launches.

Use the existing locked local toolchain, not the host's Python 3.14 / Node 22:

```bash
export PATH="/var/home/troja/Dev/aegis/.superpowers/toolchain/node-v24.20.0-linux-x64/bin:/var/home/troja/Dev/aegis/.superpowers/toolchain/uv-x86_64-unknown-linux-gnu:/usr/local/bin:/usr/bin:/bin"
export UV_CACHE_DIR=/var/home/troja/Dev/aegis/.superpowers/toolchain/uv-cache
export UV_PYTHON_INSTALL_DIR=/var/home/troja/Dev/aegis/.superpowers/toolchain/python
export NPM_CONFIG_CACHE=/var/home/troja/Dev/aegis/.superpowers/toolchain/npm-cache
export AEGIS_CONTAINER_ENGINE=podman
export PODMAN_COMPOSE_PROVIDER=/var/home/troja/Dev/aegis/.superpowers/toolchain/downloads/docker-compose-linux-x86_64
export VITEST_MAX_WORKERS=1
```

Python is **3.13.15**, uv **0.12.8**, Node **24.20.0**, PostgreSQL **18**; use tracked `uv.lock` and npm lock. Do not upgrade dependencies incidentally. `VITEST_MAX_WORKERS=1` belongs to measured local verification; it does not erase earlier frontend timeout evidence.

**`AEGIS_PODMAN_SOCKET` must point to a newly created, identity-recorded, private rootless Unix service before resource tests.** Do not reuse an old socket path or owner record as a live connection. Create only a fresh 0700 owned directory and 0600 socket with restrictive umask, record process/directory/socket identities, use foreground `podman --remote=false system service --time=0 unix://…`, and stop it with identity checks at the next pause. Preserve the committed executable/socket/provider/image attestation and fresh four-profile behavior checks. Do not enable a persistent/rootful/TCP API or mount its socket into application containers.

Locked Playwright is **1.63.0**; synthetic Chromium **153.0.8010.12** checks passed locally. Host WebKit **26.6** lacked the required ICU/JPEG/libav libraries. A separately reviewed isolated Noble runtime passed a synthetic WebKit probe and was removed. Its ignored controller is `.superpowers/toolchain/browser-runtime.py`; inspect its ownership records and lifecycle before reuse. The validated `E2E_WEBKIT_WS_ENDPOINT` is loopback-only with a bounded port and 32-hex path, applies only to WebKit, and rejects `PW_TEST_CONNECT*` overrides. Chromium keeps its local sandbox. The trusted development-browser host-network exception is not application no-egress evidence. No real Podman application browser gate has passed.

## Resource state

A private rootless API service is running for this session: identity record `.superpowers/toolchain/podman-api-owner-x0e4r4i0.json`. Root will stop this service and verify its process/directory/socket identities at the next pause; do not reuse this identity as a live connection in a later session. Create a fresh, identity-recorded 0700 directory and 0600 socket before any resource test in a new session.

Prior sessions' owned API services (sessions/records `3046`, `41151`, `89377`, and `7l5gxcb_`) are all stopped or ended by the host reboot; their identity records are archived under `.superpowers/toolchain/`. Native `podman --remote=false ps --format json` returned `[]` and reserved ports **18080** and **55432** had no listeners at last check.

**Retained resources are not cleanup targets.** The first failed deployment run left 18 volumes and 23 test networks, plus the unrelated default Podman network. Two separate leaks occurred during PostgreSQL correction work, each independently identified by exact creation time/label absence and removed by exact name, with the retained-volume count returning to 18 both times: the O4 root minimal-compose probe's own image `VOLUME` created two anonymous volumes, both removed; separately, the environment-probe fixture (missing a `/var/lib/postgresql` tmpfs) leaked one anonymous volume, which was also removed — R3 has since fixed that fixture. Do not delete by name, label, prefix, or a newly relaxed admission rule otherwise. Older refusal-test diagnostics, build caches, and recorded images are retained. Unknown contents or changed identities must stop cleanup and preserve evidence. No broad container/volume/system prune or indiscriminate Compose teardown is authorized.

No user library, original-file tree, operator database, preview, production deployment, release tag, or million-entry fixture was touched as part of this work.

## Verification and CI state

Latest complete local `make verify` still belongs to **`163c025`**, not the PostgreSQL-corrected candidate: 1,452 backend tests, 147 frontend tests across 13 files, locked installs, Ruff, mypy (225 sources), Django checks/migrations, frontend lint/types/build and whitespace checks passed. The backend took 747.16s; frontend took 28.06s with one worker. Log: `.superpowers/toolchain/make-verify-round5-20260924.log`. `make verify`, `make verify-compose`, and `make test-e2e` have not been rerun on the PostgreSQL-corrected source; run them, in that order, as part of the next full-gates slice.

The combined PostgreSQL selection at `1bb14e4` passed 318 tests with 0 skipped in 523 seconds under rootless Podman, with Ruff and mypy (228 sources) clean; full log `.superpowers/toolchain/podman-postgres-combined-20260927.log`. A resource-free run of `tests/deployment -m "not integration"` under Podman also reproduced pre-existing failures present on committed `6e27634` too: a stale Docker-only no-new-privileges assertion in `test_compose.py`'s core-services check, and four `test_rendered_mounts.py` mask/observer refusals; both remain open, along with failures in the still-unimplemented Caddy and indexer slices. None of these are PostgreSQL regressions.

Latest inspected pushed CI is still **`f618bae`**, [run 36038914373](https://github.com/troja-gnister/aegis/actions/runs/36038914373): **failed overall; backend/frontend passed, deployment/E2E failed**. No cause is established. Report: `.superpowers/toolchain/ci-read-f618bae-b07382f02e6e4c168a209f527039424a.json`. `1bb14e4` and the following documentation commit are pushed to `origin/main` (previously at `6e27634`); Docker CI for this push has not yet been inspected. Earlier runs `36027572227` (`2857a31`) and `35989701646` (`db6e107`) had the same job outcomes; their evidence remains historical and separate.

For `2857a31`, public step metadata locates deployment failure at step 7 after config/build passed, and E2E at step 8 after locked dependencies/browser installation passed. Anonymous log download returned 403; the HTML required sign-in. No GitHub credentials or cookies were read. Do not repeatedly retry the same unauthenticated log routes or attribute Docker CI failures to a Podman observation. Obtain authorized logs or reproduce the exact failure safely.

Read-only helper `.venv/bin/python .superpowers/toolchain/read-aegis-ci.py <full-sha>` retrieves public run metadata. `read-aegis-ci-steps.py` is fixed to the historical `2857a31` jobs, not a generic latest-run helper. Neither provides unavailable logs.

Useful retained local evidence:

| Path under `.superpowers/` | Scope |
| --- | --- |
| `toolchain/podman-identity-20260924.json` | Host/engine/provider identity observation. |
| `toolchain/podman-mask-mount-regressions-tz-20260924.log` | 25 passed / 1 stale assertion failed; includes 21 actual and four fake cases. |
| `toolchain/podman-observer-cause-runtime-20260924.log` | Corrected affected actual nested-mount observer test: 1 passed in 37.35s. |
| `toolchain/canonical-mask-merge-render-20260924.json` | Actual four-profile configuration rendering for committed merge fix. |
| `toolchain/postgres-bootstrap-survey-f8c7d4148bd14c529a0c3a5f6bc07053.jsonl` | O2 survey: locates the `/` ancestor cause; measures rootless overlay root `0:0`/`555`; other 55 guards pass. |
| `toolchain/podman-postgres-combined-20260927.log` | Combined PostgreSQL acceptance: 318 passed, 0 skipped, 523s; final containers empty, volumes 18. |
| `toolchain/deployment-resource-free-names-20260927.log` | Resource-free `tests/deployment -m "not integration"` classification: pre-existing Caddy/indexer/NNP/rendered_mounts failures, none PostgreSQL regressions. |
| `toolchain/caddy-runtime-f1aa7ca7b2684a09b44248b91a88c09d-l83511fx/evidence.jsonl` | Isolated four-profile Caddy runtime success and exact cleanup. |

The passing Caddy diagnostic script is `toolchain/probe-caddy-runtime-r1-71da40e.py`, SHA-256 `1dc8079aaddfa8b56409d4b3f66ce379fe52dc7fc07f6431901b5b419f70e966`. It uses a coherent immutable helper bundle `toolchain/caddy-runtime-base-71da40e/manifest.json`, SHA-256 `6642c8a1caf5682f8b2e5eb295619780c0b88a1b85feb17fa92f33ff3017782a`. Do not mix stale helper snapshots or rerun earlier rejected probes unchanged. Full artifact/review history is in the working ledger (`progress.md`).

## Remaining product tasks and acceptance boundaries

| Task | Required next work |
| --- | --- |
| 13 | Draft/apply/cancel metadata filters, accessible chips, bounded details, status and authorized rescan controls. Read `task-13-brief.md`, `task-13-context.md` and the full public task. |
| 14 | Validated test-port override/preflight; at least 603 owned physical entries, actual indexing and mobile Chromium/WebKit APIs/journeys; pre/post source manifests after stopping workers. |
| 15 | Deterministic streamed 1M-entry catalog with a 50K-child folder, fresh owned disk database, ten authenticated HTTP users, full sort/filter/page/details/status workload, plans/counts/payloads/latency/errors/sizes/identity. |
| 16 | Explicitly opted-in 1M actual synthetic source entries outside checkout/user storage; capacity/inodes, at least 12 GiB catalog/state plus source and safety margin; real scan/recovery/failure/preservation/aggregate-memory evidence. |
| 17 | At least 30 minutes on the actual 1M/50K stack, every child forward/back, bounded rows/heap, LCP/INP/useful render/restoration/filter/details/revocation; Chromium performance distinct from WebKit functional evidence. |
| 18 | Disposable upgrade from immutable Phase 1; preservation, operator runbook, exact-revision fresh-checkout/Podman/CI acceptance and accurate ledgers. |

Task 13 must preserve exact decimal-string/BigInt sizes, unknown values and explicit local-timezone conversion; consume the query observer's selected deduplicated window; retain at most five pages, no inactive datasets and bounded namespace-owned navigation. No private names/filter text in URLs, history state or persistent storage. Keep Task 12 deep-history/anchor/focus behavior and session revalidation before private back/forward content appears. Reuse the CSRF store in `frontend/src/features/auth/api.ts`, capturing the initiating namespace **before** awaited token acquisition. Rescan requires literal `root_admin`, not platform-admin status; retries reuse the request UUID. Poll active/idle at 3/30 seconds, failures at no more than 60 seconds, pause hidden/offline, and avoid refetching every retained page per indexing batch.

Performance limits remain browse p95 ≤300ms unloaded / ≤500ms under specified scan load; aggregate indexer RSS ≤750 MiB; catalog tables/indexes ≤8 GiB; metadata-only initial scan ≤4h on the calibrated reference root; mobile LCP/useful render ≤2.5s, INP ≤200ms, heap ≤250 MiB, and at most 80 rendered rows. Use the plan's complete versioned workload, storage calibration and resource limits. This host's results are provisional unless it meets the reference requirements. Separate cold/warm, unloaded/loaded, catalog/physical, smoke/full and provisional/reference-certified results; unavailable metrics are never zero-valued passes.

Originals remain read-only in every mounted role; web gets no original mounts. No application operation may create, rename, move, delete or overwrite originals. Never use inherited production database configuration. Never add `:U`, recursive ownership/permission changes or `:z/:Z` to user originals. Test-only ownership/labels are limited to reviewed newly owned resources. Preserve networking/no-egress, SELinux, nested-mount rejection, actual init/reaping and enforced resource limits; do not replace failures with skips. Host-level changes or trust-model changes require approval.

Continue with verified scoped commits and frequent pushes on `main`, synchronizing README, the implementation ledger, development guide, delivery ledger and public handoff. Phase 2A.1 completion will still leave broader 2A search/events/downloads and milestones 2B–2G. No production deployment, release tag, preview refresh or original-file cleanup is authorized.
