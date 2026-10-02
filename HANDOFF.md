# Aegis continuation handoff — October 1, 2026

Resume in `/var/home/troja/Dev/aegis` on **`main`**. Docker is the supported, primary runtime and Docker CI is green. **Phase 2A.1 stands at 18 tasks: 14 accepted, 4 remaining.** Task 14 (real-stack mobile journeys) was accepted at `62768d8` (CI [run 36947158466](https://github.com/troja-gnister/aegis/actions/runs/36947158466), all four jobs). A follow-up bugfix, `0682fba`, and the following documentation commit are pushed to `origin/main`, and Docker CI for that push has not been inspected yet. The bugfix restores Django admin login behind the gateway and adds stale-cursor recovery. Next is **Task 15**, the deterministic streamed 1M-entry catalog benchmark. Podman is a secondary, optional runtime; its prerequisite work and deferred follow-ups are recorded in [the Linux/Podman report](docs/verification/linux-podman-prerequisite.md), not repeated here.

## Next actions

1. Read this file and [`docs/development-handoff.md`](docs/development-handoff.md). Run `git status --short --branch`, `git fetch`, and confirm `main` matches `origin/main` at the pushed documentation head, on top of `0682fba`. Never reset, force-push, auto-stash, or discard local work.
2. Inspect the CI run for the pushed head and confirm all four Docker CI jobs pass.
3. Start **Task 15** from the [plan](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-15-catalog-fixture-and-authenticated-query-benchmark). Read its full requirements first; this file does not replace them.
4. Track, but do not block on, the open Task 14 items: **I3** WebKit edge-scroll auto-load after a previous-page prepend (the test avoids it); **M3** root cause of the app-shell overflow; **M4** e2e coverage of genuinely empty and lost-mount unavailable roots; **M6** record the measured e2e duration.
5. Keep one source implementer active at a time, with independent specification/code-quality review and scoped correction review. Keep README, the plan ledger, the development guide, the delivery spec and the public handoff in sync as checkpoints land.

Do not restart Tasks 1–14 or redispatch the completed PostgreSQL, Caddy, indexer or Docker CI fixes. Chronological evidence is in `.superpowers/sdd/2026-09-14-phase-2a-indexed-browser/progress.md` (ignored working notes) and the plan's evidence sections.

Authoritative documents:

- [Public development handoff](docs/development-handoff.md), [README](README.md), [development guide](docs/development.md)
- [Phase 2A.1 plan and task ledger](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md)
- [Indexed-browser design](docs/superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md)
- [Phase 2 delivery design](docs/superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md)
- [Platform architecture and benchmark requirements](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md)
- [Linux/Podman evidence and limitations](docs/verification/linux-podman-prerequisite.md)

## Tool environment and commands

Use the locked local toolchain, not the host's Python 3.14 / Node 22 (Python 3.13.15, uv 0.12.8, Node 24.20.0, PostgreSQL 18; use tracked `uv.lock` and npm lock; do not upgrade dependencies incidentally):

```bash
export PATH="/var/home/troja/Dev/aegis/.superpowers/toolchain/node-v24.20.0-linux-x64/bin:/var/home/troja/Dev/aegis/.superpowers/toolchain/uv-x86_64-unknown-linux-gnu:/usr/local/bin:/usr/bin:/bin"
export UV_CACHE_DIR=/var/home/troja/Dev/aegis/.superpowers/toolchain/uv-cache
export UV_PYTHON_INSTALL_DIR=/var/home/troja/Dev/aegis/.superpowers/toolchain/python
export NPM_CONFIG_CACHE=/var/home/troja/Dev/aegis/.superpowers/toolchain/npm-cache
export VITEST_MAX_WORKERS=1
```

Gates, run sequentially (Docker Engine with Compose v2; see [development guide](docs/development.md)):

```bash
make verify
make verify-compose
make test-e2e
git diff --check
```

Latest local evidence (Docker Engine 28.5.2): `make verify` 1808 backend and 219 frontend tests at `62768d8`; `make test-e2e` 22/22 and frontend 222 at `0682fba`. The host is Fedora Silverblue with rootless Podman and no local Docker Engine; `/tmp` is a 16 GiB tmpfs, unsuitable for million-entry work, and the host is not the calibrated N100 reference.

**Optional local Docker-in-container.** With no Docker Engine installed, a disposable `docker:dind` container can serve as a throwaway engine; see [Local Docker without installing Docker](docs/development.md#local-docker-without-installing-docker-optional). The ignored wrapper `.superpowers/toolchain/dind-run.sh` runs commands in a CI-like runner (uid 1000, docker 28.5.2); owner records are `.superpowers/toolchain/dind-owner-*.json`. After a host restart, recreate the socket directory at the recorded path and restart the container. This is a convenience, not a requirement.

Browser tooling: Playwright 1.63.0; host WebKit lacks required libraries, so WebKit runs through the e2e harness's runner image.

Podman, if used, needs `AEGIS_CONTAINER_ENGINE=podman` and a fresh, identity-recorded private `AEGIS_PODMAN_SOCKET`; see the Podman section of the README and the prerequisite report. Do not enable a persistent, rootful or TCP API.

## Safety boundaries

- Originals are read-only in every mounted role; web gets no original mounts. No operation may create, rename, move, delete or overwrite originals. Never add `:U`, recursive ownership/permission changes or `:z/:Z` to user originals.
- No user library, operator database, preview, production deployment, release tag or million-entry fixture has been touched. Never use inherited production database configuration.
- Preserve networking/no-egress, SELinux, nested-mount rejection, init/reaping and enforced resource limits; do not replace failures with skips. Host-level or trust-model changes need user approval.
- Before every harness run, check that ports `18080` and `55432` have no listeners; never stop an unrelated service to free a port.
- No broad container/volume/system prune or indiscriminate Compose teardown. Unknown contents or changed identities stop cleanup and preserve evidence.
- Do not claim full Podman compatibility, scale acceptance, or reference-host results. Separate cold/warm, unloaded/loaded, catalog/physical, smoke/full and provisional/reference-certified results; unavailable metrics are never zero-valued passes.

## Retained resources (not cleanup targets)

Earlier runs left **18 Podman volumes and 24 networks** (plus the unrelated default network), older refusal-test diagnostics, build caches and recorded images. Do not delete them by name, label, prefix or a relaxed admission rule. Disposable Docker-in-container state lives under `.superpowers/toolchain/` and is removed only via its recorded owner file. A reboot ended all earlier private API services.

## Remaining tasks and acceptance boundaries

| Task | Required work |
| --- | --- |
| 15 | **Next.** Deterministic streamed 1M-entry catalog with a 50K-child folder, fresh owned disk database, ten authenticated HTTP users, full sort/filter/page/details/status workload, plans/counts/payloads/latency/errors/sizes/identity. |
| 16 | Explicitly opted-in 1M actual synthetic source entries outside checkout/user storage; capacity/inodes, at least 12 GiB catalog/state plus source and safety margin; real scan/recovery/failure/preservation/aggregate-memory evidence. |
| 17 | At least 30 minutes on the actual 1M/50K stack, every child forward/back, bounded rows/heap, LCP/INP/useful render/restoration/filter/details/revocation; Chromium performance distinct from WebKit functional evidence. |
| 18 | Disposable upgrade from immutable Phase 1; preservation, operator runbook, exact-revision fresh-checkout/Podman/CI acceptance and accurate ledgers. |

Limits: browse p95 ≤300 ms unloaded / ≤500 ms under scan load; aggregate indexer RSS ≤750 MiB; catalog tables/indexes ≤8 GiB; metadata-only initial scan ≤4 h on the calibrated reference root; mobile LCP/useful render ≤2.5 s, INP ≤200 ms, heap ≤250 MiB, at most 80 rendered rows. Use the plan's complete versioned workload and resource limits.

Accepted contracts later tasks must not regress: exact decimal-string/BigInt sizes, unknown values and explicit local-timezone conversion; consume the query observer's deduplicated window; retain at most five pages; no private names/filter text in URLs, history state or persistent storage; keep deep-history/anchor/focus behavior and session revalidation; reuse the CSRF store in `frontend/src/features/auth/api.ts`; rescan requires literal `root_admin`; status polling 3 s active / 30 s idle, backoff at most 60 s, paused while hidden or offline. The gateway owns a single `Referrer-Policy: same-origin`.

Phase 2A.1 completion still leaves broader 2A search/events/downloads and milestones 2B–2G. No production deployment, release tag, preview refresh or original-file cleanup is authorized.

## Git checkpoint

`0682fba` and the following documentation commit are pushed to `origin/main`, and Docker CI for that push has not been inspected yet.

| Revision | Meaning |
| --- | --- |
| `0682fba43962c79f98eb9b78588c745ad0e296fe` | **Pushed** (with the following documentation commit; CI not yet inspected). Admin login behind the gateway (single `same-origin` Referrer-Policy) and stale-cursor "Start from the beginning" recovery. e2e 22/22, frontend 222. |
| `62768d816436697959ed716b076a1f363de97435` | **Task 14 accepted; pushed.** Indexed mobile browsing and read-only scan recovery e2e; 619-entry owned fixture; CI run 36947158466 all four jobs. |
| `191667f77332c9967c4de78f224d961458446939` | Docs: Task 13 acceptance. |
| `bbac055b014e4b0c303ce3ccefac47949e0440e9` | Task 13 accepted: mobile filters, details, scan status and rescan; CI run 36936455170. |
| `e0850308a5e98b4d332dc8d0a19852226c8a3d50` | Last of three fixes that turned Docker CI green (run 36462687576); the regression was bisected to `163c025`. |
| `9507de34b78a4a64a27f33696ce774616f1793a7` | Last Podman prerequisite slice (indexer fixtures). Caddy `240963a` and PostgreSQL `1bb14e4` precede it; see the prerequisite report. |
| `a82db75d9949170e1d22acfed0f08050f067edf2` | Task 12 accepted. |
