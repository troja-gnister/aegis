# Aegis

Aegis is a planned self-hosted, mobile-first file drive and media library. It will combine the focused drive features of Nextcloud with the browsing, filtering, and local organization features of PhotoPrism, without taking ownership of users' files or requiring cloud AI.

> **Project status: Phase 1 secure-platform foundation verified.** The active tree contains the Django/React foundation; the full file-drive and media-library features remain planned. The legacy Arch Linux hardening CLI remains recoverable as `legacy-hardening-cli-v0.1.0` at commit `1cb4277`.

The [platform design](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md) defines the product boundaries. The [Phase 2 delivery design](docs/superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md) consolidates the remaining v1 work into milestones 2A–2G. The [Phase 1 task ledger](docs/superpowers/plans/2026-08-31-phase-1-secure-platform-foundation.md#implementation-status) preserves the completed foundation checkpoints.

The [Phase 1 acceptance report](docs/verification/phase-1.md) records passing fresh-checkout and Linux suites, independent review, and remaining product/release limitations. Original roots containing nested filesystem mounts are rejected; use [separately declared, non-overlapping leaf roots](docs/operations/phase-1-deployment.md#slot-inspection-and-configuration).

## Product charter

Aegis will provide a secure browser interface for files mounted into its Docker deployment. It is designed first for phones and tablets, while remaining efficient on desktop browsers.

- Browse, search, upload, organize, preview, stream, and download files while keeping every mounted original unchanged.
- Handle at least 1,000,000 indexed entries and 50,000 entries in one folder on the initial target hardware.
- Show responsive photo and video libraries with thumbnails, metadata filters, and mobile viewers.
- Provide touch-friendly metadata filters, active filter chips, ranges, and saved searches across Files, Photos, and Search. Photo thumbnails and video posters load progressively without automatic grid playback.
- Preview PDF, plain text, CSV, and common spreadsheet files without downloading the complete file when avoidable.
- Support multiple accounts, groups, separately granted roots, and operation-level permissions.
- When AI is enabled, run it locally on CPU by default, optionally accelerate it with a GPU, and use frontier APIs only after explicit configuration and per-capability opt-in.
- Deploy as a small set of coordinated containers with PostgreSQL and explicit host or network-storage mounts.

## Principles and boundaries

1. **Mounted files are authoritative and read-only.** Originals keep their normal paths, remain usable without Aegis, and are never renamed, moved, overwritten, or deleted by an Aegis container. PostgreSQL is a rebuildable catalog plus the source of truth for users, grants, organization, jobs, and audit history.
2. **Browsing is indexed.** Normal API requests never enumerate large directories. Stable keyset cursors and bounded responses keep directory size from becoming request cost.
3. **Private and secure by default.** Authentication, authorization, root containment, safe content delivery, auditability, and internet-facing hardening are platform requirements.
4. **Local intelligence is optional.** Core file and media features work when AI is disabled or unavailable. Aegis never silently sends content to a cloud model.
5. **Mobile web is the primary client.** The PWA uses virtualized collections, touch-sized controls, resumable transfers, and a dark-first accessible interface.
6. **Scaling is measured.** Performance gates use representative million-entry fixtures and constrained baseline hardware, not only small developer datasets.
7. **Operational state is explicit.** Managed publications, indexing, media processing, and AI work are durable, observable, retryable jobs rather than hidden in web requests.

The first release does not include collaborative editing, desktop sync, WebDAV, public sharing, in-place modification of mounted originals, or automatic physical reorganization by AI. Those features require later design work rather than shortcuts in the v1 data model.

## Architecture at a glance

| Component | Responsibility |
| --- | --- |
| React + TypeScript PWA | Mobile-first files, photos, search, viewers, settings, and transfer manager |
| Django web/API | Same-origin sessions, authorization, metadata APIs, operation journal, admin, and internal delivery authorization |
| PostgreSQL | Accounts, grants, indexed catalog, organization, audit, durable jobs, and optional vectors |
| File operations role | Immutable upload/version publication and copy-on-write jobs in managed storage; read-only access to originals |
| Indexer role | Initial scans, filesystem events, checkpointed reconciliation, and catalog repair |
| Media role | Thumbnails, metadata, PDF/text/CSV extraction, video probing, and compatibility transcodes |
| Optional local AI role | CPU or GPU inference and preparation of explicitly approved provider payloads |
| Optional frontier connector | Audited, allowlisted provider egress without original-root mounts |
| Delivery gateway | Static assets, request limits, authorized byte-range delivery, and an optional automatic-TLS profile |

All application roles will ship from one versioned codebase and image where practical. PostgreSQL is the initial coordination and job store; Redis is not a required dependency. Originals stay unchanged on explicitly mounted read-only roots. Upload staging, immutable managed versions, derivatives, model data, and quarantine use separate role-scoped Aegis volumes. Only regenerable derivative caches may be evicted; originals and published managed versions are never byte-deletion targets.

## Feature matrix

Statuses describe implementation, not design approval:

- **Planned:** specified but implementation has not started.
- **In progress:** an active bounded plan or pull request is delivering it.
- **Implemented:** code and scoped tests landed, but the roadmap acceptance gate has not passed.
- **Verified:** the acceptance evidence and applicable phase gate passed.
- **Deferred:** intentionally outside the v1 roadmap.

Every feature change must update its row. Implemented and Verified rows link their commit, review, test report, benchmark, or recovery evidence; bundled work does not advance unrelated rows.

Phase 1 verification covers login/admin, granted root cards, authorization epochs, job/status primitives, and deployment boundaries only. It does not certify file-content delivery or per-file operations, live scan/disk-pressure metrics, indexing, or hostile-document/media processing; those are later phase gates.

Targets 2A–2G are milestones within the expanded Phase 2. A feature spanning milestones becomes Verified only when all its required behavior passes; design approval alone does not advance its status.

| ID | Capability | Status | Target | Evidence |
| --- | --- | --- | --- | --- |
| PLAT-001 | Docker Compose deployment with gateway, web, workers, PostgreSQL, and explicit roots | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| PLAT-002 | Dependency-aware liveness, readiness, and schema gates | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| OPS-002 | Structured logs, worker heartbeat, queue status, and reserved scan/pressure fields | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| AUTH-001 | Credential login with Argon2id and revocable server-side sessions | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| AUTH-002 | Multiple users and groups with administrative management | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| AUTH-003 | Additive per-user/group root grants and authorization epochs | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| AUTH-004 | Optional administrator-enforced TOTP | Planned | 2G | — |
| FILE-001 | Indexed directory API with stable keyset cursor pagination | Planned | 2A | — |
| FILE-002 | Deployment-declared, alias-checked mount slots and logical roots | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| FILE-003 | Resumable uploads into immutable managed storage with progress and conflict handling | Planned | 2B | — |
| FILE-004 | Copy-on-write folders, copies, logical organization, and idempotent operation recovery | Planned | 2B | — |
| FILE-005 | Authorized downloads and browser-compatible byte-range streaming | Planned | 2A | — |
| FILE-006 | Reversible metadata archive/hide state with no source or managed-version byte deletion | Planned | 2B | — |
| FILE-007 | Filesystem event ingestion plus checkpointed full reconciliation | Planned | 2A | — |
| FILE-008 | Permission-safe filename and path search | Planned | 2A | — |
| UX-001 | Dark responsive authenticated application shell | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| UX-002 | Virtualized mobile Files UI and persistent resumable transfer manager | Planned | 2A/2B | — |
| UX-003 | Installable PWA with Files, Photos, Search, and More navigation | Planned | 2C | — |
| UX-004 | Reconnectable server-sent operation, transfer, and job progress | Planned | 2A/2B | — |
| UX-005 | Mobile metadata-filter panel, active chips, multi-select/ranges, and saved searches | Planned | 2A/2C/2E | — |
| MEDIA-001 | Responsive thumbnails, video posters, and media probing | Planned | 2C | — |
| MEDIA-002 | Keyset photo timeline, date/type filters, and virtualized grids | Planned | 2C | — |
| MEDIA-003 | Touch-oriented photo and video viewing | Planned | 2C | — |
| MEDIA-004 | Original video range streaming with cached HLS fallback when required | Planned | 2C | — |
| DOC-001 | Progressive PDF viewer and page thumbnails | Planned | 2D | — |
| DOC-002 | Escaped, chunked text viewing with encoding detection | Planned | 2D | — |
| DOC-003 | Server-paged CSV viewing with bounded filtering | Planned | 2D | — |
| DOC-004 | Sandboxed viewing of common spreadsheet formats | Planned | 2D | — |
| DOC-005 | Non-collaborative text/CSV/spreadsheet edits and safe PDF annotations/forms as new immutable managed versions | Planned | 2D | — |
| META-001 | EXIF/GPS/date/type/size extraction and filtering | Planned | 2C/2E | — |
| ORG-001 | Albums, tags, ratings, favorites, and duplicate candidates | Planned | 2E | — |
| SEARCH-001 | Permission-safe combined filename and metadata search | Planned | 2E | — |
| SEARCH-002 | Extracted PDF/text full-text search | Planned | 2E | — |
| AI-001 | Optional local CPU embeddings, OCR, labels, and captions | Planned | 2F | — |
| AI-002 | Optional GPU acceleration using a deployment profile | Planned | 2F | — |
| AI-003 | Semantic search and virtual smart albums with provenance and confidence | Planned | 2F | — |
| AI-004 | Explicit per-capability frontier API connector | Planned | 2F | — |
| SEC-001 | Hardened same-origin edge and least-privilege container foundation | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| SEC-002 | Authentication, grant, and administration audit trail | Verified | Phase 1 | [Acceptance evidence](docs/verification/phase-1.md) |
| SEC-003 | Managed publication, version, copy, and archive audit trail | Planned | 2B | — |
| SEC-004 | Frontier egress audit trail and isolation tests | Planned | 2F | — |
| SEC-005 | Release security/rate-limit review and hostile-content gate | Planned | 2G | — |
| PERF-001 | Automated 1,000,000-entry and 50,000-entry-folder performance suite | Planned | 2A–2G | — |
| OPS-001 | Documented backup, restore, upgrade, failure-injection, and recovery workflows | Planned | 2G | — |
| OPS-003 | Live scan progress and staging/managed/derivative capacity metrics | Planned | 2A–2C | — |
| EXT-001 | WebDAV and desktop synchronization | Deferred | Later | — |
| EXT-002 | Controlled sharing | Deferred | Later | — |
| EXT-003 | Certified 10,000,000+ entry operation and storage adapters | Deferred | Later | — |
| EXT-004 | Collaborative editing | Deferred | Later | — |

## Roadmap

The current focus is **Phase 2A.1 — Indexed-browser implementation**, starting the indexed-drive milestone. Tasks 1–6 are complete: lossless filename handling, safely scoped verification, protected catalog schema, deployment-bound scan state, database scheduling/leases, a bounded read-only reader, and transactional catalog reconciliation. Checkpoints preserve logical organization and last-known metadata, reject stale supported transactions, and retain missing-file records without deleting originals. The [approved database commit-fence boundary](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-6-commit-fence-decision) excludes deliberate SQL-session constraint-timing tampering only from the absolute lease-deadline guarantee; root permissions and original-file protections are unchanged. Task 7, supervised scan execution, is implemented and awaiting independent review under its approved [reader-launch and restart coordination contract](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-7-runtime-coordination-decision). It adds a separate indexer-only lock volume and verified read-only root opening inside supervised children. Browsing APIs and new screens are not available yet. Metadata filters and photo/video thumbnails remain in the approved scope. The [Phase 1 secure foundation](docs/superpowers/plans/2026-08-31-phase-1-secure-platform-foundation.md) remains complete.

The first focused specification is [2A.1 — Indexed browser](docs/superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md): catalog, safe checkpointed scans, cursor API, basic metadata filters, virtualized mobile browsing, and early scale measurements. Event-driven reconciliation, broader search, reconnectable progress, and downloads remain required follow-up work within 2A; this first package alone cannot complete the milestone.

Its [implementation plan and task ledger](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#status-and-task-ledger) define **18 tasks: 6 complete, 12 remaining**. These cover source identity/schema, maintenance authority, safe scanning/recovery, filters/cursor APIs, mobile browsing, real-stack tests, catalog/filesystem/mobile benchmarks, and the operating/acceptance handoff. This is the count for 2A.1 only, not all remaining v1 work. Task 7 revision `6774cd2` passed 994 full-backend tests, 229 affected deployment tests, and full lint/type checks; independent review remains pending. All four [Linux CI jobs](https://github.com/troja-gnister/aegis/actions/runs/34989680413) passed at earlier checkpoint `eb68390`; that result does not certify Task 7. Historical task evidence remains in the plan. The synthetic 50K iterator checks allocation, not physical throughput; the 1M/50K package gates remain open. Local nested-mount failures remain [explicitly tracked](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md#task-4-deployment-verification-limitation), separately from Linux results.

The September 13 foundation checkpoint completed **all 14 Phase 1 tasks**: 620 backend tests, 235 Linux deployment tests, 43 frontend tests, and eight Chromium/WebKit journeys passed. Runbooks, accepted review, the enforced nested-mount restriction, and fresh-checkout verification are recorded in the [acceptance report](docs/verification/phase-1.md).

On September 14, the remaining v1 scope was consolidated into **seven Phase 2 milestones, zero accepted so far**. The first package has 18 planned tasks; task counts for subsequent packages are not yet assigned. Seven milestones does not mean seven implementation tasks. The [delivery ledger](docs/superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md#9-delivery-ledger-and-documentation) tracks the packages and evidence. The former Phase 3–6 capabilities are included below, not postponed beyond Phase 2.

| Phase | Deliverable | Acceptance gate | Status |
| --- | --- | --- | --- |
| 0 — Design and checkpoint | Canonical README/specification, legacy release tag, and first bounded implementation plan | Design reviewed, legacy state recoverable by name, and clean documentation checkpoint | Verified |
| 1 — Secure foundation | Django/React/PostgreSQL/Compose skeleton, same-origin auth, role-scoped credentials/volumes, mount slots, users/groups/grants, fenced job/operation primitives, health, and CI | A user can sign in and reach only an authorized root shell through the least-privilege deployed stack | Verified |
| 2 — Complete v1 application | Indexed drive, managed files, photo/video/document viewing and editing, metadata filters, organization/search, optional local models, and release hardening | All milestones 2A–2G pass their feature, scale, security, browser, and recovery gates | In progress |
| Later | WebDAV/sync, controlled sharing, 10M+ certification, storage adapters, and separately specified collaboration | Each capability receives its own approved specification and scale/security gate | Deferred |

### Phase 2 milestones

| Milestone | Deliverable | Acceptance gate | Status |
| --- | --- | --- | --- |
| 2A — Indexed drive | Scans/reconciliation, cursor browsing, filename/path search, basic filters, downloads/ranges, scan status, and initial 1M/50K fixtures | Read-side UI/API workflows, mount-loss recovery, authorization, and scale measurements pass | In progress |
| 2B — Managed files | Resumable uploads, immutable versions/copies, managed folders, logical organization, archive/restore, and transfers | Restart/revocation/conflict journeys preserve every original and published version | Planned |
| 2C — Photos and videos | Photo thumbnails, video posters, timeline, metadata filters, viewers, compatible playback, and installable PWA navigation | Private preview delivery, bounded media processing, and mobile interaction/load gates pass | Planned |
| 2D — Documents | PDF/text/CSV/spreadsheet viewers and non-collaborative editing as new immutable versions | Progressive viewing and save/conflict/hostile-document journeys pass | Planned |
| 2E — Organization and search | Rich metadata/full-text filters, albums, tags, ratings, favorites, saved searches, and duplicate review | Queries, suggestions, and review actions remain permission-safe and metadata-only | Planned |
| 2F — Local intelligence | Optional CPU models, optional GPU, semantic search, provenance, smart albums, and opt-in frontier providers | Core app works with models off/failed; no unapproved provider egress occurs | Planned |
| 2G — Release readiness | Integrated performance/security tests, optional TOTP, backup/restore, failure injection, upgrades, and operations | Full benchmark, deployment, upgrade, and documented recovery exercises pass | Planned |

Performance and security checks begin in 2A and grow with the workload; they are not postponed until 2G. Core metadata filters and thumbnails do not require model inference. Each milestone is delivered through smaller reviewed packages and regular commits on `main`.

## Performance contract

Initial measurements target an x86 home server or NAS with 4–8 CPU cores, 8–16 GB RAM, PostgreSQL on local storage, and no required GPU.

Acceptance uses the fixed [reference benchmark profile](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md#53-reference-benchmark-profile), including container memory limits, workload mix, storage calibration, and mobile network/device conditions.

| Scenario | Budget |
| --- | --- |
| Indexed directory page | p95 server time at or below 300 ms on baseline hardware |
| Browse while configured workers are active | p95 server time at or below 500 ms |
| Mobile largest contentful paint | at or below 2.5 s for the defined test journey |
| Mobile interaction to next paint | at or below 200 ms for the defined test journey |
| Scale fixture | at least 1,000,000 total entries and 50,000 direct children in one folder |

Directory APIs use bounded page sizes, compact list records, compound indexes, and keyset cursors. They do not perform deep `OFFSET` pagination, synchronous directory scans, or exact global counts on normal browse requests. The catalog and API identifiers are designed so later partitioning and 10M+ certification do not require a client-facing semantic rewrite.

## Security contract

- Mutations fail closed when authorization state or PostgreSQL is unavailable.
- Browsers receive opaque object/root identifiers, never trusted absolute filesystem paths.
- Filesystem operations stay beneath the authorized root; symlink following is disabled by default.
- The gateway serves content only after short-lived internal authorization from Django.
- Active or unknown content is downloaded as an attachment; browser-rendered text is escaped.
- Originals and APIs are not stored in browser caches; authenticated thumbnails must revalidate against the current session and authorization epoch.
- Secrets are injected through Docker secrets or protected configuration and are redacted from logs.
- Application containers run unprivileged with minimal mounts and capabilities; every mounted original is read-only, and web receives no originals. PostgreSQL's constrained storage/secret bootstrap drops to its database UID before serving clients and has no original-root mounts.
- Original roots must be non-overlapping and contain no nested filesystem mounts. The selected root may itself be a mountpoint; ordinary subdirectories are allowed. Host setup and runtime attestation fail closed on unsupported topology.
- Web, processing, and database roles are outbound-isolated. The ingress gateway and optional public TLS front are documented exceptions; gateway outbound blocking is not currently guaranteed.
- Future uploads, copies, and edits create immutable versions in a separate managed append-only store. No API or job permanently deletes original or versioned content; cleanup is limited to regenerable derivatives and unpublished Aegis-owned temporary artifacts outside original roots.
- Duplicate detection only labels candidates for human review. Accepting or dismissing a duplicate never deletes, moves, renames, or overwrites either file.
- Cloud model use is disabled by default, explicitly selected per capability, and recorded in the audit trail.

## Development and documentation

The Django backend, React shell, role-separated containers, and browser/CI gates are verified for Phase 1. Review and clean-install evidence are recorded; the expanded Phase 2 scope is approved and its first indexed-browser package is in implementation. Work packages have bounded plans so task counts, scale, security, and recovery gates stay visible.

- [Development and verification](docs/development.md)
- [Deployment and operation](docs/operations/phase-1-deployment.md)
- [Phase 1 acceptance evidence and limitations](docs/verification/phase-1.md)
- [Approved platform design](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md)
- [Phase 2 delivery design and milestone ledger](docs/superpowers/specs/2026-09-14-phase-2-v1-delivery-design.md)
- [Phase 2A.1 indexed-browser specification](docs/superpowers/specs/2026-09-14-phase-2a-indexed-browser-design.md)
- [Phase 2A.1 implementation plan and task ledger](docs/superpowers/plans/2026-09-14-phase-2a-indexed-browser.md)
- [Approved Phase 1 implementation plan](docs/superpowers/plans/2026-08-31-phase-1-secure-platform-foundation.md)
- License: [MIT](LICENSE)

Implementation changes include tests appropriate to their risk, preserve the stable feature IDs above, and update the feature matrix and roadmap when a verification gate advances.
