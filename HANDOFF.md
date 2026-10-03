# Aegis continuation handoff — October 3, 2026

Resume in `/var/home/troja/Dev/aegis` on **`main`**. Docker is the supported, primary runtime; Podman is a secondary, optional runtime whose follow-ups are deferred (see [the Linux/Podman report](docs/verification/linux-podman-prerequisite.md)).

**Phase 2A.1 has 18 tasks: 15 accepted, 3 remaining (16, 17, 18).** Task 15, the streamed 1M-entry authenticated catalog benchmark, was accepted at `e8ea863`, pushed; [CI run 36959823576](https://github.com/troja-gnister/aegis/actions/runs/36959823576) passed all four jobs.

On top of that, a reviewed web performance slice is **committed locally and not pushed**. That commit is `f6db82e`, and the commit carrying this handoff follows it, so `main` is ahead of `origin/main`.

The public docs (README, plan ledger, `docs/development.md` status line, `docs/development-handoff.md`, delivery spec) still say "14 accepted, Task 15 next". **They have not yet been synced for Task 15 acceptance or the web performance slice.**

## Next actions, in order

1. **Check the repository state.**
   - Read this file and [`docs/development-handoff.md`](docs/development-handoff.md).
   - Run `git status --short --branch` and `git fetch`. Expect `main` to be ahead of `origin/main` by the two local commits.
   - Never reset, force-push, auto-stash or discard local work.
2. **Get the user's decision on the web performance slice (`f6db82e`), then push only with explicit approval.** The user must accept one behavior change first; the push question was interrupted, so ask it again.
   - **What changed:** pooled web database logins live up to 60 s, plus a 5 s idle check. An out-of-band `ALTER ROLE ... PASSWORD` or `NOLOGIN` on the web database role therefore takes effect within about 65 s instead of on the next request.
   - **What did not change:**
     - The documented rotation, which recreates PostgreSQL and its dependents (`docs/operations/phase-1-deployment.md:160`), takes effect as before.
     - Application sessions, deactivation, epochs and SQL grant changes still take effect immediately.
   - **Review result:** the independent review found this does not conflict with any accepted contract. It is a user decision.
   - **Options offered:**
     - (a) accept, add an operator-runbook note on `AEGIS_WEB_WORKERS` and the 65 s window, push, and watch CI (recommended);
     - (b) shorten the pool lifetime, then re-measure;
     - (c) hold.
   - **After pushing,** confirm all four Docker CI jobs pass. CI is checked through public annotations and the run page; `gh` is not installed, and job logs need authentication.
3. **Sync the public docs** for Task 15 acceptance (15/18) and the web performance slice:
   - README;
   - the plan ledger and Task 15 evidence row;
   - `docs/development.md`;
   - `docs/development-handoff.md`;
   - the delivery spec status.

   Keep them consistent with each other and with this file.
4. **Task 16 is blocked on disk space; ask the user.**
   - `/var/home` has about 26 GB free (95% used).
   - Task 16 needs at least 12 GiB for catalog and state, plus a 1M-file synthetic source tree, plus margin.
   - The tree must sit outside the checkout and user storage, and Task 16 needs explicit opt-in.
   - The user must free space or name another disk before Task 16 starts.
   - `/tmp` is a 16 GiB tmpfs and is unsuitable.
5. Then run **Task 16** (real 1M physical scan, recovery and loaded browse at p95 ≤ 500 ms under scan load), **Task 17** (a 30-minute mobile session) and **Task 18** (upgrade from Phase 1 and the acceptance handoff), in order, from the [plan](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md). Read each task's full requirements; this file does not replace them.

Do not restart Tasks 1–15 or redo the web performance slice. Chronological working notes, briefs, reports and reviews are in `.superpowers/sdd/2026-09-14-phase-2a-indexed-browser/` (ignored, local only): `progress.md` and the `web-perf-{brief,report,review}.md` files.

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
- **Open minor items m1–m8**, from `web-perf-review.md`:
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
- **Scratch directories:** 21 `/tmp/aegis-mount-preflight-*` directories exist. The user knows; do not delete them without asking.

## Tool environment and commands

Use the locked local toolchain, not the host's Python 3.14 or Node 22. That means Python 3.13.15 (`.venv`), uv 0.12.8, Node 24.20.0, Playwright 1.63.0 and PostgreSQL 18, with the tracked `uv.lock` and npm lock. Do not upgrade dependencies incidentally.

```bash
export PATH="/var/home/troja/Dev/aegis/.superpowers/toolchain/node-v24.20.0-linux-x64/bin:/var/home/troja/Dev/aegis/.superpowers/toolchain/uv-x86_64-unknown-linux-gnu:/usr/local/bin:/usr/bin:/bin"
export UV_CACHE_DIR=/var/home/troja/Dev/aegis/.superpowers/toolchain/uv-cache
export UV_PYTHON_INSTALL_DIR=/var/home/troja/Dev/aegis/.superpowers/toolchain/python
export NPM_CONFIG_CACHE=/var/home/troja/Dev/aegis/.superpowers/toolchain/npm-cache
export VITEST_MAX_WORKERS=1
```

Fast local checks (no engine): `.venv/bin/ruff check backend tests scripts`, `.venv/bin/mypy backend`, `git diff --check`. The project does **not** run `ruff format`, so ignore its output.

Full gates use Docker Engine with Compose v2: `make verify`, `make verify-compose`, `make test-e2e`. See the [development guide](docs/development.md).

### Local Docker engine

The host is Fedora Silverblue with rootless Podman and no Docker Engine.

- **The engine:** a disposable `docker:28-dind` container, `aegis-dind-74afb27e`, runs in rootless Podman. Its owner record is `.superpowers/toolchain/dind-owner-74afb27e.json`.
- **How to run commands:** use `.superpowers/toolchain/dind-run.sh '<command from repo root>'`. It runs a CI-like Ubuntu Noble runner (uid 1000, docker 28.5.2, compose v2.40.3, the locked uv and Node).
- **No `make` in the runner:** run the underlying commands from the Makefile instead.
- **Current state:** the container is **stopped** (Exited).
- **After a host reboot:** recreate the socket directory at the path recorded in the owner file, then `podman start aegis-dind-74afb27e`. See [Local Docker without installing Docker](docs/development.md#local-docker-without-installing-docker-optional).
- **Removal:** remove it and its images only by the exact identities in the owner file.

Podman, if used directly, needs `AEGIS_CONTAINER_ENGINE=podman` and a fresh, identity-recorded private `AEGIS_PODMAN_SOCKET`. Do not enable a persistent, rootful or TCP API.

## Working method and user rules

- **Workflow:**
  - Use subagent-driven development: one implementer at a time (opus), working from a written brief in the `.superpowers/sdd/...` directory.
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
- **Cleanup:**
  - Delete only exact recorded identities; never delete by name, label or prefix. No broad prune.
  - **Earlier runs left 18 Podman volumes and 24 networks. They are not cleanup targets.**
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
| commit after `f6db82e` | **Local, not pushed.** This handoff. |
| `f6db82e` | **Local, not pushed.** Web performance slice: multi-worker web, pooled web-role connections, cheaper status. Reviewed and approved; pending the user's acceptance of the 65 s revocation window before push. |
| `e8ea863` | **Task 15 accepted; pushed.** Million-entry authenticated catalog benchmark; CI run 36959823576, all four jobs passed. |
| `0744cab` | Docs: Task 14 acceptance; pushed. |
| `0682fba` | Admin login behind the gateway and stale-cursor recovery; pushed. |
| `62768d8` | Task 14 accepted; CI run 36947158466. |
| `bbac055` | Task 13 accepted; CI run 36936455170. |
| `e085030` | Last of three fixes that turned Docker CI green (run 36462687576). |
