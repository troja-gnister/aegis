# Aegis

Aegis is a planned self-hosted, mobile-first file drive and media library. It will combine the focused drive features of Nextcloud with the browsing, filtering, and local organization features of PhotoPrism, without taking ownership of users' files or requiring cloud AI.

> **Project status: approved design; rewrite not yet implemented.** The current source tree is the legacy Arch Linux hardening CLI at commit `1cb4277`. Phase 0 will preserve that release with a named Git tag, and Phase 1 will replace the active tree with the web platform foundation. Do not treat the current binary as the product described below.

The canonical platform design is [docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md).

## Product charter

Aegis will provide a secure browser interface for files mounted into its Docker deployment. It is designed first for phones and tablets, while remaining efficient on desktop browsers.

- Browse, search, upload, organize, preview, stream, download, delete, and restore files.
- Handle at least 1,000,000 indexed entries and 50,000 entries in one folder on the initial target hardware.
- Show responsive photo and video libraries with thumbnails, metadata filters, and mobile viewers.
- Preview PDF, text, and CSV files without downloading the complete file when avoidable.
- Support multiple accounts, groups, separately granted roots, and operation-level permissions.
- When AI is enabled, run it locally on CPU by default, optionally accelerate it with a GPU, and use frontier APIs only after explicit configuration and per-capability opt-in.
- Deploy as a small set of coordinated containers with PostgreSQL and explicit host or network-storage mounts.

## Principles and boundaries

1. **Mounted files are authoritative.** Originals keep their normal paths and remain usable without Aegis. PostgreSQL is a rebuildable catalog plus the source of truth for users, grants, organization, jobs, and audit history.
2. **Browsing is indexed.** Normal API requests never enumerate large directories. Stable keyset cursors and bounded responses keep directory size from becoming request cost.
3. **Private and secure by default.** Authentication, authorization, root containment, safe content delivery, auditability, and internet-facing hardening are platform requirements.
4. **Local intelligence is optional.** Core file and media features work when AI is disabled or unavailable. Aegis never silently sends content to a cloud model.
5. **Mobile web is the primary client.** The PWA uses virtualized collections, touch-sized controls, resumable transfers, and a dark-first accessible interface.
6. **Scaling is measured.** Performance gates use representative million-entry fixtures and constrained baseline hardware, not only small developer datasets.
7. **Operational state is explicit.** Mutations, indexing, media processing, and AI work are durable, observable, retryable jobs rather than hidden in web requests.

The first release does not include collaborative editing, desktop sync, WebDAV, historical file versions, public sharing, or automatic physical reorganization by AI. Those features require later design work rather than shortcuts in the v1 data model.

## Architecture at a glance

| Component | Responsibility |
| --- | --- |
| React + TypeScript PWA | Mobile-first files, photos, search, viewers, settings, and transfer manager |
| Django web/API | Same-origin sessions, authorization, metadata APIs, operation journal, admin, and internal delivery authorization |
| PostgreSQL | Accounts, grants, indexed catalog, organization, audit, durable jobs, and optional vectors |
| File operations role | Upload publication, copy/move/delete work, trash retention, and crash recovery on writable roots |
| Indexer role | Initial scans, filesystem events, checkpointed reconciliation, and catalog repair |
| Media role | Thumbnails, metadata, PDF/text/CSV extraction, video probing, and compatibility transcodes |
| Optional AI role | CPU or GPU local inference and explicitly enabled frontier-provider tasks |
| Delivery gateway | Static assets, request limits, authorized byte-range delivery, and an optional automatic-TLS profile |

All application roles will ship from one versioned codebase and image where practical. PostgreSQL is the initial coordination and job store; Redis is not a required dependency. Originals stay on explicitly mounted roots. Generated assets, upload staging, and model data live in an Aegis-managed volume, while recoverable deletions use hidden trash inside each writable root.

## Feature matrix

Statuses describe implementation, not design approval: **planned** means specified but not built; **deferred** means intentionally outside the v1 roadmap. Every feature pull request must update its row.

