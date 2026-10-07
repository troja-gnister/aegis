# Aegis continuation handoff — October 4, 2026

Resume on **`main`** on the **Fedora 44 Server development host**, which runs native rootless Podman and no Docker Engine; see [Setting up the new computer](#setting-up-the-new-computer). Docker is the supported, primary runtime. Podman is a secondary, optional runtime; its remaining gap, O11, belongs to part 2. See [the Linux/Podman report](docs/verification/linux-podman-prerequisite.md).

**Part 1 (Podman development environment) is complete:** commits `b6139ef` (spec) through `5776d58`, then part 1's documentation commits starting with `b027e0b`, all made on this host. Final fresh-shell gates under rootless Podman (October 4): `make verify` exit 0 (backend 1,892 and frontend 229 tests passed; 202 s); `make verify-compose` 1,258 passed, 0 failed, 0 skipped, plus exactly the nine O11 errors (25 min 13 s); `make test-e2e` 22/22 (164 s); `git diff --check` clean; `stop` exited 0 with no pre-existing Podman object removed or changed. Full Podman compatibility is not claimed while O11 is open. `git status --short --branch` shows whether these commits are on `origin/main`; pushing them needs the user's approval. Once they are pushed, Docker CI for the pushed head must pass all four jobs; it also has to confirm the Docker branches of the changed deployment tests, because this host has no Docker engine. Details: the [Fedora Server verification section](docs/verification/linux-podman-prerequisite.md#fedora-server-development-host-october-2026) and the [part 1 design](docs/superpowers/specs/2026-10-04-podman-dev-environment-design.md).

**Phase 2A.1 has 18 tasks: 15 accepted, 3 remaining (16, 17, 18).** Task 15, the streamed 1M-entry authenticated catalog benchmark, was accepted at `e8ea863`; [CI run 36959823576](https://github.com/troja-gnister/aegis/actions/runs/36959823576) passed all four jobs.

A reviewed web performance slice, `f6db82e`, followed by the handoff commits `805fce4` and `6bfaa01`, was **pushed to `origin/main` at the user's request on October 3.** Docker CI [run 37123170741](https://github.com/troja-gnister/aegis/actions/runs/37123170741) for `6bfaa01`: attempt 1 failed e2e only (`phase2a.spec.ts:339` in mobile Chromium), and attempt 2 passed all four jobs. Part 1 did not reproduce that failure locally.

The public docs still say "14 accepted, Task 15 next" and **are not yet synced** for Task 15 acceptance or the web performance slice. They are the README, the plan ledger, the `docs/development.md` status line, `docs/development-handoff.md` and the delivery spec.

## Next actions, in order

Before new work, run `git status --short --branch` and `git fetch`. If part 1 is still unpushed, ask the user before pushing. Confirm that `main` matches `origin/main` and that Docker CI for the pushed head passed all four jobs: backend, frontend, deployment and e2e. If a job fails, diagnose it first (`gh` is installed on this host). Never reset, force-push, auto-stash or discard work.

1. **Brainstorm part 2: persistent private-network deployment on this host.** It gets its own specification, plan and review. Agreed requirements:
   - free ports, because default ports may already be taken on a shared host, and a firewall rule that admits only private-network sources;
   - local-CA HTTPS;
   - fixing O11, so real client addresses reach Caddy and the gateway;
   - SELinux and UID mapping for read-only originals, without relabeling or ownership changes;
   - the user's chosen test root as the first root;
   - pull-and-run published images, with no checkout or local build on the server;
   - existing services are left untouched until the user approves replacing anything.
2. **Follow-ups from the part 1 final review.** All are minor and none blocks the push. Do (c) before part 2 configures a real root on Podman; agree the order of the rest with the user. Part 1's ledger holds the details.
   - (a) Privacy: write the credential-diagnostic probe under an `/e2e/` path so it exercises the new stack branch, anchor `SPEC_FRAME` to `at` frames, and guard `typeof error.stack` (`frontend/e2e/verify-diagnostics.mjs`, `safe-location.ts`, `safe-test.ts`).
   - (b) Helper: when the recorded API PID was reused (a different start time proves the API process is gone), `stop` should still never signal it but should continue the identity-checked socket and directory removal; today it refuses there.
   - (c) Before part 2 configures real roots on Podman: refuse commas in extra `-v`/`--mount` roots of `podman compose run` and in `aegisctl mounts validate`.
   - (d) Ticket to open: a `$` in the checkout path defeats the "runtime cannot mount the repository" checks (`scripts/e2e_support.py:703`, `scripts/benchmarks/resources.py:249`); pre-existing, both engines.
   - (e) Helper hardening backlog (tar-tree member path validation, clean errors instead of tracebacks, the WebKit token in journald and the container command line), plus a retention policy for the guard's `/tmp` folders and dangling build images.
   - (f) The pre-existing high-severity npm advisory in the frontend lockfile.
3. **Part 3, after part 2 is running: sync the public docs** for Task 15 acceptance (15/18) and the web performance slice:
   - README;
   - the plan ledger and Task 15 evidence row;
   - `docs/development.md`;
   - `docs/development-handoff.md`;
   - the delivery spec status;
   - the operator-runbook note on `AEGIS_WEB_WORKERS` and the 65 s window (review item m8) in `docs/operations/`.

   Keep them consistent with each other and with this file. Ask before pushing.
4. **Part 3, before Task 16:** check the machine's free disk; see the setup section. Ask the user for the disk or path to use and for explicit opt-in.
5. **Part 3, then** run **Task 16** (real 1M physical scan, recovery and loaded browse at p95 ≤ 500 ms under scan load), **Task 17** (a 30-minute mobile session) and **Task 18** (upgrade from Phase 1 and the acceptance handoff), in order, from the [plan](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md). Read each task's full requirements; this file does not replace them.

Part 4, media viewing (authorized downloads and ranges, thumbnails, photo and video viewers), follows part 3 and needs its own design.

**Revocation window, decided October 4.** The web slice pools web database logins for up to 60 s, plus a 5 s idle check, so an out-of-band `ALTER ROLE ... PASSWORD` or `NOLOGIN` on the web database role takes effect within about 65 s. The documented rotation, which recreates PostgreSQL and its dependents, is unchanged, and application sessions, deactivation, epochs and SQL grant changes still take effect immediately. Decision: keep the 60 s pool lifetime. Part 3 still adds the runbook note m8.

Do not restart Tasks 1–15, the web performance slice or part 1. The old machine's ignored working notes (`.superpowers/`) did not carry over; everything needed is in this file, the plan's evidence sections and `docs/verification/`. On this host, the ignored `.superpowers/sdd/2026-10-04-podman-dev-environment/` directory holds part 1's ledger, evidence and deferred minor review items, and host-specific operational notes for part 2 are kept in ignored local notes.

## Web performance slice (`f6db82e`) summary

- **Web workers:** `AEGIS_WEB_WORKERS` sets the number of uvicorn worker processes. It is validated to 1–4, with a Compose default of 4.
- **Connection pool:** only the web role uses a psycopg pool. Management commands, health probes and the operations, indexer and media roles stay unpooled.
- **Status endpoint:** the code-side schema identity is cached per process. The live migration check still fails closed, and it now requires every code migration to be recorded. The duplicate deployment read is gone, and the manifest is still hashed on every request.
- **Unchanged:** the authorization `FOR SHARE` locks, query semantics, cursor signing and security headers.
- **Full Task 15 profile before → after** (development host, provisional, not the N100 reference): p95 for directory pages 287.1 → 143.9 ms, details 327.5 → 149.9 ms, status 550.8 → 112.7 ms. Classes over 300 ms fell from 14 to 0. Throughput rose from 40.5 to 111 req/s, with zero errors. Web memory with 4 workers peaked at about 384 MiB against the 1024 MiB limit. `docs/verification/phase-2a-catalog-benchmark.{md,json}` are regenerated, with the earlier run kept as a labeled prior run.
- **Gates:**
  - Backend: 1880 passed, 1 skipped.
  - e2e: 22/22. Ruff and mypy are clean.
  - Deployment: 1180 passed, 7 failed. The 7 are known failures specific to the local runner: four from a missing `make`, two from `test_e2e_forces_local_podman_for_compose_boundary`, and one from a `test_container_boundaries` `--env-file` rejection, already recorded in Task 14.
- **Review:** independent review approved it, with no Critical or Important findings.
- **Open minor items m1–m8**, from the independent review:
  - m1: an unexpected exception from the pool check gives a bare 500;
  - m2: up to 16 web connections, with a 10 s acquire queue;
  - m3: the pool switch is not tied to the web role;
  - m4: no test runs a SQL `REVOKE`;
  - m5: a missing migration-history table aborts the caller's transaction;
  - m6: the generator's "Same profile, harness and host." line is not checked;
  - m7: an empty "classes over 300 ms" table;
  - m8: the runbook does not mention `AEGIS_WEB_WORKERS` or the 65 s window.
- **Expected limits:** after a PostgreSQL restart, a request can see a 503 for up to about 10 s. On a 4-core N100, web and PostgreSQL share the cores, so the gains will shrink.

## Other open items (tracked, not blocking)

- **Task 14:**
  - I3: WebKit edge-scroll auto-load after a previous-page prepend;
  - M3: app-shell overflow root cause;
  - M4: e2e coverage of empty and lost-mount roots;
  - M6: the e2e duration is not recorded.
- **Task 15:** no fail-closed test case for `AEGIS_DJANGO_SECRET_KEY_FILE` in `backend/aegis/settings/test.py`. N100 reference certification is still open; all results are provisional.
- **Podman:**
  - O11, client source IPs not preserved by rootless published ports: part 2. The nine `test_tls_gateway.py` tests fail under Podman until then; they are not skipped.
  - Fixed in part 1: the core-services NNP assertion (`4f6e575`) and the `test_rendered_mounts.py` refusals (`d247492`, `6c8486c`, `4a0b0b3`). `test_database_roles.py:414` passes under Podman unchanged.
  - Recorded and left in place: the guard's per-launch `/tmp/aegis-mask-check-*` folders (529, about 35 MiB, after the final run), observer diagnostics `/tmp/aegis-mount-preflight-*`, and one synthetic `/tmp/aegis-verify-*` folder per `make verify` run from `backend/tests/unit/common/test_verify_targets.py:656`, which should use `tmp_path`.
- **CI flake:** `phase2a.spec.ts:339` passed 50/50 locally in mobile Chromium, but the server stack was not limited to four CPUs, so CI-like contention was not reproduced. Keep watching CI.

## Setting up the new computer

The new computer is the Fedora 44 Server host, set up on October 4. It has no Docker Engine: it uses rootless Podman through the committed helper, so for this host follow the README's [Developing on Podman](README.md#developing-on-podman) subsection instead of installing Docker in step 2 and both Playwright browsers in step 3: on this host Playwright installs only Chromium, and WebKit runs in the helper's container. Nothing from the old machine carried over except what is in git:

- the ignored `.superpowers/` directory (working notes, reviews, toolchain, the Docker-in-container helper);
- `.venv`;
- `node_modules`.

This file and the tracked docs contain everything needed. Old-host details no longer apply: its disk limits, its retained Podman volumes and networks, and its `/tmp` scratch directories.

1. **Get the code.** Clone `https://github.com/troja-gnister/aegis`, or `git pull` an existing clone, and check out `main`. Confirm `git log -1` is the newest handoff commit, at or after part 1's documentation commit `b027e0b`.
2. **Install the prerequisites** from [`docs/development.md#prerequisites`](docs/development.md#prerequisites):
   - Python 3.13 (`.python-version`) and uv 0.12.8;
   - Node.js 24 (`frontend/package.json` engines `>=24 <25`) and npm;
   - Git and GNU make;
   - **Docker Engine with Compose v2.** It is the primary runtime. Prefer a native Docker Engine; the old host's Docker-in-Podman helper was a workaround and is not tracked.
3. **Set up the project:**
   - Run `uv sync --locked --group dev`.
   - In `frontend/`, run `npm ci` and then `npm exec -- playwright install chromium webkit` (on this Podman host, `chromium` only; see above). Playwright is 1.63.0, from the lockfile.
   - Use the tracked lockfiles and do not upgrade dependencies incidentally.
   - Set `VITEST_MAX_WORKERS=1` for frontend gates on small machines.
4. **Run the gates sequentially:** `make verify`, `make verify-compose`, `make test-e2e`, `git diff --check`. Fast checks without an engine:
   - `uv run --locked ruff check backend tests scripts`;
   - `uv run --locked mypy backend`.

   The project does **not** use `ruff format`.
5. **Check the new machine against Task 16.** Record its CPU, RAM and free disk. Task 16 needs at least 12 GiB for catalog and state, plus a 1M-file synthetic source tree, plus margin. The tree must be on a filesystem outside the checkout and user storage, and **not** on a tmpfs. Ask the user which disk or path to use, and get explicit opt-in.
6. **The new machine is not the reference host.** Like the old one, it is not the calibrated N100 reference unless the user says it is, so label its benchmark results provisional.

On Podman, use the helper (`python3 -m scripts.podman_dev tools`, then `uv run --locked python -m scripts.podman_dev start`, `env`, `status` and `stop`) described in the README's [Developing on Podman](README.md#developing-on-podman) subsection. It sets `AEGIS_CONTAINER_ENGINE=podman` and a fresh, identity-recorded private `AEGIS_PODMAN_SOCKET`. Do not enable a persistent, rootful or TCP API.

## Working method and user rules

- **Workflow:**
  - Use subagent-driven development: one implementer at a time (opus), working from a written brief in an ignored `.superpowers/sdd/<plan>/` directory.
  - Follow each implementation with an independent spec and quality review, then fix rounds with scoped re-review, at most 5.
  - Record each step in `progress.md`.
  - Implementers must not commit or spawn agents.
- **Commit and push:** the root session commits. **Ask the user before every push to the shared `main`**, and watch CI after it.
- **Docker first:** keep Aegis as universal as possible. Podman-specific configuration belongs in the README's optional Podman section. The user plans to use Aegis as a Nextcloud replacement, so HTTPS and real client IPs matter.
- **Security:** do not weaken CSRF, and never trust a `"null"` origin. Keep no secrets in git or public CI annotations; annotations carry only the test id and exception type.

## Safety boundaries

- **Originals:** originals are read-only in every mounted role, and web gets no original mounts.
  - No operation may create, rename, move, delete or overwrite originals.
  - Never add `:U`, recursive ownership or permission changes, or `:z`/`:Z` to user originals. `:z` is approved only on the two Caddyfile binds.
- **Untouched data:** no user library, operator database, production deployment, release tag or million-entry physical fixture has been touched. Never use inherited production database configuration.
- **Keep the existing protections:** networking and no-egress, SELinux, nested-mount rejection, init and reaping, and enforced resource limits. Do not replace failures with skips. Host-level or trust-model changes need user approval.
- **Ports:** before every harness run, check that ports `18080` and `55432` have no listeners. Never stop an unrelated service to free a port.
- **Cleanup:** delete only exact recorded identities; never delete by name, label or prefix. No broad prune, and never remove pre-existing containers, volumes or networks you did not create.
- **Claims:** do not claim full Podman compatibility, scale acceptance or reference-host results. Keep cold/warm, unloaded/loaded, catalog/physical, smoke/full and provisional/reference results separate; an unavailable metric is never a zero-valued pass.

## Acceptance limits and contracts

- **Browse latency:** p95 ≤ 300 ms unloaded and ≤ 500 ms under scan load.
- **Indexer and catalog:** aggregate indexer RSS ≤ 750 MiB; catalog tables and indexes ≤ 8 GiB; a metadata-only initial scan finishes within 4 h on the calibrated reference root.
- **Mobile:** LCP and useful render ≤ 2.5 s, INP ≤ 200 ms, heap ≤ 250 MiB, and at most 80 rendered rows.

Accepted contracts must not regress:

- **Values:** exact decimal-string/BigInt sizes, unknown values and explicit local-timezone conversion.
- **Paging:** keep the query observer's deduplicated window and retain at most five pages.
- **Privacy:** no private names or filter text in URLs, history state or persistent storage.
- **Navigation:** keep deep-history, anchor and focus behavior, and session revalidation.
- **CSRF:** reuse the CSRF store in `frontend/src/features/auth/api.ts`.
- **Rescan:** requires the literal `root_admin`.
- **Status polling:** every 3 s while active and every 30 s when idle, with backoff of at most 60 s, paused while hidden or offline.
- **Headers:** the gateway owns a single `Referrer-Policy: same-origin`.

Phase 2A.1 completion still leaves the broader 2A search, events and downloads, and milestones 2B–2G. No production deployment, release tag, preview refresh or original-file cleanup is authorized.

## Authoritative documents

- [Public development handoff](docs/development-handoff.md), [README](README.md), [development guide](docs/development.md)
- [Phase 2A.1 plan and task ledger](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md)
- [Indexed-browser design](docs/superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md)
- [Phase 2 delivery design](docs/superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md)
- [Platform architecture and benchmark requirements](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md)
- [Phase 1 secure foundation plan](docs/superpowers/plans/2026-08-31-phase-1-secure-platform-foundation.md)
- [Catalog benchmark summary](docs/verification/phase-2a-catalog-benchmark.md)
- [Linux/Podman evidence and limitations](docs/verification/linux-podman-prerequisite.md)

## Git checkpoint

| Revision | Meaning |
| --- | --- |
| `b6139ef`–`5776d58`, then part 1's documentation commits from `b027e0b` | **Part 1, Podman development environment.** Pushed only with the user's approval; after the push, check that Docker CI passed all four jobs. Spec and plan, helper (`8884a8b`–`d52895e`), helper fixes (`538b6b3`, `f7f69c4`), core-services NNP expectation (`4f6e575`), guard fixes (`d247492`, `6c8486c`, `4a0b0b3`), e2e fixes (`1df748a`, `7779fab`) and flake tooling (`5776d58`). |
| `805fce4` and `6bfaa01` | Handoffs; pushed October 3. CI run 37123170741 for `6bfaa01`: attempt 1 failed e2e only, attempt 2 passed all four jobs. |
| `f6db82e` | **Pushed October 3** at the user's request; covered by CI run 37123170741 on `6bfaa01`. Web performance slice: multi-worker web, pooled web-role connections, cheaper status. Reviewed and approved. |
| `e8ea863` | **Task 15 accepted; pushed.** Million-entry authenticated catalog benchmark; CI run 36959823576, all four jobs passed. |
| `0744cab` | Docs: Task 14 acceptance; pushed. |
| `0682fba` | Admin login behind the gateway and stale-cursor recovery; pushed. |
| `62768d8` | Task 14 accepted; CI run 36947158466. |
| `bbac055` | Task 13 accepted; CI run 36936455170. |
| `e085030` | Last of three fixes that turned Docker CI green (run 36462687576). |
