# Aegis continuation handoff — October 3, 2026

Resume on **`main`** on a **new computer**; see [Setting up the new computer](#setting-up-the-new-computer) first. Docker is the supported, primary runtime. Podman is a secondary, optional runtime whose follow-ups are deferred; see [the Linux/Podman report](docs/verification/linux-podman-prerequisite.md).

**Phase 2A.1 has 18 tasks: 15 accepted, 3 remaining (16, 17, 18).** Task 15, the streamed 1M-entry authenticated catalog benchmark, was accepted at `e8ea863`; [CI run 36959823576](https://github.com/troja-gnister/aegis/actions/runs/36959823576) passed all four jobs.

A reviewed web performance slice, `f6db82e`, followed by this handoff, was **pushed to `origin/main` at the user's request on October 3.** Docker CI for that push had not been inspected when this file was written.

The public docs still say "14 accepted, Task 15 next" and **are not yet synced** for Task 15 acceptance or the web performance slice. They are the README, the plan ledger, the `docs/development.md` status line, `docs/development-handoff.md` and the delivery spec.

## Next actions, in order

1. **Set up and check the repository.**
   - Set up the new computer as described below.
   - Read this file and [`docs/development-handoff.md`](docs/development-handoff.md).
   - Run `git status --short --branch` and `git fetch`, and confirm `main` matches `origin/main`.
   - Never reset, force-push, auto-stash or discard work.
2. **Inspect the Docker CI run** for the pushed head on the [Actions page](https://github.com/troja-gnister/aegis/actions) and confirm all four jobs pass: backend, frontend, deployment and e2e.
   - Public annotations carry only the test id and exception type.
   - Job logs need authentication; use `gh` if it is installed on the new computer.
   - If a job fails, diagnose it before any new work.
3. **Confirm the revocation window with the user.**
   - **What changed:** the web slice pools web database logins for up to 60 s, plus a 5 s idle check. An out-of-band `ALTER ROLE ... PASSWORD` or `NOLOGIN` on the web database role now takes effect within about 65 s instead of on the next request.
   - **What did not change:** the documented rotation, which recreates PostgreSQL and its dependents (`docs/operations/phase-1-deployment.md:160`), is unchanged. Application sessions, deactivation, epochs and SQL grant changes still take effect immediately.
   - **Review result:** the independent review found no contract conflict.
   - **Status:** the user had this question pending when they asked to push; it was never answered explicitly. Mention it once and offer to shorten the lifetime and re-measure.
   - **Either way:** add the operator-runbook note on `AEGIS_WEB_WORKERS` and the 65 s window (review item m8) to `docs/operations/`.
4. **Sync the public docs** for Task 15 acceptance (15/18) and the web performance slice:
   - README;
   - the plan ledger and Task 15 evidence row;
   - `docs/development.md`;
   - `docs/development-handoff.md`;
   - the delivery spec status.

   Keep them consistent with each other and with this file. Ask before pushing.
5. **Before Task 16,** check the new machine's free disk; see the setup section. Ask the user for the disk or path to use and for explicit opt-in.
6. Then run **Task 16** (real 1M physical scan, recovery and loaded browse at p95 ≤ 500 ms under scan load), **Task 17** (a 30-minute mobile session) and **Task 18** (upgrade from Phase 1 and the acceptance handoff), in order, from the [plan](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md). Read each task's full requirements; this file does not replace them.

Do not restart Tasks 1–15 or redo the web performance slice. The old machine's ignored working notes (`.superpowers/`) do not carry over; everything needed is in this file, the plan's evidence sections and `docs/verification/`.

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
- **Podman-only, deferred:**
  - core-services NNP assertion;
  - `test_rendered_mounts.py` refusals;
  - `test_database_roles.py:414` host-network check;
  - O11, client source IPs not preserved by rootless published ports.

## Setting up the new computer

Development moves to a **new computer** after this handoff. Nothing from the old machine carries over except what is in git:

- the ignored `.superpowers/` directory (working notes, reviews, toolchain, the Docker-in-container helper);
- `.venv`;
- `node_modules`.

This file and the tracked docs contain everything needed. Old-host details no longer apply: its disk limits, its retained Podman volumes and networks, and its `/tmp` scratch directories.

1. **Get the code.** Clone `https://github.com/troja-gnister/aegis`, or `git pull` an existing clone, and check out `main`. Confirm `git log -1` is the newest handoff commit (after `805fce4`).
2. **Install the prerequisites** from [`docs/development.md#prerequisites`](docs/development.md#prerequisites):
   - Python 3.13 (`.python-version`) and uv 0.12.8;
   - Node.js 24 (`frontend/package.json` engines `>=24 <25`) and npm;
   - Git and GNU make;
   - **Docker Engine with Compose v2.** It is the primary runtime. Prefer a native Docker Engine; the old host's Docker-in-Podman helper was a workaround and is not tracked.
3. **Set up the project:**
   - Run `uv sync --locked --group dev`.
   - In `frontend/`, run `npm ci` and then `npm exec -- playwright install chromium webkit`. Playwright is 1.63.0, from the lockfile.
   - Use the tracked lockfiles and do not upgrade dependencies incidentally.
   - Set `VITEST_MAX_WORKERS=1` for frontend gates on small machines.
4. **Run the gates sequentially:** `make verify`, `make verify-compose`, `make test-e2e`, `git diff --check`. Fast checks without an engine:
   - `uv run --locked ruff check backend tests scripts`;
   - `uv run --locked mypy backend`.

   The project does **not** use `ruff format`.
5. **Check the new machine against Task 16.** Record its CPU, RAM and free disk. Task 16 needs at least 12 GiB for catalog and state, plus a 1M-file synthetic source tree, plus margin. The tree must be on a filesystem outside the checkout and user storage, and **not** on a tmpfs. Ask the user which disk or path to use, and get explicit opt-in.
6. **The new machine is not the reference host.** Like the old one, it is not the calibrated N100 reference unless the user says it is, so label its benchmark results provisional.

On Podman, set `AEGIS_CONTAINER_ENGINE=podman` and a fresh, identity-recorded private `AEGIS_PODMAN_SOCKET`; see the README's optional Podman section. Do not enable a persistent, rootful or TCP API.

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
| `805fce4` and the following commit | Handoff; pushed October 3. |
| `f6db82e` | **Pushed October 3** at the user's request; CI not yet inspected. Web performance slice: multi-worker web, pooled web-role connections, cheaper status. Reviewed and approved. |
| `e8ea863` | **Task 15 accepted; pushed.** Million-entry authenticated catalog benchmark; CI run 36959823576, all four jobs passed. |
| `0744cab` | Docs: Task 14 acceptance; pushed. |
| `0682fba` | Admin login behind the gateway and stale-cursor recovery; pushed. |
| `62768d8` | Task 14 accepted; CI run 36947158466. |
| `bbac055` | Task 13 accepted; CI run 36936455170. |
| `e085030` | Last of three fixes that turned Docker CI green (run 36462687576). |
