# Aegis continuation handoff — September 24, 2026

Development is paused at the user's request. Resume in `/var/home/troja/Dev/aegis` on **`main`**. Finish the separately scoped Linux/Podman prerequisite, then Phase 2A.1 Tasks **13–18**, in order. **18 tasks: 12 accepted, 6 remaining. Task 13 has not started.** Do not restart Tasks 1–12 or expand this package into the full rewrite.

This file records both the pushed repository and unfinished local work. **Ten PostgreSQL candidate files remain deliberately uncommitted.** Preserve them. The temporary test API has been stopped, its recorded directory removed, and no Podman containers were running at pause. No implementation or resource harness is running.

## First actions in a new session

1. Read this file and `docs/development-handoff.md`. Check `git status --short --branch`, `git remote -v`, and recent history before editing. Fetch origin; use `git pull --ff-only origin main` only when safe. Never reset, force-push, auto-stash, or discard local work. The source inventory below is expected; investigate any additional changes.
2. Verify the local candidate against its frozen manifest and backup before resuming its correction. The immediate blocker is **D1: Compose serialization of the PostgreSQL test diagnostic**, described below. No production-helper fix is justified until an actual diagnostic establishes the bootstrap failure's cause.
3. Read the complete authoritative documents and the relevant prerequisite brief/review. Keep one source implementer active at a time; use independent specification/code-quality review and scoped correction review. The user explicitly authorized this workflow and work on `main`. Existing agent handles are optional conveniences; all necessary state is in files.
4. Finish D1, freeze and review the correction, then run the focused PostgreSQL checks with freshly owned resources. Continue PostgreSQL → Caddy → indexer fixture compatibility → full gates → Task 13. Do not execute the currently blocked diagnostic just to rediscover its rendering failure.

Authoritative documents:

- [Public development handoff](docs/development-handoff.md)
- [README](README.md) and [development guide](docs/development.md)
- [Phase 2A.1 implementation plan and task ledger](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md)
- [Indexed-browser design](docs/superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md)
- [Phase 2 delivery design and ledger](docs/superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md)
- [Platform architecture and benchmark requirements](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md)
- [Linux/Podman evidence and limitations](docs/verification/linux-podman-prerequisite.md)

Follow applicable `AGENTS.md` instructions if present. None was found during the existing work. Read each task's complete requirements before implementation; this handoff does not replace them.

## Git checkpoint and unfinished files

Immediately before this documentation-only pause checkpoint, freshly fetched `HEAD` and `origin/main` both pointed to **`f618bae3524befcffb2b907ff482be7ce0879330`**, zero ahead/behind. Origin is `git@github.com:troja-gnister/aegis.git`. The commit containing this handoff adds documentation only; use `git log` to identify its final hash.

| Revision | Meaning |
| --- | --- |
| `f618bae3524befcffb2b907ff482be7ce0879330` | Last pushed documentation checkpoint before this pause; records merge/private-tmpfs evidence. |
| `520cf1f5bb9d5126953d33b0ce37751080ef7db8` | Latest committed source: Podman overlay contributes only the mask option, preserving base NNP without a duplicate Compose merge. |
| `71da40ed34bbe05990093afbf12925df40462ccf` | Guarded duplicate-mask compatibility, with reviewed identity/behavior checks and focused actual mount evidence. |
| `163c02563d95de8e4e3930ae68377e946feef3b8` | Explicit local-engine/verification tooling; latest complete local `make verify` evidence. |
| `ab57e3037b9daed79aeb691eb486a4d2397ae4aa` | Original accepted portable handoff, present in history. |
| `a82db75d9949170e1d22acfed0f08050f067edf2` | Last accepted Task 12 application change, present in history. |

These ten local source files are the PostgreSQL candidate, **not accepted runtime compatibility**:

| Status | File |
| --- | --- |
| Modified | `compose.yaml` |
| Modified | `deploy/postgres/entrypoint.sh` |
| Modified | `docker/postgres.Dockerfile` |
| Modified | `tests/deployment/test_container_boundaries.py` |
| Modified | `tests/deployment/test_postgres_role_init.py` |
| New, untracked | `deploy/postgres/private-tmpfs.sh` |
| New, untracked | `tests/deployment/test_postgres_launch.py` |
| New, untracked | `tests/deployment/test_postgres_tmpfs.py` |
| New, untracked | `tests/support/postgres-role-init-tmpfs.sh` |
| New, untracked | `tests/support/postgres_tmpfs.py` |

