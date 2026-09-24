# Development handoff

## Checkpoint

Resumed September 23, 2026, with a separate Linux/Podman prerequisite after Task 12 and before starting Task 13. Continue on `main`, with scoped commits and regular pushes. The [README roadmap](../README.md#roadmap) and [Phase 2A.1 task ledger](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#status-and-task-ledger) are the public progress record.

**18 tasks: 12 complete, 6 remaining** in Phase 2A.1. Tasks 1–12 are accepted. The last accepted application revision is `a82db75d9949170e1d22acfed0f08050f067edf2`; the original portable handoff is `ab57e3037b9daed79aeb691eb486a4d2397ae4aa`. Both are present in `origin/main` history. Task 12 began at `913937f`, with cleanup, deep-history and focus corrections at `c523f98` and `a82db75`. Independent review and two scoped correction reviews closed all findings without remaining issues. No Task 13 or Task 14 implementation has started.

Final local evidence at `a82db75`: 29 list/page tests, lint, TypeScript, production build/artifact and whitespace checks passed. The full 129-test frontend run and 96 covering tests belong to preceding correction `c523f98`. The final synthetic Chromium probe passed at 320/390/412 pixels: zero horizontal overflow, 44-pixel controls, five presented pages, 14 rendered rows, preserved anchor and paging focus, nearest offscreen focus fallback, and no unintended restoration fetch. All owned browser/server processes are stopped. [Detailed evidence and limits](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-12-correction-verification) remain in the plan.

The exact final application revision passed overall [CI run 35737649769](https://github.com/troja-gnister/aegis/actions/runs/35737649769) and all four backend, frontend, deployment, and browser-journey jobs. Its frontend job passed all 131 tests across 12 files.

This package has 18 tasks, not the entire rewrite. Later indexed-drive packages and milestones 2B–2G remain required. Thumbnails, media/document viewers, editing, and the 1M-entry/50K-folder acceptance gates are not delivered by this checkpoint.

## Current Linux prerequisite

The [September 22–24 environment report](verification/linux-podman-prerequisite.md) separates scoped runtime evidence from complete application compatibility. Reviewed local-engine tooling at `163c02563d95de8e4e3930ae68377e946feef3b8` passed `make verify`: 1,452 backend tests, 147 frontend tests, static checks and production frontend build. Its earlier failed attempts remain in the report. Pushed checkpoint `db6e10765e000cee7335244444b21434079bdac1` failed [CI run 35989701646](https://github.com/troja-gnister/aegis/actions/runs/35989701646): backend/frontend passed, deployment/browser journeys failed. The later documentation checkpoint `2857a3168bccee34e9116d1ee2493caa5658be0b`, containing mask source `71da40e`, also failed [CI run 36027572227](https://github.com/troja-gnister/aegis/actions/runs/36027572227): backend/frontend passed, deployment/browser journeys failed. Detailed errors remain unavailable; no cause is assigned from the public exit codes alone.

Guarded duplicate-mask compatibility is committed at `71da40ed34bbe05990093afbf12925df40462ccf`, after independent review, 346 engine/adapter tests and focused actual mount checks. The runtime selection passed 25 cases and exposed one stale assertion: the observer rejected the nested mount, but the test matched its outer error instead of the direct cause. The reviewed test-only correction passed all 10 targeted fake cases and the affected actual observer retry; disposable databases were removed and container inventories were empty. The report preserves intermediate capability/timezone refusals and their narrow fixes. Fresh native/Compose behavior checks precede each guarded launch, and the strict mount parser remains unchanged. Canonical overlay merging is corrected at `520cf1f5bb9d5126953d33b0ce37751080ef7db8`: the overlay adds the mask option while preserving NNP from the base. The reviewed local PostgreSQL candidate remains uncommitted and failed its first bootstrap run after the merge correction; diagnosis continues with no relaxed assertion. Isolated Caddy private-tmpfs write/recreation and strict-parser checks passed with exact cleanup. Finish PostgreSQL runtime verification and Caddy's separate application configuration, then rerun full deployment/browser gates and resolve Docker CI failures before Task 13. No local Podman application browser gate has run; full compatibility is not accepted.

The isolated WebKit synthetic test container and the bounded mount probes were removed after exact-identity checks; uniquely owned images/build cache are retained. After the failed deployment gate, no containers remained, but 18 volumes and 23 test networks were retained because admission rules did not recognize all declared resources. Their later observation is not a creation identity ledger and does not authorize deletion. The old temporary rootless API process and socket were absent on resumption; a newly owned foreground Unix API is active during prerequisite testing and must be stopped with identity checks at the next stopping checkpoint. Private refusal-test diagnostics are retained without name-based cleanup. A fresh clone includes the committed tooling but none of those local resources or the ignored toolchain/diagnostic artifacts. Use this public status to retain Tasks 1–12 acceptance while completing the remaining runtime prerequisite; the tooling checkpoint does not establish application compatibility.

## Resume on another computer

1. Clone or fetch the repository and fast-forward `main`. Check `git status --short` before changing anything; preserve local changes.
2. Read this handoff, the task ledger, the [indexed-browser specification](superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md), and the next task's full requirements. Do not restart completed tasks or repeat their historical reviews.
3. Follow [development prerequisites and frozen installs](development.md#prerequisites): Python 3.13, uv 0.12.8, Node.js 24 and PostgreSQL 18. The established runtime is Docker Compose; the [Linux/Podman prerequisite report](verification/linux-podman-prerequisite.md) records the pending alternative. Use the tracked Python/npm locks and their Playwright versions.
4. Recreate only explicitly owned development/test resources. Local credentials, mounts, volumes, ignored reports, browser artifacts, and running processes do not travel with Git. Do not copy secret files or assume the previous computer's paths exist.
5. Finish the reviewed Linux/Podman prerequisite and its applicable runtime gates, then resume task-by-task implementation at Task 13. Keep the README, public task ledger, verification evidence, and this handoff consistent as checkpoints land.

The public ledger records accepted revisions and review/test outcomes even when ignored working notes are absent on a fresh clone. Rebuild working notes from those records rather than redispatching completed tasks.

## Next: Task 13 integration notes

Task 13 adds applied/draft metadata filters, active chips, bounded details, scan status, and authorized rescan interactions. Consume the query observer's selected, deduplicated window; raw cached pages may overlap. Keep at most five active pages, no inactive datasets, and bounded namespace-owned navigation state. Never put private filter text or names into URLs, browser history state, or persistent storage.

Inspect the accepted `FilesPage` navigation/selection seam before wiring controls. Preserve real session revalidation on back/forward navigation: private content stays hidden while it settles, and obsolete responses cannot acquire a newer account's authority.

Resolve one narrow scope dependency before implementation: `frontend/src/features/auth/api.ts` owns private CSRF state, while `requestScan(rootId, requestId, csrfToken)` needs its existing token. Add only a reviewed, generation-safe shared accessor and covering tests if needed; do not create a second token store or authentication system. Capture the initiating namespace across awaited token acquisition. Rescan requires the literal `root_admin` grant, not merely platform-administrator status, and retains its request UUID for retries.

Preserve decimal-string precision, unknown-value semantics, and explicit local-timezone date conversion. Active/idle status polling is 3/30 seconds, with failure backoff capped at 60 seconds and hidden/offline pauses. New scan progress must not refetch all retained pages after every batch. Native dialog/focus/date behavior still needs Chromium and WebKit evidence in Task 14.

## Task 14 harness prerequisites

The existing browser harness reserves project `aegis-phase1-e2e` and ports `18080`/`55432`. The previous computer had a user preview on `18080`; that is historical context, not a service discovered on this host. On September 23, the current host had no listeners on either reserved port and no Podman containers. Recheck before every harness run and never stop an unrelated service to free a port. Before Task 14 execution, implement and test a small validated test-only port override/preflight within its planned harness scope. Use separately checked loopback ports; preserve isolated CI defaults.

Replace the harness's tracked empty-root inputs with explicitly owned synthetic physical fixtures, not writes into tracked roots or user libraries. Task 14 requires at least 603 entries, real indexing/API journeys in both mobile browser engines, and a pre/post preservation manifest. Stop owned workers before descriptor-safe comparison and exact fixture cleanup. Unknown files or identity changes must retain diagnostics and prevent unsafe cleanup. Synthetic fulfilled-response probes are not real-stack acceptance.

The [Task 4 local nested-mount limitation](superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-4-deployment-verification-limitation) remains historical evidence, separate from successful Linux CI; do not erase it or weaken rejection tests.

## Non-negotiable safety boundaries

- Originals stay read-only in every mounted role; web receives none. No app feature may create, delete, rename, move, or overwrite originals. Duplicate handling is metadata-only review. Future edits create immutable managed versions outside originals.
- Existing user/operator deployments and databases are never test or cleanup targets. The previous computer's `aegis-preview` project, local preview export, Downloads mount, credentials, images, and volumes remain untouched. The preview was not refreshed to this checkpoint.
- Use canonical disposable-database verification, never inherited application database settings. Run full verification/deployment/browser harnesses sequentially and only after checking their resource boundaries.
- Do not commit secrets, generated mounts, raw logs, private screenshots, or browser storage. No release, production deployment, preview refresh, or destructive cleanup is implied by resuming implementation.
