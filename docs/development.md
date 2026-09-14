# Development

Phase 1 provides credential login, authorized root cards, administration, durable job primitives, and the deployment boundary. Its [acceptance evidence](verification/phase-1.md) records the tested revision, clean-install results, and limitations. It does not enumerate files, generate previews, or edit documents. Follow the [roadmap](../README.md#roadmap) for those later slices.

The [Phase 2 delivery design](superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md) groups the complete v1 build into milestones 2A–2G. Its scope includes metadata-filter controls and photo/video thumbnails. Specifications and plans are not implementation evidence: keep feature statuses Planned until their work begins, and retain the original-file and isolated-test boundaries throughout.

The first work package is [2A.1 indexed browsing](superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md). Its [implementation plan](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md) defines 18 tasks: three completed and 15 remaining. The filename domain, focused verification commands, protected catalog schema and deployment-bound scan state are implemented; reviewed Task 3 revision `3a11b83` passed 728 backend tests and 34 actual-role/cleanup tests. Deployment installs the scan configuration alongside schema and counter guards; scheduling and worker execution are not enabled yet. No browsing API, screen or Phase 2 benchmark result is claimed yet.

## Prerequisites

Use Python 3.13, uv 0.12.8, Node.js 24 LTS, npm, Git, and Docker with Compose v2 or newer. Docker must be running. Development uses Docker Desktop/macOS; CI targets Ubuntu 24.04. Run host commands as a non-root user. Dependencies and browser binaries require network access during installation; application workers do not get internet access.

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

The browser runner reserves the Compose project `aegis-phase1-e2e` and loopback ports `18080` and `55432`. It refuses pre-existing project resources. Do not run it concurrently with itself or use that project name for anything else. Its explicit teardown removes its disposable containers, networks, and four test volumes, including the synthetic database. No test data is retained for recovery. Development/production volumes are not cleanup targets.

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
