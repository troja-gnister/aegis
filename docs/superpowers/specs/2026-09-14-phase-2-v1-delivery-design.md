# Phase 2 — Complete v1 delivery design

**Date:** September 14, 2026

**Status:** Written specification approved on September 14, 2026, including scope and filter/thumbnail behavior. The first implementation plan is in progress; no Phase 2 package is accepted yet.

**Canonical status:** [README roadmap](../../../README.md#roadmap)

**Foundation:** [Platform design](2026-08-31-aegis-platform-rewrite-design.md) and [verified Phase 1](../../verification/phase-1.md)

## 1. Decision and scope

Phase 2 now delivers the complete v1 application. The former Phase 2–6 features are consolidated into seven milestones, not dropped or considered implemented. Phase 1 remains verified; its evidence is historical and unchanged.

The stack remains Django, PostgreSQL, React/TypeScript, role-separated workers, Nginx delivery, and Docker Compose. PostgreSQL is the catalog and initial durable-work store. No required Redis, separate search service, or microservice migration is introduced.

This document governs delivery order and shared contracts. Each work package receives a focused specification and implementation plan. One milestone can contain multiple independently verified packages; a package is not automatically a completed milestone. The first package, [2A.1 — Indexed browser](2026-09-14-phase-2a-indexed-browser-design.md), builds actual indexed browsing on the existing authorized-root shell. The remainder of 2A still requires event reconciliation, broader filename/path search, reconnectable progress, and authorized byte delivery before that milestone can pass.

WebDAV/sync, public sharing, collaboration, arbitrary storage adapters, full nested-mount support, and certified 10M+ operation remain outside v1. The public identifiers and cursor contracts must allow later scale work without changing their meaning.

## 2. Milestones and dependencies

| Milestone | Deliverable | Dependencies | Completion evidence |
| --- | --- | --- | --- |
| 2A — Indexed drive | Catalog, checkpointed scans, event/periodic reconciliation, filename/path search, authorized downloads/ranges, mobile Files browser, basic filters, scan status, and initial scale fixtures | Verified Phase 1 | Real indexed browse/search/download workflows, root isolation, scan failure recovery, and read-side scale evidence |
| 2B — Managed files | Resumable uploads, immutable versions/copies, managed folders, logical organization, metadata-only archive/restore, operation audit, and persistent transfer UI | Catalog, authorization, containment, and progress contracts from 2A | Interrupted uploads and copies recover; revocation fences execution; originals and every published version remain unchanged |
| 2C — Photos and videos | Installable PWA navigation, responsive photo thumbnails, video posters, metadata extraction/filters, timeline, photo/video viewers, and on-demand compatible playback | 2A catalog/delivery; 2B publication integration | Real photo/video browsing, private derivative delivery, bounded processors, and mobile interaction/load tests |
| 2D — Documents | Progressive PDF/text/CSV/XLSX/ODS viewing, extraction, and non-collaborative editing as new immutable versions | 2B versions; 2C processor and derivative boundaries | Viewer/save journeys, hostile-document limits, explicit save conflicts, and original/version preservation |
| 2E — Organization and search | Rich combined filters/search, extracted-text search, albums, tags, ratings, favorites, saved searches, and duplicate review | Indexed extraction and stable identities from 2A–2D | Permission-safe queries and suggestions; human metadata survives reconciliation/reprocessing; duplicate actions change no bytes |
| 2F — Local intelligence | Optional local CPU models, optional GPU, OCR/labels/captions/embeddings, semantic search, smart albums, and explicitly configured frontier connector | 2C–2E asset revisions, metadata, and processor boundaries | Core app passes with models off/failed; local processing has no egress; provider jobs have explicit consent, isolated payloads, and audit |
| 2G — Release readiness | Full-workload scale measurements, browser/accessibility/security gates, optional TOTP, backup/restore, upgrades, resource tuning, and recovery drills | Every required v1 capability | Reproducible benchmark and security reports plus exercised deployment, upgrade, backup, and recovery runbooks |

Milestone 2G is the final integration gate, not the first security or performance check. Each preceding package ships its own tests and evidence. Photo/video/document functionality is required before Phase 2 completion; it is not postponed to a separate later phase.

## 3. Original and managed-content protection

- Every original root stays read-only in every mounted role. Web receives no originals. No API, job, editor, duplicate action, or recovery path may create, unlink, overwrite, move, or rename original files.
- Roots containing descendant filesystem mounts remain rejected. Ordinary subdirectories are allowed, and a selected root may itself be a mountpoint. Operator-controlled mount topology must remain stable during setup and be revalidated after changes.
- Physical source identity and logical organization are separate. Reconciliation updates observed source metadata, never user-authored organization or published-version history.
- Uploads, copies, and edits use separate Aegis-owned storage and publish new immutable versions. Prior published versions are not retention-deletion targets. Archive/hide/restore is metadata only.
- Duplicate detection creates candidate labels and review decisions. Neither acceptance nor dismissal deletes, replaces, moves, or renames content.
- Only regenerable derivatives and unpublished, operation-owned temporary artifacts outside original roots may be cleaned. Source files, published managed versions, and existing operator databases are never test-cleanup targets.

These rules preserve the [platform's publication and recovery contract](2026-08-31-aegis-platform-rewrite-design.md#10-non-destructive-file-command-lifecycle); they do not claim that a compromised host administrator cannot change storage outside Aegis.

## 4. Shared catalog, query, and authorization boundary

Scanning and extraction are background work. Browser list, search, and filter requests query the catalog only; they never enumerate a directory or extract metadata as a fallback.

The query layer accepts a versioned, allowlisted filter representation, not SQL, arbitrary field names, filesystem paths, or an unrestricted expression language. Cursors bind the user/session authorization context, permitted roots, directory, normalized filters, sort, direction, contract version, and expiry. Filters reset pagination. All orderings have deterministic opaque-ID tie-breakers.

Default pages contain 100 entries; the server maximum remains 250. Details and rich metadata load separately. No ordinary browse request computes an exact total or loads a whole directory. Browser caches have explicit page and memory bounds in addition to row/grid virtualization.

Authorization constrains results, autocomplete options, saved searches, derivative requests, and any displayed counts. A root or account revocation invalidates stale cursors and private UI state. A platform administrator still receives no implicit product-data access.

Root-level catalog maintenance is distinct from actor-bound operations: scans maintain an administratively activated root, while uploads, copies, organization, and edits remain bound to a live requesting principal. The first package must specify a narrow root-maintenance authority and cannot implement this by bypassing the existing operation authorization checks or inventing a privileged user account.

## 5. Metadata filtering in the web UI

Files, Photos, and Search use one filter model with context-appropriate controls. This is visible product functionality, not an API-only feature. The [PhotoPrism filter reference](https://docs.photoprism.app/user-guide/search/filters/) informs familiar groupings; Aegis does not promise compatibility with its entire query language.

| Group | Controls | Delivery |
| --- | --- | --- |
| File attributes | Authorized root/folder, filename, kind/type, size, modified date, and indexed availability | 2A |
| Capture and image metadata | Capture date/year/month, camera/lens, ISO/aperture, dimensions/resolution, and orientation | 2C |
| Video metadata | Duration and codec, alongside capture and dimension filters | 2C |
| Location | Present/missing location and available location values when enabled for the root | 2C/2E |
| Human organization | Albums, tags, favorites, ratings, duplicate-review state, and saved searches | 2E |
| Optional enrichment | Model-derived labels and semantic search; people only if the separately controlled face-grouping capability is enabled | 2F |

Filters combine with AND across categories and OR within a multi-select category. Numeric/date controls express explicit ranges. Unsupported fields or invalid combinations return a clear validation error; they are never silently ignored. Missing metadata is distinct from pending extraction, failed extraction, and genuinely absent values. Capture date and file modification date remain separate; a fallback sort date must not be labeled as a known capture date.

On phones, a Filters button opens a touch-friendly panel with grouped controls, removable active chips, clear-all, and explicit Apply/Cancel. Saved searches persist on the server under the owning user. Desktop may use a side panel with the same semantics. Typing cancels obsolete requests and does not issue an expensive query for every keystroke.

Large facet dictionaries use bounded, permission-scoped autocomplete. Exact live counts are not required for each option; omit counts unless a bounded precomputed or asynchronous result exists, and label its generation/time. Filter values, counts, and options cannot reveal roots the caller cannot browse.

Core file/media filters do not require model inference. Location features may not silently call an external geocoder or map service. Local metadata or an explicitly installed local location dataset supplies offline values; any future network-backed lookup requires a separate opt-in and privacy review.

## 6. Photo and video thumbnails

Milestone 2C provides responsive image thumbnails and video poster images with play indicators and duration badges. Browsing a grid never starts video playback automatically. Files and Photos share the derivative contract; a file browser can show media thumbnails without opening the full-screen viewer.

The media worker generates thumbnails/posters asynchronously in bounded, isolated jobs. The first indexed result can display an appropriate icon immediately. Pending, failed, unsupported, and ready previews have distinct safe states and an authorized retry action. Browser requests never synchronously decode or transcode an original in the API process.

Derived keys include source revision, processor version, and transformation parameters. A source change invalidates stale derivatives. Small screens request an appropriately sized preview rather than the original or the largest available image. Virtualized grids, bounded neighbor prefetch, bounded page caching, and cancellation protect mobile memory and bandwidth.

Derivative delivery checks the current session and root grant. Thumbnail responses follow the existing private revalidation policy: strong ETag, `Cache-Control: private, no-cache`, `Vary: Cookie`, and user/session/root-epoch cache namespaces. Service workers never cache authenticated derivatives or originals. Logout, expiry, account switch, and grant changes clear private state before previously viewed content can reappear.

Photo/video thumbnails must work with all optional model providers disabled. They are regenerable files in derivative storage, not modified originals. Actual decoder/encoder versions, supported formats, image sizes, and sandbox limits belong to 2C's focused processor specification and are not certified by the first catalog package.

## 7. Performance gates

The [reference benchmark profile](2026-08-31-aegis-platform-rewrite-design.md#53-reference-benchmark-profile) and [performance contract](2026-08-31-aegis-platform-rewrite-design.md#15-performance-contract) remain binding. Consolidation does not relax their dataset, authorization, workload, or hardware assumptions.

Measurement begins with the first catalog package:

- deterministic catalog and synthetic filesystem fixtures with at least 1,000,000 entries and 50,000 direct children in a folder;
- named cold and warm runs, query plans, payload size, query count, memory, and index size;
- p95 directory pages at or below 300 ms and at or below 500 ms under the applicable worker load;
- indexer RSS at or below 750 MiB, catalog tables plus indexes at or below 8 GiB, and initial catalog-only scan within four hours on the calibrated root;
- mobile LCP at or below 2.5 seconds, INP at or below 200 ms, and heap at or below 250 MiB during the defined long browsing journey.

Early catalog-only fixtures establish query behavior, not filesystem throughput. Early scan-only load does not certify preview/transcode/model contention. Real media and document workloads are added when their milestones land, and 2G repeats the integrated gates with optional-model profiles reported separately.

Local Apple-silicon and hosted CI results are useful regression evidence but do not certify the specified Linux x86-64 N100-class reference machine. The final report must identify the actual host and calibration; if the reference environment has not been exercised, its acceptance gate remains open.

## 8. Verification and operational rules

Every package adds unit/property tests, real-PostgreSQL integration tests, applicable Docker boundary tests, and mobile Chromium/WebKit journeys. Fault tests cover worker/process loss, stale leases, revoked grants, unavailable/replaced mounts, failed enumeration, disk pressure, and publication/database interruption where relevant.

Existing Phase 1 identity, admin, mount, audit, and role-privilege tests remain regression gates. Database migrations must preserve existing clusters and explicitly extend the role allowlists; broad runtime grants are not a migration shortcut. Verification uses isolated synthetic sources and disposable databases, never an operator library or existing application database.

Milestone 2G includes actual backup/restore and upgrade exercises. Adding managed versions adds mandatory backup state. Quiescing Aegis alone does not quiesce external host/SMB/NFS writers; recovery reports must retain the platform's application-consistent versus crash-consistent distinction.

The accepted gateway/TLS ingress egress exception remains explicit. Internal web, database, processing, and local-model roles must have outbound-denial tests. An isolated, opted-in frontier connector is not permission to grant general egress or original mounts to a processor.

Work continues on `main`, with scoped verified commits and regular pushes. No automatic release tag or production deployment follows from a milestone being accepted.

## 9. Delivery ledger and documentation

As of this implementation checkpoint: seven milestones defined, zero accepted. The [2A.1 implementation plan](../plans/2026-09-14-phase-2a-indexed-browser.md#status-and-task-ledger) defines 18 tasks, five complete and 13 remaining. Filename handling, focused verification, protected catalog schema, deployment-bound scan state, database-enforced scheduling/leases and the bounded read-only reader are implemented; transactional catalog reconciliation is next. Local deployment evidence limitations remain recorded in the plan, with no package acceptance. Subsequent package task counts have not yet been assigned; neither seven milestones nor 18 first-package tasks is the full remaining rewrite task count.

| Milestone | Design/implementation state | Accepted packages | Acceptance evidence |
| --- | --- | --- | --- |
| 2A | [2A.1 specification](2026-09-14-phase-2a-indexed-browser-design.md) approved; [18-task plan](../plans/2026-09-14-phase-2a-indexed-browser.md) in progress | 0 | No package acceptance yet |
| 2B | Planned | 0 | None yet |
| 2C | Planned, including metadata UI and photo/video thumbnails | 0 | None yet |
| 2D | Planned | 0 | None yet |
| 2E | Planned | 0 | None yet |
| 2F | Planned | 0 | None yet |
| 2G | Planned | 0 | None yet |

The README retains stable feature IDs and becomes the canonical milestone/status index. Package plans own executable task counts; this ledger links them as they are approved. A feature spanning several packages becomes Verified only after all its required behavior and acceptance checks pass. Writing a design or creating a fixture does not advance implementation status.

## 10. Review boundary

The expanded v1 scope, milestone order, metadata UI, and photo/video thumbnail behavior were approved in conversation. The user approved this written specification and the focused 2A.1 specification on September 14, 2026; the first implementation plan can now proceed. Subsequent packages refine their subsystem contracts without reopening approved product boundaries; any change to original protection, authorization, egress, or benchmark budgets requires an explicit amendment.