All **54** relevant live sources matched the frozen diagnostic manifest at pause:

```text
.superpowers/sdd/2026-09-14-phase-2a-indexed-browser/podman-postgres-diagnostic-source/manifest.json
SHA256 93e28319460fd54383d3c0e213fccf366f80ffee3e6bd2c28ef090295d562185
```

The manifest maps repository-relative paths directly to SHA-256 strings. Verify its own hash, then hash every listed file; do not assume a previous review covers later edits.

An additional local backup preserves all ten files, their source modes/hashes, and a complete patch against `f618bae`:

```text
.superpowers/handoff/2026-09-24/manifest.json
SHA256 0ffe3b25ad4ff8f70e0d18228ea31140617c11d139892d9859d061c1b2fb30d5
.superpowers/handoff/2026-09-24/postgres-candidate.patch
SHA256 d254a7f20093c5095a4518c7245ca8f74ea9485f3268215a80216a97b5071c12
.superpowers/handoff/2026-09-24/source/
```

`git apply --check --reverse` passed against the live worktree without applying anything. **Do not apply the patch to the current tree: its changes are already present.** Backup copies use private file permissions; the manifest records original working modes. These backups, source freezes, toolchains and detailed reports are ignored local files. They survive a new session on this computer but are absent from a fresh clone. This documentation commit does not publish the unfinished source; a different computer needs a deliberate transfer or later verified source commit.

## Immediate blocker: PostgreSQL diagnostic D1

Working artifacts live under:

```text
.superpowers/sdd/2026-09-14-phase-2a-indexed-browser/
```

Read these complete files before editing:

- `progress.md` — chronological recovery ledger; accepted tasks stay accepted.
- `podman-postgres-implementation-brief.md`
- `podman-postgres-compatibility-design-review.md` — including September 24 supplement.
- `podman-postgres-provenance-review.md` — prior R1 correction passed.
- `podman-postgres-diagnostic-report.md`
- **`podman-postgres-diagnostic-review.md`** — current independent review, changes requested.

The current review's SHA-256 is `8e5010f87bddd21af9f6b2a11bbe0e4d4be96b2d17c95d83ee3d00d63facf6fd`. It found **one P1 issue, D1**, and no additional diagnostic privacy/control-flow/cleanup/security-assertion finding. No correction for D1 has been started.

The bootstrap-only diagnostic embeds Bash expressions (`${target-}`, `${#FUNCNAME[@]}`, `$frame`, `$?`, `$LINENO`) directly into a Compose entrypoint. The actual selected Compose 2.39.4 provider rejects the full command during `config`, before a diagnostic container starts. Encoding each dollar once as `$$` renders successfully, but the provider's JSON retains the escaped representation. The expected process arguments therefore must remain distinct from the serialized Compose representation. The current fake provider uses the same string for both and misses this boundary.

Required correction, as one bounded change:

1. Encode the **complete assembled owned bootstrap program exactly once** for Compose, protecting all expressions against host environment interpolation.
2. Retain the intended literal argument vector separately. Before start, compare the inspected `Config.Entrypoint` with that exact intended vector. Preserve strict normal-entrypoint/command checks; no ignored comparison or repeated generic unescaping.
3. Add a resource-free check using the actual selected provider's `config --format json`, the complete program and explicit `--env-file /dev/null`. Assert successful rendering and the precise escaped representation.
4. Model rendered configuration and inspected process arguments separately in the fake provider. Cover correct literal arguments, altered arguments refusing **before start**, and preserved variable/status/array expressions. Keep diagnostic privacy bounds, cleanup and no-start-on-refusal behavior.

The review retained its pure-provider reproduction at `/tmp/aegis-pg-diagnostic-compose-review-632w8nbp/`; treat that as optional scratch evidence, not a durable dependency. Existing correction agents, if available: `/root/podman_postgres` and `/root/podman_postgres_review`. Resume the implementer for D1, then the reviewer for D1 closure and introduced regressions only. Neither is running at pause.

Preserve the current `93e283…` freeze and all earlier reports. Create a new full source manifest, correction-only diff against it, and RED/GREEN report. No production-helper change, container launch, host change, public acceptance claim or broad re-review is part of D1.

### PostgreSQL design that must remain intact

The shared Docker/Podman declarations omit unsupported tmpfs `uid/gid` options. The candidate prepares only fixed, newly created private tmpfs roots using the existing root bootstrap and capabilities `CHOWN`, `DAC_OVERRIDE`, `FOWNER`, `SETGID`, `SETUID`.