| ID | Capability | Status | Target |
| --- | --- | --- | --- |
| PLAT-001 | Docker Compose deployment with gateway, web, workers, PostgreSQL, and explicit roots | Planned | Phase 1 |
| PLAT-002 | Health/readiness checks, structured logs, worker heartbeat, queue and scan visibility | Planned | Phase 1, hardened in Phase 6 |
| AUTH-001 | Credential login with Argon2id and revocable server-side sessions | Planned | Phase 1 |
| AUTH-002 | Multiple users and groups with administrative management | Planned | Phase 1 |
| AUTH-003 | Additive per-user/group root grants and operation-level permissions | Planned | Phase 1 |
| AUTH-004 | Optional administrator-enforced TOTP | Planned | Phase 6 |
| FILE-001 | Indexed, virtualized file browser with stable cursor pagination | Planned | Phase 2 |
| FILE-002 | Multiple mounted logical roots with safe path containment | Planned | Phase 1–2 |
| FILE-003 | Resumable uploads with staging, progress, and conflict handling | Planned | Phase 2 |
| FILE-004 | Create folder, rename, move, copy, and idempotent operation recovery | Planned | Phase 2 |
| FILE-005 | Authorized downloads and browser-compatible byte-range streaming | Planned | Phase 2 |
| FILE-006 | Root-local recycle bin, restore, and configurable retention | Planned | Phase 2 |
| FILE-007 | Filesystem event ingestion plus checkpointed full reconciliation | Planned | Phase 2 |
| FILE-008 | Permission-safe filename and path search | Planned | Phase 2 |
| UX-001 | Installable dark-first mobile PWA with Files, Photos, Search, and More navigation | Planned | Phase 3 |
| UX-002 | Persistent resumable transfer manager and live job progress | Planned | Phase 2–3 |
| MEDIA-001 | Responsive thumbnails, EXIF metadata, video posters, and media probing | Planned | Phase 3 |
| MEDIA-002 | Photo timeline, filters, favorites, and fast virtualized grids | Planned | Phase 3–4 |
| MEDIA-003 | Touch-oriented photo and video viewing | Planned | Phase 3 |
| MEDIA-004 | Original video range streaming with cached HLS fallback when required | Planned | Phase 3 |
| DOC-001 | Progressive PDF viewing, page thumbnails, and extracted-text search | Planned | Phase 3–4 |
| DOC-002 | Escaped, chunked text viewing with encoding detection | Planned | Phase 3 |
| DOC-003 | Server-paged CSV viewing with bounded filtering | Planned | Phase 3 |
| ORG-001 | Albums, tags, ratings, favorites, and duplicate candidates | Planned | Phase 4 |
| SEARCH-001 | Permission-safe metadata, full-text, and combined search | Planned | Phase 4 |
| AI-001 | Optional local CPU embeddings, OCR, labels, and captions | Planned | Phase 5 |
| AI-002 | Optional GPU acceleration using a deployment profile | Planned | Phase 5 |
| AI-003 | Semantic search and virtual smart albums with provenance and confidence | Planned | Phase 5 |
| AI-004 | Explicit per-capability frontier API connector with audited egress | Planned | Phase 5 |
| SEC-001 | Hardened internet-facing defaults, least-privilege containers, and safe content handling | Planned | Phase 1, audited in Phase 6 |
| SEC-002 | Audit trail for authentication, grants, file mutations, administration, and cloud AI use | Planned | Phase 1–2 |
| PERF-001 | Automated 1,000,000-entry and 50,000-entry-folder performance suite | Planned | Phase 2, enforced thereafter |
| OPS-001 | Documented backup, restore, upgrade, failure-injection, and recovery workflows | Planned | Phase 6 |
| EXT-001 | WebDAV and desktop synchronization | Deferred | Later |
| EXT-002 | Historical file versions and controlled sharing | Deferred | Later |
| EXT-003 | Certified 10,000,000+ entry operation and storage adapters | Deferred | Later |
| EXT-004 | Collaborative editing | Deferred | Later |

## Roadmap

The current focus is **Phase 0**. A phase is complete only when its acceptance gate passes; landing some listed features is not enough.

| Phase | Deliverable | Acceptance gate | Status |
| --- | --- | --- | --- |
| 0 — Design and checkpoint | Canonical README/specification, legacy release tag, and first bounded implementation plan | Design reviewed, legacy state recoverable by name, and clean documentation checkpoint | In progress |
| 1 — Secure foundation | Django/React/PostgreSQL/Compose skeleton, same-origin auth, users/groups, roots/grants, job and operation primitives, health, and CI | A user can sign in and reach only an authorized root shell through the deployed stack | Planned |
| 2 — Scalable drive core | Indexer, cursor browser, search, resumable upload, full mutations, trash, reconciliation, and audit | Required workflows pass against 1M total entries and a 50K-entry folder | Planned |
| 3 — Mobile media and documents | Dark PWA, thumbnails, photo timeline, photo/video viewers, range/HLS delivery, and PDF/text/CSV viewers | Mobile interaction budgets and Chromium/WebKit browser journeys pass | Planned |
| 4 — Search and human organization | Rich metadata, albums, tags, ratings, favorites, duplicates, and full-text search | Combined search remains correct and permission-safe across users and roots | Planned |
| 5 — Local intelligence | CPU AI, optional GPU, semantic search, provenance, smart albums, and opt-in frontier providers | Files remain fully usable with AI disabled or failed; no unapproved egress occurs | Planned |
| 6 — Release hardening | Backup/restore drills, hostile media tests, failure injection, security review, upgrade path, and operations guide | A documented recovery exercise and release checklist pass for v1 | Planned |
| Later | WebDAV/sync, versions, controlled sharing, 10M+ certification, storage adapters, and separately specified collaboration | Each capability receives its own approved specification and scale/security gate | Deferred |

## Performance contract

Initial measurements target an x86 home server or NAS with 4–8 CPU cores, 8–16 GB RAM, PostgreSQL on local storage, and no required GPU.

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
- Secrets are injected through Docker secrets or protected configuration and are redacted from logs.
- Containers run unprivileged with minimal mounts and capabilities; indexing, media, and AI roles never receive write access to originals.
- Cloud model use is disabled by default, explicitly selected per capability, and recorded in the audit trail.

## Development and documentation

The web rewrite does not have runnable development commands yet. The first implementation plan will cover only Phase 1 after this design is reviewed. Subsequent phases receive their own bounded plans so scale, security, and recovery gates stay visible.

- [Approved platform design](docs/superpowers/specs/2026-08-31-aegis-platform-rewrite-design.md)
- Future implementation plans: `docs/superpowers/plans/`
- License: [MIT](LICENSE)

When implementation starts, changes must include tests appropriate to their risk, preserve the stable feature IDs above, and update both the feature matrix and current roadmap status in the same pull request.
