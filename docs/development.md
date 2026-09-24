# Development

Phase 1 provides credential login, authorized root cards, administration, durable job primitives, and the deployment boundary. Its [acceptance evidence](verification/phase-1.md) records the tested revision, clean-install results, and limitations. It does not enumerate files, generate previews, or edit documents. Follow the [roadmap](../README.md#roadmap) for those later slices.

The [Phase 2 delivery design](superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md) groups the complete v1 build into milestones 2A–2G. Its scope includes metadata-filter controls and photo/video thumbnails. Specifications and plans are not implementation evidence: keep feature statuses Planned until their work begins, and retain the original-file and isolated-test boundaries throughout.

The first work package is [2A.1 indexed browsing](superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md). Its [implementation plan](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md) defines **18 tasks: 12 complete, 6 remaining**. Tasks 1–12 are accepted: filename handling, focused verification, protected catalog and scan state, scheduling/leases, bounded read-only reading, transactional reconciliation, supervised scan execution, typed metadata filters, signed cursors, permission-bound indexed queries/details, private catalog/status/rescan HTTP APIs, bounded browser data state, and virtualized mobile file navigation. Linux/Podman prerequisite verification precedes Task 13; see the [development handoff](development-handoff.md). Task 7 review corrections `6022c97` passed 1,027 full-backend tests, 144 actual-role/installed-wheel checks, 14 current-code Linux runtime tests, full Ruff/mypy (205 sources), Django checks and migration-drift detection; independent review and scoped fix review passed. The plan preserves the initial revision's passing results and three lifecycle findings. Its [runtime contract and evidence](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-7-runtime-coordination-decision) cover read-only supervised scans, independent liveness, interruption/restart handling and indexer-only coordination storage. All four [Linux CI jobs](https://github.com/troja-gnister/aegis/actions/runs/35006055700) passed at Task 8 completion checkpoint `1ac4eb8`. No Phase 2 benchmark acceptance is claimed. The 50K iterator is allocation evidence only; local nested-mount failures/skips and complete historical verification remain documented in the plan.

The [approved transaction boundary and original failing reproduction](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-6-commit-fence-decision) remain documented. Each checkpoint wrapper owns a top-level transaction on an idle, autocommit-enabled indexer connection, refuses caller-owned outer transactions, explicitly defers the guard and returns only after commit. Ordinary expiry and terminal/replay transitions are tested for rollback; root/attempt/revocation checks, organization permissions and read-only originals are unchanged. Deliberate database-session constraint-timing tampering remains outside the absolute lease-deadline guarantee. Task 7 wires these wrappers to bounded mutation connections and acknowledges reader batches only after commit or explicit discard; the corrections retain these transaction and original-file boundaries, with independent review complete. Task 8's typed filters and signed cursor contracts are accepted. Its byte-limit corrections `78b05f1` passed 153 focused tests and full Ruff/mypy (209 sources); independent scoped review addressed both findings without new issues. Task 9's permission-bound indexed queries and bounded details are accepted: authentication correction `6c8b1f3` passed 96 covering tests, full Ruff/mypy (216 sources), migration/actual-role checks and independent scoped review. The earlier 1,265-test full-backend run remains pre-fix evidence. Task 10 covers the real HTTP request budget and session invalidation; its evidence excludes UI delivery. Development verification uses disposable fixtures and does not change the user-owned preview deployment or its read-only originals.

Task 10's private catalog and scan HTTP endpoints passed independent review after correction `c364394`: 75 focused tests, 273 covering tests, and all 1,346 backend tests passed, with clean Ruff/mypy (221 sources), system/migration and whitespace checks. Ready status now requires compatible worker/schema availability, and narrowly recognized database connection/shutdown failures return the safe 503 problem while programming errors remain 500s. Ordinary authenticated browsing measured 13 total SQL statements; activity-refresh browsing measured 16 including session persistence. Initial `bccbca0` results remain explicitly pre-fix in the plan. The [separate frontend cache-race repair](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#current-checkpoint-ci-limitation) passed independent review and local checks at `1a0994a`. All four [combined-checkpoint CI jobs](https://github.com/troja-gnister/aegis/actions/runs/35639373348) passed at `07704cf`; that checkpoint predates Task 11.

Task 11's validated browser data layer passed independent review at `d58fcb8`: 30 focused tests, 89 covering tests and all 96 frontend tests passed, with clean lint, TypeScript, production build and whitespace checks on Node 24.20. The five-page raw query cache is projected into a deduplicated current window; consumers must use the query observer's selected data, not manually flatten raw cached pages. Request, navigation and cleanup ownership follow the active session generation, and cleanup failures keep private access closed even after the originating login form unmounts. Earlier 78-test and 92-test results are pre-fix evidence preserved in the [correction history](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-11-correction-verification). All four [combined-checkpoint CI jobs](https://github.com/troja-gnister/aegis/actions/runs/35666075296) passed at completion checkpoint `dbfed7f`, before Task 12.

Task 12's dark virtualized file navigation passed independent review after corrections `c523f98` and `a82db75`. The first correction passed 96 covering and 129 full-frontend tests; the final isolated deferred-focus correction passed all 29 list/page tests plus lint, types, build/artifact and whitespace checks. The final synthetic Chromium probe passed at 320/390/412 pixels with bounded pages/rows, no overflow, 44-pixel controls, and preserved anchor/keyboard/focus behavior. Real-boundary tests cover deep-history restoration and delayed session cleanup. [Exact revisions, failures, corrections, and limits](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-12-correction-verification) remain recorded. Real-stack indexed-file journeys and physical-scale acceptance remain later tasks. All owned probe servers were stopped; no preview or original files were changed.

Task 12's exact code checkpoint `a82db75d9949170e1d22acfed0f08050f067edf2` passed overall [CI run 35737649769](https://github.com/troja-gnister/aegis/actions/runs/35737649769) and all four backend, frontend, deployment, and browser-journey jobs. The frontend job passed all 131 tests across 12 files. This does not close the later real-stack indexed-file or physical-scale gates.

## Prerequisites

Development paused on September 24; the [repository-root continuation handoff](../HANDOFF.md) contains the exact local source inventory, backup hashes, locked toolchain paths, pending PostgreSQL diagnostic correction and subsequent prerequisite sequence. The ten candidate source files remain uncommitted. The temporary rootless API is stopped and must be recreated with fresh ownership records before resource tests. Latest inspected checkpoint `f618bae` passed backend/frontend CI and failed deployment/browser journeys; no full compatibility or additional task acceptance follows.

Use Python 3.13, uv 0.12.8, Node.js 24 LTS, npm, and Git. The established verification path uses Docker with Compose v2 or newer; historical local evidence uses Docker Desktop/macOS and CI targets Ubuntu 24.04. Linux/rootless Podman compatibility is under separate verification and is not yet accepted. Tooling checkpoint `163c025` passes `make verify` with 1,452 backend and 147 frontend tests; the local application deployment gate remains failed. Pushed checkpoint `db6e107` also failed Docker CI deployment and browser journeys, while backend/frontend passed. Guarded duplicate-mask compatibility at `71da40e` passed focused actual mount and nested-mount checks without relaxing the parser. Its documentation checkpoint `2857a31` passed backend/frontend CI but still failed deployment and browser journeys. Its controlled launch interfaces require fresh version/behavior checks; do not apply the Podman overlay manually as an operator workaround. Canonical Podman merging is corrected at `520cf1f`, preserving the base security options. Isolated Caddy private-tmpfs write/recreation checks passed, but its application configuration and PostgreSQL bootstrap runtime remain unfinished. Podman 5.8.7 still requires those separately reviewed adaptations. Follow the [prerequisite report](verification/linux-podman-prerequisite.md) before attempting that path. A shell alias does not adapt Python subprocess calls. Run host commands as a non-root user. Dependencies and browser binaries require network access during installation; application workers do not get internet access.

Work on `main` for the current implementation workflow. For independent verification, create a separate detached Git worktree at the exact committed revision being tested. Start with an empty Git status, install from its tracked locks, and do not copy `.env`, secrets, or generated mount files from another checkout. Generated inputs must not conceal missing tracked files. Never share a production database with tests.

```bash
uv sync --locked --group dev
uv lock --check
npm --prefix frontend ci
```

Install the browser versions selected by the committed npm lock from `frontend/`:

```bash
npm exec -- playwright install chromium webkit
```

On the Ubuntu CI runner, use `install --with-deps chromium webkit` to install the required OS libraries too. That operation needs host package-install privileges. Do not install a different global Playwright version.

## Canonical verification

From the repository root, run these sequentially:

```bash
make verify
make verify-compose
make test-e2e
git diff --check
git status --short
```

| Command | Checks and resources |
| --- | --- |
| `make verify` | Frozen locks, Ruff, strict mypy, Django/migration checks, backend tests on temporary PostgreSQL 18, then npm install, lint, types, Vitest, and production build |
| `make verify-compose` | Generated test secrets, Compose validation, pinned image builds, and deployment/TLS/actual-database-role tests |
| `make test-e2e` | A complete disposable gateway/web/worker/PostgreSQL deployment, administrator bootstrap, idempotent Alice/Bob fixtures, mobile Chromium and WebKit |

The verification runner ignores inherited application/database configuration, generates a private password file, and uses a uniquely named PostgreSQL container with memory-only data on an automatically assigned loopback port. It removes that exact container and password file on exit. It never connects to an operator database. Deployment fixtures likewise create their own secrets and resources.

The browser runner reserves the Compose project `aegis-phase1-e2e` and loopback ports `18080` and `55432`. It refuses pre-existing project resources. Do not run it concurrently with itself or use that project name for anything else. Its explicit teardown removes its disposable containers, networks, and project-owned test volumes, including the synthetic database and indexer coordination. No test data is retained for recovery. Development/production volumes are not cleanup targets.

Do not run this harness where those ports belong to another deployment. The previous development computer had a user preview on `18080`; that does not establish a listener or ownership on another host. Check both ports before every run and leave unrelated services untouched. Task 14 must add a validated test-only port override/preflight for environments with conflicting ports. See the [cross-computer handoff](development-handoff.md#task-14-harness-prerequisites).

The browser suite uses the two tracked empty directories under [tests/fixtures/roots](../tests/fixtures/roots) as original-root fixtures. Unit and deployment regressions also create disposable synthetic source trees to test permissions, aliases, nested-mount rejection, and preservation; they never use user libraries. Host preflight examines root identities and bounded mount metadata without enumerating contents or writing inside originals. Supported roots may themselves be mountpoints but cannot contain descendant mounts; unavailable or unsupported host topology fails closed. Test-only identities cannot be seeded with production settings. The browser checks include direct/group grant isolation, an ungranted administrator, refresh/logout/history, protected URLs, 320/390-pixel layouts, keyboard activation, and reduced motion.

## Focused checks

Ruff and ESLint are the committed style gates; mypy and TypeScript check types. Run the checks before committing formatting or dependency changes.

```bash
uv run --locked ruff check backend tests scripts
uv run --locked mypy backend
uv run --locked python scripts/verify.py backend
uv run --locked python scripts/verify.py deployment
npm --prefix frontend test
npm --prefix frontend run lint
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

The `backend` and `deployment` runner modes include their own isolated database. A direct `pytest` invocation does not provision PostgreSQL; use it only with a deliberately configured disposable instance, never inherited production credentials. `make test` is the low-level pytest alias, not the self-contained acceptance command.

For a focused check, repeat `--test-target` with repository-relative existing files or directories beneath the selected mode's test tree:

```bash
uv run --locked python scripts/verify.py backend --test-target backend/tests/unit/catalog --test-target backend/tests/unit/common/test_verify_targets.py
uv run --locked python scripts/verify.py deployment --test-target tests/deployment/test_verification_runner.py
```

Optional file selectors use identifier-only `::ClassName::test_name` syntax; parameterized bracket selectors and pytest options are not accepted. Absolute paths, parent traversal, symlinks escaping the allowed tree, and cross-mode targets fail before any database is created. `compose` does not accept test targets. Backend focused checks still run Django checks and migration-drift detection; omitting targets preserves the full suite.

Catalog child names retain their exact filesystem bytes separately from plain-text display and ordering. Display escapes undecodable bytes, literal backslashes, and Unicode controls (including bidi controls); markup-shaped text remains literal data, not HTML. Order keys use NFC after case folding and do not establish raw identity. The 255-byte component domain expands at most six bytes per source byte, giving a 1,530-byte maximum key below the 2,048-byte defensive stored-key ceiling and PostgreSQL's index-tuple budget. Boundary fixtures and an exhaustive Unicode scalar expansion check cover escaping, canonical decomposition, and case folding; oversize keys fail rather than truncate. This domain foundation does not enumerate or open files and does not complete milestone 2A.

## Interactive deployment

Use the [deployment runbook](operations/phase-1-deployment.md). It establishes secret ownership, an explicit project name, mount preflight/rendering, migrations, readiness, bootstrap, and grants in the required order. The React production build is served through the gateway on the same origin as Django. `npm run dev` by itself is not an authenticated full-stack deployment.

For local-only HTTP, keep the published port on loopback and use development settings. Do not expose that configuration to other devices or the internet. For a phone on another device, configure trusted HTTPS as described in the runbook.

## Repository hygiene

Never commit `.env`, credential files, generated manifests/attestations, mount overrides, browser artifacts, virtual environments, or build caches. Frozen dependency changes must include the corresponding lockfile. Migrations are reviewed source and must be committed with model changes.

Playwright traces and video are disabled because they can retain credentials and session cookies. Failure screenshots mask inputs; form values are cleared before teardown snapshots. CI retains only masked synthetic screenshots for three days, not raw traces, reports, or error-context files. Service failure diagnostics are bounded and strip free-text messages. Never attach raw container logs or browser storage to an issue.

No cleanup command may target originals, a broad workspace path, or an existing application database volume. In particular, do not use a volume-deleting Compose command for a real deployment. See [safe shutdown](operations/phase-1-deployment.md#safe-shutdown).