- `/run/aegis-source-secrets`: root-owned 0700, validated only; six read-only, regular, nonempty, bounded input files. No ownership/permission mutation there.
- `/run/secrets`: 70:70, 0700; six staged files 70:70, 0400.
- `/run/postgresql`: 70:70, bootstrap 0775; the unchanged official entrypoint later makes the live socket directory 03775.
- `/tmp`: 70:70, 1777; `/var/run` must be the literal `../run` link.
- Validate the entire fixed target/source set before any mutation or copy. Change only the three empty directory inodes nonrecursively. Preserve bounded metadata parsing, physical identities, alias/ancestor/nested-mount checks, rechecks, UID 70 source denial, zero final capabilities, marker/HBA/environment privacy, reconciliation and rotation.

Container-visible mount metadata alone cannot distinguish every host tmpfs-root bind. The already reviewed R1 correction therefore records creation and checks effective engine provenance, image/configuration/security/source identity and the complete owned inventory **before native start of the exact recorded CID**. All launch phases use that order. Do not restore `compose up` before inspection. Direct role-init fixtures use their separately reviewed fixed-path test-only wrapper, not a generalized production selector.

The current diagnostic changes only the test bootstrap override: allowlisted target identifiers, at most six function/line frames, bounded ERR location/status, no shell tracing, no command/environment/secret dump. Logs come from the exact CID with `--tail 32` and a 30-second timeout before asserting bootstrap exit status. Accepted combined UTF-8 output is limited to 8192 bytes; this is **finite-tail collection followed by validation**, not a streaming byte cap.

### PostgreSQL evidence and next actual commands

| Scope / snapshot | Observed result |
| --- | --- |
| Original helper/fixture candidate | Parent: **153 passed in 90.40s**, zero skips; resource-free evidence. |
| R1 recorded-create/inspect/start correction, `a77c0a…` | Parent: **93 passed in 3.33s**, zero skips; independent review passed. |
| First canonical actual static/live run on R1 | **2 failed in 1.12s**: duplicate NNP prevented rendering, before application bootstrap. |
| R1 plus committed merge correction, `1ee640…` | **2 failed in 15.77s**: stale static security assertion; bootstrap passed provenance checks, then exited 1. Logs were fetched too late to establish its cause. |
| Current diagnostic/static correction, `93e283…` | Implementer: **126 passed in 8.77s**, zero skips; static checks passed. Actual-provider review then found D1. The diagnostic has never executed in a container. |

Do not aggregate these overlapping selections or report them as full acceptance. Both actual failed runs removed their disposable databases and recorded containers; final container inventories were empty. The production bootstrap's exit cause is still **unknown**. An 18-digit inode bound, image utility behavior and metadata guards are hypotheses, not measured diagnoses.

After D1 review passes, recheck ownership, socket, ports and source hashes, then capture a new complete log for:

```bash
uv run --locked python scripts/verify.py deployment \
  --test-target tests/deployment/test_container_boundaries.py::test_postgres_stages_fixed_source_secrets_into_uid_70_private_tmpfs \
  --test-target tests/deployment/test_container_boundaries.py::test_live_postgres_stages_secrets_and_drops_to_uid_70
```

Use the diagnostic to establish the actual cause; make the smallest evidenced correction with regression tests and independent review. After the bootstrap/live checks pass, cover initialization, existing-state reconciliation, accepted-base behavior, invalid secrets and rotation, including:

```bash
uv run --locked python scripts/verify.py deployment \
  --test-target tests/deployment/test_container_boundaries.py::test_postgres_reconciles_populated_accepted_base_and_rotated_secrets \
  --test-target tests/deployment/test_postgres_role_init.py
```

Retain original-file/secret-source manifests after stopping owned workers. Commit only the verified scoped PostgreSQL change, synchronize the public docs, and push before proceeding to the next prerequisite.

## Subsequent prerequisite slices

**Caddy application configuration is unimplemented.** Read `podman-caddy-tmpfs-design.md` and `podman-caddy-implementation-brief.md`. A prepared implementation agent may be resumable as `/root/podman_caddy_configuration`; it received no implementation GO.

Isolated diagnostics already passed two native and two Compose generations with pinned image `ebbf5210d94567392591a0a07d61279a8be8c9041091c1713427ffa23e0f842b` (Linux amd64, Caddy 2.11.4), exact UID/GID 10001, absent file capabilities, zero kernel capabilities, NNP, empty distinct 16 MiB private mounts, 0700/1777 modes, protective flags, tiny write/read, empty recreation and the unchanged full strict mount parser. Fresh mask checks and exact cleanup passed. These probes used no server, host binds, secrets, network or published ports; OOM score remained inherited 100. They do not establish TLS, canonical configuration or general resource enforcement.

The intended narrow Podman overlay uses `!override` only for Caddy/Caddy-local's private `/config` and `/tmp` mounts, adding `U,notmpcopyup` there. Keep Docker declarations unchanged, durable `/data` unchanged, and the `520cf1f` mask-only overlay contribution. Precisely validate the before/after service and every target/option/size/mode/user; no global `tmpfs` comparison exemption. Verify actual provider profile rendering, server/autosave, local TLS and certificate durability after recreation. `U` is for these newly owned private tmpfs mounts only, never originals or bind mounts.

**Indexer test-fixture compatibility is unimplemented.** Read `podman-indexer-tmpfs-design.md`, `podman-indexer-cleanup-diagnosis.md` and `podman-indexer-tmpfs-implementation-brief.md` after PostgreSQL/Caddy are verified. Four native coordination test mounts need precise Podman options with Docker strings unchanged. Dynamic-user and 501:20 positive fixtures require actual metadata/coordination evidence; a deliberately misowned 0:0 fixture must remain misowned without `U`. Caddy's 10001 evidence does not prove these users work.

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

Observed host: Fedora Silverblue **44.20260922.0**, booted kernel **7.2.6-200.fc44.x86_64**, Intel i7-1355U (10 cores / 12 threads), about 30 GiB RAM, Btrfs. A newer deployment/kernel was staged but not booted; do not report it as active. Latest measured checkout free space was about **36 GiB**. `/tmp` is a 16 GiB tmpfs and is unsuitable for the million-entry database/source workload. Recheck capacity and inodes before creating any large fixture. This computer is **not the calibrated N100 reference host**.

Rootless Podman **5.8.7**, crun **1.28**, cgroup v2/systemd, netavark **1.17.2**, aardvark **1.17.1**, pasta; SELinux was **Enforcing**. Rootless container UID 0 maps to host UID 1000; subordinate IDs start at 524288. The selected external provider is Docker Compose **2.39.4**, SHA-256 `7af95166a730b87e172d4fc9aefea8725d3c6c7327d59149267b452114ddb7d4`. Native engine commands explicitly use `podman --remote=false`; provider API 1.44 versus Podman API 5.8.7 is recorded valid behavior. Revalidate current versions and the guarded policy before launches.

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

**`AEGIS_PODMAN_SOCKET` must point to a newly created, identity-recorded, private rootless Unix service before resource tests. No such service is running now.** Do not reuse the old socket path or owner record as a live connection. Create only a fresh 0700 owned directory and 0600 socket with restrictive umask, record process/directory/socket identities, use foreground `podman --remote=false system service --time=0 unix://…`, and stop it with identity checks at the next pause. Preserve the committed executable/socket/provider/image attestation and fresh four-profile behavior checks. Do not enable a persistent/rootful/TCP API or mount its socket into application containers.

Locked Playwright is **1.63.0**; synthetic Chromium **153.0.8010.12** checks passed locally. Host WebKit **26.6** lacked the required ICU/JPEG/libav libraries. A separately reviewed isolated Noble runtime passed a synthetic WebKit probe and was removed. Its ignored controller is `.superpowers/toolchain/browser-runtime.py`; inspect its ownership records and lifecycle before reuse. The validated `E2E_WEBKIT_WS_ENDPOINT` is loopback-only with a bounded port and 32-hex path, applies only to WebKit, and rejects `PW_TEST_CONNECT*` overrides. Chromium keeps its local sandbox. The trusted development-browser host-network exception is not application no-egress evidence. No real Podman application browser gate has passed.

## Resource state at pause

The owned foreground API was interrupted through its recorded execution session **3046**; it exited 0. PID **372508** was then absent, and the exact command was absent. Its socket had already been removed by the service. Directory `/tmp/aegis-podman-api.20baw5r0` matched device 59 / inode 108838 / UID-GID 1000 / mode 0700, was empty and was removed. It is a historical identity, not a resource to recreate by name.

The local owner record now says `stopped`. Cleanup evidence is `.superpowers/toolchain/podman-api-stop-20260924.json`. Native `podman --remote=false ps --format json` returned `[]`; reserved ports **18080** and **55432** had no listeners. Other host services were left untouched. Recheck before starting the `aegis-phase1-e2e` harness or any database/container test.

**Retained resources are not cleanup targets.** The first failed deployment run left 18 volumes and 23 test networks, plus an unrelated default network. Its later inventory, `.superpowers/toolchain/podman-deployment-first-run-retained.json`, is an observation, not a creation-time ownership ledger. Do not delete by name, label, prefix or a newly relaxed admission rule. Older refusal-test diagnostics, build caches and recorded images are retained. Unknown contents or changed identities must stop cleanup and preserve evidence. No broad container/volume/system prune or indiscriminate Compose teardown is authorized.

No user library, original-file tree, operator database, preview, production deployment, release tag or million-entry fixture was touched as part of this pause.

## Verification and CI state

Latest complete local `make verify` belongs to **`163c025`**, not the current candidate: 1,452 backend tests, 147 frontend tests across 13 files, locked installs, Ruff, mypy (225 sources), Django checks/migrations, frontend lint/types/build and whitespace checks passed. The backend took 747.16s; frontend took 28.06s with one worker. Log: `.superpowers/toolchain/make-verify-round5-20260924.log`. Disposable resources were removed.

Latest inspected pushed CI is **`f618bae`**, [run 36038914373](https://github.com/troja-gnister/aegis/actions/runs/36038914373): **failed overall; backend/frontend passed, deployment/E2E failed**. No cause is established. Report: `.superpowers/toolchain/ci-read-f618bae-b07382f02e6e4c168a209f527039424a.json`. Earlier runs `36027572227` (`2857a31`) and `35989701646` (`db6e107`) had the same job outcomes; their evidence remains historical and separate. The new documentation-only pause commit may trigger another run; its status is not inferred here.

For `2857a31`, public step metadata locates deployment failure at step 7 after config/build passed, and E2E at step 8 after locked dependencies/browser installation passed. Anonymous log download returned 403; the HTML required sign-in. No GitHub credentials or cookies were read. A prior request for sanitized first errors/pytest summaries/cleanup output remains unanswered. Do not repeatedly retry the same unauthenticated log routes or attribute Docker CI failures to a Podman observation. Obtain authorized logs or reproduce the exact failure safely.

Read-only helper `.venv/bin/python .superpowers/toolchain/read-aegis-ci.py <full-sha>` retrieves public run metadata. `read-aegis-ci-steps.py` is fixed to the historical `2857a31` jobs, not a generic latest-run helper. Neither provides unavailable logs.

Useful retained local evidence:

| Path under `.superpowers/` | Scope |
| --- | --- |
| `toolchain/podman-identity-20260924.json` | Host/engine/provider identity observation. |
| `toolchain/podman-mask-mount-regressions-tz-20260924.log` | 25 passed / 1 stale assertion failed; includes 21 actual and four fake cases. |
| `toolchain/podman-observer-cause-runtime-20260924.log` | Corrected affected actual nested-mount observer test: 1 passed in 37.35s. |
| `toolchain/canonical-mask-merge-render-20260924.json` | Actual four-profile configuration rendering for committed merge fix. |
| `toolchain/podman-postgres-parent-resource-free-20260924.log` | 153 helper/fixture tests; no live PostgreSQL acceptance. |
| `toolchain/podman-postgres-live-r1-20260924.log` | First two actual failures before merge fix. |
| `toolchain/podman-postgres-live-merge-20260924.log` | Two failures after merge fix, bootstrap cause unknown. |
| `sdd/2026-09-14-phase-2a-indexed-browser/podman-postgres-diagnostic-green2.log` | 126 current fake/static diagnostic tests; D1 still blocks runtime. |
| `toolchain/caddy-runtime-f1aa7ca7b2684a09b44248b91a88c09d-l83511fx/evidence.jsonl` | Isolated four-profile Caddy runtime success and exact cleanup. |

The passing Caddy diagnostic script is `toolchain/probe-caddy-runtime-r1-71da40e.py`, SHA-256 `1dc8079aaddfa8b56409d4b3f66ce379fe52dc7fc07f6431901b5b419f70e966`. It uses a coherent immutable helper bundle `toolchain/caddy-runtime-base-71da40e/manifest.json`, SHA-256 `6642c8a1caf5682f8b2e5eb295619780c0b88a1b85feb17fa92f33ff3017782a`. Do not mix stale helper snapshots or rerun earlier rejected probes unchanged. Full artifact/review history is in the working ledger.

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
