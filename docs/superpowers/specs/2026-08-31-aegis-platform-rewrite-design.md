# Aegis platform rewrite design

**Status:** Architecture approved; written checkpoint awaiting final review

**Date:** 2026-08-31

**Scope:** Complete replacement of the legacy Aegis Arch Linux hardening CLI with a self-hosted file drive, media library, document viewer, and optional AI organization platform

**Canonical roadmap:** [README.md](../../../README.md)

## 1. Decision summary

Aegis will be a Docker-deployable, mobile-first web application for browsing and managing files that the operator explicitly mounts into the deployment. It will provide the focused drive capabilities normally sought from Nextcloud and the photo/video discovery experience normally sought from PhotoPrism, without collaborative editing in the first release.

The selected architecture is:

- a Django modular monolith for authentication, authorization, administration, APIs, operation journaling, and orchestration;
- a React and TypeScript progressive web app, optimized first for mobile browsers;
- PostgreSQL for identities, grants, indexed metadata, durable work, organization, search metadata, and audit records;
- separate process roles from the same versioned application code for file operations, indexing, media/document processing, optional local AI, and an isolated optional frontier connector;
- an unprivileged delivery gateway for static assets, API proxying, request limits, and authorized byte-range file delivery;
- mounted filesystems as the source of truth for original files;
- local CPU inference as the default AI mode, with optional GPU acceleration and explicitly enabled frontier APIs.

The implementation target is at least 1,000,000 indexed entries overall and 50,000 direct children in one folder on an x86 NAS or home server with 4–8 CPU cores and 8–16 GB RAM. The external API and catalog identities must admit later 10,000,000+ entry scaling without changing their meaning.

This document defines the overall platform. It does not authorize a single all-at-once build. The first implementation plan will cover only Phase 1, and each later phase will receive a bounded plan with its own verification gate.

## 2. Repository and legacy boundary

The repository at the start of this rewrite contains an unrelated C application: an Arch Linux hardening CLI. Commit `1cb4277` is the known legacy checkpoint.

During Phase 0:

1. preserve commit `1cb4277` with the annotated tag `legacy-hardening-cli-v0.1.0`;
2. keep this design and the central README in normal history;
3. review and approve the first implementation plan;
4. replace the active legacy source tree during Phase 1 rather than carrying it as a dormant subdirectory.

Git history and the named tag are the recovery mechanism for the legacy product. The new application does not reuse its C architecture, configuration, or release identity.

The Phase 0 checkpoint changes documentation and local design-tool hygiene only: `.superpowers/` is ignored so browser-companion session artifacts cannot enter commits. It changes no legacy or new-product runtime behavior.

## 3. Goals

### 3.1 Product goals

- Let authenticated users browse every file beneath one or more roots granted to them.
- Support multiple users and Django groups with different visible roots and distinct operation permissions.
- Provide complete first-release drive operations: folder creation, upload, rename, move, copy, recoverable delete, restore, download, and stream.
- Detect and reconcile changes made outside Aegis, including host-side and SMB/NFS-side changes.
- Provide thumbnail-driven photo and video browsing, filtering, timeline views, touch-friendly viewing, and browser-compatible playback.
- View PDF, text, and CSV content efficiently in mobile browsers.
- Provide human organization through albums, tags, ratings, favorites, and saved filters.
- Add optional local AI labeling, OCR, embeddings, semantic search, captions, and smart albums without making AI a dependency.
- Be safe to expose to the internet behind TLS when configured according to the deployment guide.
- Be observable and recoverable by a self-hosting operator without requiring a distributed-systems stack.

### 3.2 Engineering goals

- Keep directory browse latency proportional to page size, not folder size.
- Keep scans and derived work out of interactive web requests.
- Bound memory use while scanning, sorting, previewing, and serving files.
- Make file mutations idempotent and recoverable across process, database, and host restarts.
- Preserve original files in ordinary mounted filesystem paths.
- Make generated derivatives disposable and reproducible.
- Enforce authorization before both metadata access and byte delivery.
- Establish repeatable performance, hostile-input, browser, and failure-injection tests before v1.

## 4. Non-goals for v1

- Real-time collaborative document editing.
- Desktop synchronization clients or WebDAV.
- Historical file versioning.
- Public or anonymous link sharing.
- Arbitrary object-storage adapters.
- Automatic physical moves or renames based on an AI result.
- Guaranteed identity preservation for every out-of-band rename on every network filesystem.
- Offline synchronization of original files into the browser.
- Certified operation above 10,000,000 entries. The design accommodates that work, but certification is later.

## 5. Operating assumptions and scale envelope

### 5.1 Initial environment

- Linux x86 host or NAS.
- 4–8 CPU cores and 8–16 GB RAM.
- PostgreSQL data on local reliable storage.
- Original roots on local filesystems, bind mounts, or operator-managed SMB/NFS mounts.
- No required GPU.
- One Docker Compose deployment and one PostgreSQL primary.
- LAN/VPN use, direct internet use behind the bundled or an operator-provided TLS proxy, or both.

Network filesystem watchers are hints, not a correctness mechanism. Atomicity and permission behavior can also vary by filesystem. Aegis detects root capabilities, records them, and uses the conservative operation path when a stronger primitive is unavailable.

### 5.2 Required scale tests

The Phase 2 fixture contains:

- at least 1,000,000 catalog entries distributed across multiple roots and folders;
- at least one folder with 50,000 direct children;
- mixed ASCII, Unicode, long, case-adjacent, and byte-irregular names;
- files, directories, symlinks, unavailable entries, media, and documents;
- representative user/group grants and concurrent background work.

Later scale work may add PostgreSQL partitioning, read replicas, or sharded processing queues. No client API exposes physical table layout, database sequence values, or absolute paths, so those changes remain internal.

### 5.3 Reference benchmark profile

Release acceptance uses one versioned benchmark manifest rather than the full supported-hardware range. The initial reference is an x86-64 Intel N100-class 4-core host with 16 GB RAM. Core services receive four CPU cores total and these hard memory limits: PostgreSQL 3 GiB, web 1 GiB, operations 1 GiB, indexer 1 GiB, media 2 GiB, and gateway 256 MiB. AI is excluded from the base measurement and added as an explicitly reported load profile.

PostgreSQL and managed volumes use local SSD/NVMe storage with at least 20,000 measured random-read IOPS and 300 MB/s sequential throughput. The originals fixture uses a separately calibrated root with at least 150 MB/s sequential throughput and 150 metadata operations per second, representing modest NAS storage. The release report records CPU model/governor, RAM, kernel, Docker, filesystem/mount options, PostgreSQL settings, image digests, storage calibration, and deterministic dataset seed.

The server workload uses ten concurrent authenticated users for 15 minutes after a five-minute warmup, page size 100, and a fixed mix: 60% first/next directory pages, 15% alternate sorts, 10% entry details, 10% filename searches, and 5% transfer/job status. At least one quarter of list requests target the 50,000-child folder. A worker-load run adds one bounded index scan and two preview jobs. Cold results are recorded separately after service/database restart and are not blended into warm p95.

Mobile measurements use a 412-by-915 CSS-pixel viewport, touch input, 4x CPU slowdown, 40 Mbps downstream, 10 Mbps upstream, and 40 ms round-trip latency. The exact browser versions and trace journey are pinned in the benchmark manifest. WebKit functional journeys run separately because Chromium emulation is not a substitute for WebKit correctness.

## 6. System architecture

~~~mermaid
flowchart LR
    B[Mobile or desktop browser] --> G[Unprivileged Nginx gateway]
    T[Optional Caddy TLS profile] --> G
    G --> W[Django web and API]
    W --> P[(PostgreSQL)]
    P --> O[File operations worker]
    P --> I[Indexer worker]
    P --> M[Media and document worker]
    P --> A[Optional AI worker]
    O --> R[Mounted roots]
    I --> R
    G -. authorized internal delivery .-> R
    M --> R
    W --> D[Role-scoped managed volumes]
    O --> D
    M --> D
    A --> D
    G -. authorized derivative delivery .-> D
    A --> E[Approved frontier outbox]
    E --> C[Isolated frontier connector]
    C -. allowlisted TLS egress .-> F[Frontier model API]
~~~

Edges to the managed-volumes node represent only the role-specific mounts in Section 6.6; no container receives the aggregated storage shown by the diagram.

### 6.1 Gateway

The core gateway is an unprivileged Nginx container. It serves the built React application, proxies `/api/v1/` and upload endpoints to Django, applies request and connection limits, and exposes protected internal locations for originals and derivatives. Django authorizes a request and returns a short-lived internal redirect; the protected location cannot be requested directly. Original roots are mounted read-only into Nginx, each protected location is bound to one configured root, and symlink traversal is disabled. Nginx performs byte-range and conditional delivery so Python never buffers a large download or video.

The core profile can bind HTTP for a trusted LAN or sit behind the operator's existing TLS proxy. An optional Caddy profile provides automatic HTTPS in front of Nginx. Internet-facing documentation requires TLS, correct proxy trust settings, and secure cookies. HSTS is enabled only on an HTTPS deployment.

### 6.2 Django web/API

Django owns:

- credential authentication, sessions, CSRF protection, and administrative workflows;
- users, groups, roots, and grants;
- all permission decisions;
- versioned JSON APIs and compact list representations;
- creation and state transitions of durable operations and jobs;
- audit emission;
- signed internal delivery decisions;
- upload session coordination;
- server-sent event streams for progress.

The application is a modular monolith with explicit modules for identity, roots and grants, catalog, file operations, transfers, media, documents, search, organization, AI providers, audit, and operations. Module boundaries are Python and database boundaries inside one deployable application, not network services.

The initial API layer uses Django REST Framework. The same-origin PWA uses server-side sessions rather than storing bearer tokens in browser storage.

### 6.3 React PWA

The frontend uses React, TypeScript, Vite, React Router, TanStack Query, and a virtual collection primitive. Its responsibilities are interaction state, bounded client caching, optimistic presentation only when recoverable, mobile navigation, viewer controls, and resumable transfer coordination. It does not recreate authorization rules.

The service worker caches only the unauthenticated application shell and fingerprinted public build assets. It never caches an authenticated response, original, derivative, API payload, or transfer record. Originals, document bytes, APIs, and authenticated HTML use `Cache-Control: private, no-store`.

Thumbnails and posters may use the browser HTTP cache only with `Cache-Control: private, no-cache`, a strong ETag, and `Vary: Cookie`, which forces authorization revalidation before reuse. Their content-addressed URL also includes a signed user/session cache namespace and current root-authorization epoch. Login, logout, session expiry, account switch, and grant change rotate that namespace. A revalidation always passes through Django before Nginx can return a derivative.

### 6.4 PostgreSQL

PostgreSQL stores:

- users, groups, sessions, roots, and grants;
- the current indexed catalog and tombstones;
- file operation journals and idempotency records;
- resumable upload sessions;
- durable job queues, leases, attempts, and failure details;
- media/document metadata and derivative manifests;
- albums, tags, ratings, favorites, saved filters, and AI suggestions;
- audit records;
- full-text indexes and, when enabled, pgvector embeddings.

PostgreSQL does not store original file bytes. It is authoritative for security and user-created organization even though the file catalog can be rebuilt.

The initial work queue uses rows claimed with `SELECT ... FOR UPDATE SKIP LOCKED`, time-bounded leases, heartbeats, priority, bounded retries, and optional `LISTEN/NOTIFY` wakeups. Queue polling uses jitter. Large scans enqueue directory-sized batches rather than one row per discovered file. Redis is not an initial dependency.

### 6.5 Worker roles

All workers use the same application release and migration version.

| Role | Filesystem access | Work |
| --- | --- | --- |
| File operations | Read/write only on writable roots plus staging | final upload publication, cross-filesystem copy/move, recursive operations, trash purge, and recovery |
| Indexer | Read-only roots | initial scan, watcher events, checkpointed reconciliation, and catalog repair |
| Media/document | Read-only roots; read/write derivative volume | thumbnails, EXIF, probes, PDF pages/text, text sniffing, CSV indexes, and video compatibility output |
| Local AI supervisor | Read-only selected inputs; read/write model/derivative/outbox volumes | sandboxed embeddings, OCR, labels, captions, face grouping when enabled, and preparation of explicitly approved outbox payloads; no egress |
| Frontier connector | No root or general managed-volume access; one outbox payload | one allowlisted provider call and its audited result |

Same-filesystem mutations use the high-priority operations queue and may be awaited briefly by the API for an immediate response. The web process never needs a writable originals mount. Potentially long or recursive work returns an accepted operation immediately and continues in the file-operations worker.

### 6.6 Storage layout

Operators declare mount slots in deployment configuration before containers start. Each slot has a stable slot ID, host source, fixed container path below `/srv/aegis/roots/`, read-only/read-write mode, and expected filesystem identity. Compose mounts the same slot path read-only into gateway/indexer/media/AI and, only when writable, read-write into operations. The browser and Django APIs never receive the host or container path.

A platform administrator can activate a logical root only by selecting a predeclared slot; Django cannot register an arbitrary path that is absent from the deployment manifest. Adding or changing a slot requires host-side preflight and recreation of the affected containers before database activation. Runtime readiness compares the manifest with the identity and access mode reported by each role, and a mismatch keeps that root unavailable.

The host-side preflight canonicalizes sources and compares real paths, filesystem/mount identity, root inode, and ancestor relationships. It rejects identical, nested, or otherwise detectable physical aliases so one subtree cannot acquire two authorization identities. For network mounts whose aliases cannot be proven distinct, the operator must supply a stable remote/share identity; ambiguous duplicates fail closed rather than relying on display names. The expected identity is rechecked before scan finalization and every mutation.

Role-scoped Aegis volumes prevent an untrusted processor from reading unrelated state:

| Volume | Writers | Readers | Contents |
| --- | --- | --- | --- |
| Staging | web | operations | resumable upload chunks; never served by the gateway |
| Derivatives | media and local AI | gateway and authorized processors | thumbnails, PDF pages, transcodes, and extraction artifacts |
| Models | explicit model-management job | local AI | pinned local model artifacts and manifests |
| Media scratch/quarantine | media | media and authorized administration | per-job temporary output and quarantined failures |
| Frontier outbox | trusted AI supervisor | isolated frontier connector | only the payload approved for one audited provider job |

The volumes are separate Compose mounts, not merely directories protected by application convention. A tool sandbox sees only one job's input descriptor, scratch directory, and output limit. It receives no PostgreSQL credential and no broad staging, derivative, model, or root mount.

At upload finalization, the operations worker streams assembled content from staging into a hidden destination-local temporary file, flushes it, and atomically renames it into view. This gives atomic publication even when the staging volume and destination root are different filesystems.

Recoverable deletion uses a reserved hidden trash directory inside the same writable root. A same-filesystem rename makes normal deletes atomic and restores inexpensive. No managed volume holds the authoritative trash copy.

### 6.7 Filesystem identity and metadata contract

Deployment config sets the unprivileged UID, primary GID, and supplementary groups used for root access. A root may additionally set its creation group and umask; the secure default umask is `0027`. Uploads are owned by the configured operations UID/GID. Aegis does not require root, grant arbitrary ownership changes, or retain setuid/setgid bits. Operators use host ownership, ACLs, and supplementary groups to make each mount slot readable or writable as intended.

Activation preflight verifies read access and, for a writable slot, bounded create, file-sync, atomic-rename, directory-sync, lock, trash, and cleanup operations inside the reserved Aegis area. It records case behavior, timestamp precision, stable-identity support, sparse-file support, ACL/xattr support, and free-space semantics. A root that fails mandatory checks remains read-only or unavailable rather than accepting unsafe mutations.

Same-filesystem rename/trash naturally preserves the filesystem object's metadata. For a copy, v1 guarantees regular-file bytes, directory structure, safe names, and a verified destination. It preserves modification time and ordinary permission bits when the destination permits. ACLs and extended attributes use a configured allowlist and are reported when unsupported; a cross-filesystem move never removes the source if required metadata could not be preserved. Sparse extents are retained when both filesystems support them, otherwise free-space checks assume expanded size. Hard-link topology is not preserved by copy: each destination is an independent file and the UI reports that limitation.

Symlink entries remain inert and are never dereferenced. Rename and trash operate on the link object. Copy may recreate only the link text, and only when the destination root explicitly permits symlink objects; otherwise it returns a policy conflict. Devices, sockets, and FIFOs are metadata-visible but cannot be previewed, uploaded, copied, or opened by Aegis.

## 7. Domain and data model

### 7.1 Principals, roots, and grants

- **User** and **Group** are Django authentication principals.
- **Root** is an administratively activated logical filesystem boundary with an opaque UUID, display name, deployment mount-slot reference, read/write mode, case behavior, and scan policy.
- **Grant** assigns a principal a bit set for one complete logical root.
- **EffectiveGrant** is the additive union of the user's direct and group grants.

The v1 permission bits are independently assignable:

1. browse and search metadata;
2. preview and stream;
3. download and export content;
4. upload and create folders;
5. rename and move within a root;
6. request server-side copies;
7. delete and restore;
8. administer that root.

There are no deny rules in v1. An administrator expresses a subtree boundary by registering it as a separate logical root. Overlapping canonical root paths are rejected to avoid ambiguous authorization. A platform administrator can manage the installation but receives no implicit file access; data access still requires a root grant.

Operation authorization is evaluated on both sides of an operation:

| Operation | Source requirement | Destination requirement |
| --- | --- | --- |
| Rename or move within one root | rename/move | the same root grant and a writable destination |
| Copy within one root | copy plus download/export | upload/create on the same root |
| Copy across roots | copy plus download/export on the source root | upload/create on the destination root |
| Move across roots | copy, download/export, and delete/restore on the source root | upload/create on the destination root |
| Restore to original location | delete/restore | writable original root |
| Restore to a different folder | delete/restore | upload/create on the destination root |

A cross-root move is always the authorized copy workflow followed by a recoverable source delete; it is never treated as a rename. Directory operations require the same permissions for every affected root. Preview/stream is content disclosure and cannot promise that a browser user is technically unable to save displayed bytes; the download/export permission controls explicit original delivery and operations that transfer complete content.

### 7.2 Catalog entries

A catalog entry represents a location beneath one root:

- opaque UUID;
- root and parent UUID;
- exact raw filename bytes plus a safe Unicode display form;
- entry kind;
- relative depth and optional materialized path acceleration fields;
- size and relevant timestamps;
- device/inode identity when meaningful;
- source fingerprint and optional strong content hash;
- media/document type summary;
- catalog version and last-seen scan generation;
- live, missing, trashed, quarantined, or inaccessible state.

New browser-created names must be valid Unicode and UTF-8 encodable. They cannot contain NUL, a path separator, or the special components `.` and `..`. Existing Unix names that are not valid UTF-8 remain addressable through opaque entry IDs and show an escaped replacement label; exact raw bytes are retained for filesystem access.

Location identity remains stable across Aegis-managed renames and moves. For external moves, the indexer preserves identity when filesystem identity or a sufficiently strong fingerprint makes the match unambiguous. Otherwise it records an old tombstone and a new entry rather than guessing.

### 7.3 Assets and derived metadata

Media/document extraction attaches to an asset revision identified by the source fingerprint and processor version, not merely the current path. A source fingerprint includes root identity, stable filesystem identity when available, size, modification time at nanosecond precision, and a strong hash when computed. A detected source change invalidates older derived records.

Derived object keys include source fingerprint, transformation parameters, processor version, and model version. They are immutable, content-addressed, evictable, and reproducible. Human metadata is kept distinct from machine suggestions so reprocessing cannot overwrite user choices.

### 7.4 Catalog indexes

The principal directory index begins with root, parent, live-state, selected sort value, normalized name key, and opaque ID. Separate bounded indexes support name, modification time, size, and media date sorting. The exact migration-level index definitions are benchmarked against the Phase 2 fixture.

Filename search uses permission-filtered root predicates and PostgreSQL trigram/prefix indexes. Extracted document search uses PostgreSQL full-text search. Photo timelines use capture timestamp plus entry ID keysets. Optional vector search filters candidate roots before results can be returned.

No normal browse request computes an exact total. Cached or asynchronously computed counts are explicitly labeled approximate or as-of a catalog generation.

## 8. API and interaction contracts

### 8.1 General API

- All product endpoints are versioned below `/api/v1/`.
- JSON list responses use compact summary records; details and rich metadata load separately.
- Object IDs are opaque UUIDs and are meaningful only with authorization.
- Default page size is 100 and the server-enforced maximum is 250.
- Error responses use a stable problem-details shape with a machine code, safe message, request ID, and field details where applicable.
- The API never accepts an absolute filesystem path.

### 8.2 Keyset pagination

A browse cursor is signed, opaque, expiring, and bound to the authenticated user, root, parent, filter hash, sort, direction, and last returned sort tuple. The query uses a compound comparison against that tuple and a deterministic UUID tie-breaker. It does not use deep `OFFSET`.

Cross-request browsing does not claim a database snapshot while external files are changing. Clients deduplicate by entry ID. If a cursor's root, grant, filter, or catalog contract is no longer valid, the server rejects it with a restartable cursor error rather than returning an unsafe or mismatched page.

### 8.3 Concurrency and preconditions

Entry details include a version token. Destructive or overwriting commands include the expected version. A stale version returns HTTP 412. A destination-name conflict returns HTTP 409. Replace, keep-both, and cancel are explicit follow-up choices; the server never silently chooses one.

Every mutation has a client-generated idempotency key. A repeated key with the same principal and request hash returns the existing operation. Reusing a key for a different request returns a conflict.

### 8.4 Progress

Uploads, recursive operations, indexing, media work, and AI work expose durable progress records. The client receives updates through server-sent events with event IDs and can reconnect with `Last-Event-ID`. Polling remains a fallback. WebSockets are not required for v1.

## 9. Filesystem indexing and reconciliation

### 9.1 Correctness model

The mounted filesystem is authoritative for original existence and bytes. PostgreSQL is an eventually consistent catalog. Watchers reduce delay; checkpointed scans establish correctness.

A normal browser request queries PostgreSQL only. It never calls directory enumeration as a fallback. Until a change is indexed, the UI may show the prior catalog state; direct mutation results can update affected catalog rows immediately.

### 9.2 Scan algorithm

For each root, the indexer:

1. verifies the configured mount-slot identity, captures the root reconciliation epoch, obtains a scan generation, and enqueues bounded directory work;
2. enumerates one directory at a time with `os.scandir`;
3. converts entries to bounded batches and bulk-upserts their current metadata;
4. schedules child directories without retaining the full tree in memory;
5. persists checkpoints and counters;
6. finalizes unseen entries with the fenced rules below only after the relevant directory completes successfully;
7. emits downstream work only when a source fingerprint changed.

An interrupted scan resumes from durable checkpoints. It cannot erase catalog entries merely because a scan stopped midway. Per-root scan concurrency and I/O rate are configurable so browsing retains priority.

Each focused reconciliation commit increments a root-scoped monotonic reconciliation epoch and records that epoch on affected rows. Scan rows carry their scan ID and captured start epoch. Directory scan finalization acquires the same per-directory advisory lock used by focused reconciliation, rechecks the mount-slot identity, and may mark missing only rows that were not seen by this completed scan and whose last observed epoch is not newer than the scan's start epoch. Compare-and-swap predicates prevent a later watcher/focused update from being overwritten by stale scan finalization.

Watcher events received after scan start remain in the durable event sequence and are applied after or alongside finalization. Overflow marks the affected root/directory dirty and schedules a new scan. An identity mismatch, enumeration error, lost mount, or incomplete directory leaves prior live rows intact and records degraded health; an empty or replacement mount can never be interpreted as a successful mass deletion scan.

### 9.3 Watcher events

Where supported, filesystem events are normalized, assigned a durable per-root ingestion sequence, and coalesced by root and path. Events schedule focused reconciliation; they do not directly establish truth. Focused catalog commits serialize with scan finalization for the same directory. Queue overflow, watcher loss, container downtime, and network mount behavior are handled by the next scan. A periodic full or rolling scan is mandatory.

### 9.4 External changes

- External create or modification appears after focused or periodic reconciliation.
- External rename preserves ID only when unambiguously correlated.
- External deletion becomes a catalog tombstone and is not recoverable through Aegis.
- Temporary permission or mount failures mark entries/root health inaccessible; they do not immediately convert the entire subtree to deleted.
- A missing root pauses destructive reconciliation and raises an operator-visible health error.

## 10. File command lifecycle

### 10.1 Journaled state machine

Before touching a writable root, Aegis authenticates, authorizes, validates preconditions, and commits an operation record. Acceptance is not a permanent authorization grant: execution-time checks described below still apply. The operation moves through:

`accepted -> running -> filesystem_applied -> catalog_applied -> completed`

It may instead enter `blocked`, `failed`, or `reconciling`. State changes, request identity, actor, affected IDs, safe error details, and audit references are durable.

The database and filesystem cannot share one transaction. The required order is:

1. commit authorization context and operation intent;
2. perform the filesystem primitive;
3. commit catalog and audit effects;
4. mark completion.

If step 2 succeeds and step 3 fails, recovery inspects the filesystem and finishes or reconciles the catalog. No rollback claim is made without verifying filesystem state.

### 10.2 Coordination

Operations acquire deterministic root/path-scoped coordination records or advisory locks in canonical order. They never take a global filesystem lock. Interactive operations have short lock windows; long jobs use renewable leases and checkpointed manifests.

Each accepted operation records the request hash, expected entry versions, source/destination root identities, and the authorization epoch of every relevant principal/root grant. Disabling a user, changing a grant, changing a root mode, or replacing/deactivating a mount slot increments the applicable epoch and fences queued work.

When a worker claims an operation, and again immediately before every publish, rename, trash, unlink, or other irreversible primitive, it must verify:

1. the actor's account remains active;
2. current effective source and destination permissions satisfy the complete operation matrix;
3. every root remains active, in the required read/write mode, and mounted with the recorded deployment identity;
4. expected catalog versions still match;
5. descriptor-relative `fstat` identity for each source/destination parent and entry matches the object that was authorized;
6. the worker still owns the current operation attempt and path-scoped fence.

Long manifests repeat these checks at every checkpoint. A revocation stops before the next item; it never authorizes the remainder from a stale snapshot. If destination publication already happened when a later source-side check fails, Aegis preserves the destination, preserves the source, and reports a completed copy requiring attention.

Job claims use a monotonically increasing attempt/fence token and compare-and-swap state transitions. Each worker uses an operation-owned, attempt-specific temporary name. A worker with an expired token may clean its own unpublished temporary data but cannot publish, remove a source, or update completion state. The short final primitive is guarded by a live PostgreSQL advisory lock acquired after the execution-time checks. Losing the lease, lock connection, root identity, or authorization forces revalidation rather than continuation.

### 10.3 Durability and publication

Every operation-owned temporary or trash name includes the operation ID and attempt token, is created with exclusive no-follow semantics, and is recorded before filesystem use. Publication follows this minimum sequence:

1. open source, source parent, and destination parent by descriptors and record their filesystem identity;
2. stream to the destination-local temporary while calculating a strong digest;
3. confirm the source descriptor's identity, size, and timestamps did not change during the read;
4. flush and `fsync` the temporary file, then verify its bytes/digest;
5. reacquire the execution-time authorization and fencing checks;
6. atomically rename the temporary into the destination;
7. `fsync` the destination directory before recording the destination durable.

A same-filesystem rename synchronizes every changed parent directory after the atomic rename. File creation also synchronizes the created file before its parent. Permanent removal synchronizes the affected parent after unlink. A filesystem that does not provide the required atomic rename, file synchronization, directory synchronization, stable identity, or locking primitive is recorded as having reduced capabilities; Aegis uses the conservative fallback and never describes an unverified source removal as a durable move.

For a cross-filesystem move, the worker re-reads and hashes the source after destination publication, revalidates that the authorized source path still names the same object, and compares that digest with the destination. It then performs the source side as a normal recoverable delete: atomically rename the source into its root-local trash and synchronize both the original and trash parent directories. It does not immediately unlink the source bytes. If the source changed, an external writer is still active, identity is ambiguous, or the source filesystem cannot establish this sequence, the operation degrades to a completed copy with manual source cleanup. This policy favors duplicate data over silent loss.

### 10.4 Upload

Uploads follow the tus resumable-upload protocol:

1. Django authorizes the target root/folder and creates a quota-bounded upload session.
2. Chunks stream through bounded request buffers to the staging volume; the complete chunk or upload is never held in Django process memory.
3. The server validates declared length and optional client checksum.
4. Finalization revalidates grants, destination version, free space, and conflict policy.
5. A worker streams the assembled content into an operation-owned destination-local temporary file while hashing it, synchronizes and verifies the file, atomically renames it into the final name, and synchronizes the destination directory.
6. Catalog update completes the operation and schedules derived work.

Expired sessions are garbage-collected. Request, per-user, and installation limits prevent unbounded staging use. A disk-space failure leaves no partially visible destination and returns HTTP 507.

### 10.5 Rename and move

A rename or same-filesystem move uses the filesystem's atomic rename primitive after source/destination descriptor validation and follows the parent-directory synchronization contract. A cross-filesystem move is a background operation:

1. stream to a destination-local temporary name while computing a strong digest;
2. synchronize and verify destination bytes;
3. atomically publish and synchronize the destination;
4. update the destination catalog state;
5. re-read, hash, and identity-check the source;
6. move the unchanged source into root-local trash and synchronize its parent directories.

If the source-side checks or trash move fail, the result is reported as a completed copy with cleanup required rather than pretending the move was atomic.

### 10.6 Copy and recursive operations

Copies use the same temporary, verification, and publish sequence without deleting the source. Directory copy/move/delete operations first build a bounded checkpointed manifest and expose progress. Restarts continue from recorded items and idempotently inspect already completed destinations.

### 10.7 Recycle bin

Delete through Aegis atomically renames the entry into a reserved hidden directory in the same writable root. PostgreSQL records the original parent/name, trash location, actor, deletion time, retention deadline, and source fingerprint. The indexer excludes internal staging and trash paths from ordinary views.

Restore revalidates permission and conflict policy. A retention worker permanently purges expired items in bounded batches. Retention is configurable per installation and may be overridden per root. Read-only roots cannot offer Aegis-managed deletion. Out-of-band deletion bypasses the recycle bin.

## 11. Path and content security

### 11.1 Root containment

Filesystem calls start from an open descriptor for the authorized root and resolve relative components beneath it. Components are validated independently; string prefix checks are insufficient. Operations reject traversal, NUL, absolute paths, and unexpected mount transitions.

Symlink following is disabled by default for browsing, indexing, delivery, and mutations. Symlinks can be shown as inert entries. A future explicit policy may permit safe in-root following, but v1 does not.

### 11.2 Authentication

- Passwords use Argon2id through Django's password-hashing framework.
- Sessions are server-side, revocable, rotated on login, and expire by idle and absolute lifetime.
- Cookies are HttpOnly and SameSite; Secure is mandatory in HTTPS mode.
- State-changing requests require CSRF validation.
- Login errors do not reveal account existence.
- Per-IP and per-account throttles apply to authentication and recovery endpoints.
- Optional administrator-enforced TOTP lands before v1 release hardening.

Account recovery is administrator-driven in the initial self-hosted release; email infrastructure is not assumed.

### 11.3 Authorization

Every catalog query, search, preview, download, and mutation starts with effective root permission. List endpoints include root permission in the query boundary instead of filtering unauthorized results after retrieval. Delivery authorization binds actor, entry, current version, action, and short expiration.

Django superuser status does not bypass product data checks. Grant changes revoke relevant cached decisions and active delivery authorizations.

### 11.4 Browser and content safety

- Same-origin deployment is the default; permissive CORS is not enabled.
- CSP, frame restrictions, MIME sniffing protection, referrer policy, and permissions policy are set at the gateway.
- HTML, SVG, unknown, and potentially active formats default to attachment download.
- Text and CSV output is escaped; formulas are not executed.
- PDF and media viewers use sandboxed, pinned libraries and never trust file-supplied MIME alone.
- Content-Disposition filenames use safe encoding.
- Private originals are excluded from service-worker storage.
- Authenticated HTML, APIs, originals, and document bytes use `private, no-store`; thumbnails use the revalidating policy defined above.
- Cache Storage, IndexedDB transfer metadata, query caches, object URLs, and in-memory selection/viewer state are purged on logout, session expiry, and account change.
- A page restored from the browser back/forward cache must revalidate the session before revealing prior authenticated state.

### 11.5 Container and processor safety

Containers run as configured unprivileged UIDs with read-only application filesystems, dropped Linux capabilities, no privileged mode, and no Docker socket. Only the file-operations role receives writable original mounts. Media and AI tools receive CPU, memory, time, output-size, and concurrency limits.

External tools are invoked with argument arrays, never shell interpolation. Processor input is treated as hostile. Malformed files, decompression bombs, huge dimensions, recursive documents, and excessive CSV fields are rejected or quarantined by policy.

Application roles attach only to Docker internal networks with no internet route. The gateway is the sole core ingress, and PostgreSQL is never published. A dedicated frontier connector is the only egress-capable application role: it has no original-root, staging, derivative, model, or quarantine mount; it can read only a single approved outbox payload and write its response through a narrow database interface. Outbound TLS is restricted to the configured provider allowlist. The local AI worker itself has no internet route, so enabling cloud access cannot silently expand the trust boundary of a process that can read originals.

Database access also follows role boundaries. A migration principal owns schema changes and is absent during normal operation. Web, operations, indexer, media, local AI, and frontier connector use distinct credentials with table/operation privileges limited to their duties. Processor roles cannot create or alter immutable web-created operation intents or grants; they can claim eligible jobs and update only fenced status/result fields. Gateway has no database credential.

### 11.6 Secrets and audit

Database credentials, Django secrets, encryption keys, and frontier API keys come from Docker secrets or permission-protected configuration. API keys are encrypted at rest with an installation key. Logs and error payloads redact paths, credentials, tokens, cookies, and file content.

Audit events cover login success/failure, logout, user/group/grant changes, root configuration, file mutations, trash/restore/purge, administrative actions, frontier egress, and security-relevant configuration changes. Records include actor, time, request/operation ID, root and opaque object IDs, outcome, and safe metadata.

## 12. Media and document pipeline

Work is progressive and priority-aware:

| Tier | Trigger and priority | Output |
| --- | --- | --- |
| 0 — Catalog | Immediate | identity, kind, size, timestamps, and coarse type |
| 1 — Preview | High | responsive image thumbnails, video poster/probe, PDF first page, and text/CSV sniff |
| 2 — Rich extraction | Normal | EXIF/GPS, PDF/OCR text, CSV row index/schema, and on-demand video compatibility cache |
| 3 — AI | Low | embeddings, labels, captions, OCR enhancement, optional face grouping, and smart-album candidates |

Interactive browse, upload finalization, and requested previews outrank background backfill. Per-role concurrency and I/O budgets prevent the initial scan from saturating the host.

### 12.1 Photos

The photo timeline is a permission-filtered keyset query by capture date, falling back predictably to file modification time. Responsive derivative widths avoid sending full originals to a phone grid. Orientation is normalized from EXIF. The full-screen viewer supports swipe navigation, touch zoom, metadata, favorite/rating actions, and bounded neighbor prefetch.

GPS display and location filters are disabled per root or installation when desired. Originals remain unchanged.

### 12.2 Video

When the browser supports the original codecs and container, Nginx serves the original with byte ranges. If not, Aegis generates and caches an HLS-compatible rendition on demand. The UI shows poster/probe results while a compatibility job runs and never transcodes synchronously inside an API request.

Renditions are disposable. Limits cover input duration/size, resolution, concurrent encodes, and total derivative storage.

### 12.3 PDF

PDF.js provides paged browser rendering. Aegis generates a first-page preview and optional page thumbnails, then extracts bounded text for permission-filtered search. The client loads page ranges progressively rather than downloading a large document before showing the first page when the source permits ranges.

### 12.4 Text

The server detects encoding with a bounded sample and exposes escaped chunks by byte range with line anchors. It caps line length, rendered bytes, and search work. Binary-looking files do not enter the text viewer automatically.

### 12.5 CSV

CSV processing detects delimiter and encoding from bounded samples, builds a row-offset/schema artifact, and exposes server-paged rows. Filters and sorts are bounded, cancelable jobs; the browser virtualizes visible rows. Formula-like cells are plain text. Huge or malformed rows return a safe partial/error state instead of exhausting memory.

## 13. Organization, search, and AI

### 13.1 Human organization

Albums, tags, ratings, favorites, saved filters, and duplicate-candidate decisions live in PostgreSQL. They are virtual references and never move originals. Human values take precedence over machine suggestions.

Duplicate detection presents candidates based on strong hashes and perceptual similarity. Aegis does not delete duplicates automatically.

### 13.2 Provider model

AI capabilities use a provider interface with these modes:

- **off:** no model work is scheduled;
- **local CPU:** default optional mode using small release-supported models;
- **local GPU:** the same capability contract through a GPU Compose profile;
- **frontier:** an explicitly configured connector selected per capability.

The AI profile is optional and can remain off. When AI is enabled, local CPU is the default provider. Provider selection is capability-specific. Enabling a frontier caption provider does not enable cloud OCR or embeddings. There is no automatic cloud fallback when a local model fails.

Each machine result records provider, model/version, input fingerprint, parameters, creation time, confidence where meaningful, and whether content left the installation. Model artifacts have pinned manifests and checksums.

### 13.3 AI capabilities

Phase 5 includes local embeddings, semantic search, OCR, broad labels, captions, and virtual smart albums. Optional face grouping is disabled by default and requires an explicit administrator setting because it processes biometric-like data.

AI suggestions are stored separately from accepted human metadata. Re-running or replacing a model creates new provenance rather than silently rewriting prior results. AI never renames or moves a physical file without a later, explicit user file command.

### 13.4 Frontier privacy boundary

Before content can leave the deployment, an administrator must configure a provider/key and enable a named capability. The initiating UI identifies the provider and data class. Every outbound job applies root policy, minimizes payload, records audited egress, and surfaces provider failures without falling back elsewhere.

The default deployment has no frontier provider. The local AI supervisor has no egress in every profile; only the separately enabled connector can reach an allowlisted provider.

## 14. Mobile-first experience

### 14.1 Navigation and layout

The primary bottom navigation has **Files**, **Photos**, **Search**, and **More**.

- Files includes root selection, breadcrumb navigation, search/sort controls, selection mode, and a virtual list or grid.
- Photos opens a virtualized timeline with date/type/metadata filters.
- Search combines filename, metadata, document text, and later semantic modes while preserving root scope.
- More contains transfers, jobs, trash, settings, account actions, and authorized administration.

Desktop layouts may add a side rail and detail pane, but use the same information architecture.

### 14.2 Visual system

The default theme is dark:

- deep slate canvas and elevated surfaces rather than pure black;
- high-contrast neutral text;
- restrained indigo primary actions and cyan informational accents;
- semantic warning/error/success tokens that remain distinguishable without color alone;
- clearly visible keyboard focus;
- at least 44-by-44 CSS-pixel touch targets;
- reduced-motion support.

System and light themes are optional settings. Automated contrast checks and keyboard/screen-reader journeys are release gates, not polish tasks.

### 14.3 Large collections

The client renders only visible rows or thumbnails. It requests bounded pages and prefetches at most the next useful page. Details, EXIF, permissions, and AI fields are lazy-loaded. Selection state uses opaque IDs and does not require materializing an entire folder in browser memory.

The API's returned order is authoritative. The browser does not download a 50,000-entry folder to sort it locally.

### 14.4 Transfers and unstable networks

The transfer manager persists resumable upload identifiers, progress, target, and safe failure state. It can continue after navigation or network loss and clearly distinguishes pause, retry, conflict, and failure. Background browser execution is opportunistic; correctness relies on server-side upload state rather than a permanently running tab.

### 14.5 PWA caching

The unauthenticated app shell can open offline and explain that server content is unavailable. v1 does not sync originals for offline use. Authenticated application data is never placed in the service-worker cache. Content-addressed thumbnail URLs permit conditional revalidation, but their session namespace and authorization epoch prevent reuse as permanent unauthenticated links.

## 15. Performance contract

On the baseline host and required fixture:

- p95 server time for an indexed directory page is at most 300 ms;
- p95 indexed browsing while workers run at their configured limits is at most 500 ms;
- the defined mobile browse journey has largest contentful paint at most 2.5 seconds;
- the defined mobile interaction journey has interaction to next paint at most 200 ms;
- process memory remains within documented container budgets during scan, list, preview, and viewer tests;
- no normal list endpoint performs a synchronous filesystem enumeration or exact total count.
- the initial catalog-only 1,000,000-entry scan completes within four hours on the calibrated originals root;
- indexer resident memory remains at or below 750 MiB and catalog tables plus indexes remain at or below 8 GiB for that fixture;
- a mobile browser viewing the 50,000-entry folder remains at or below 250 MiB heap after a ten-minute scroll/select/navigation journey.

Measurements report cold and warm database/cache cases separately and identify host storage. A passing result cannot depend on disabling authorization, reconciliation, or representative metadata.

Performance mechanisms include:

- compound covering indexes proven with query plans;
- stable keyset pagination;
- compact list DTOs;
- virtualized frontend collections;
- batched scans and PostgreSQL upserts;
- bounded worker concurrency and priority;
- gateway byte delivery;
- content-addressed derivative caching;
- asynchronous exact/aggregate calculations.

Regression thresholds run in CI where practical and in a repeatable release benchmark environment for the full fixture.

## 16. Deployment and operations

### 16.1 Compose services

The production Compose definition includes:

- `gateway`: unprivileged Nginx, the only core published HTTP service;
- `web`: Django ASGI/WSGI application with bounded worker count;
- `operations`: file mutation worker with only required writable mounts;
- `indexer`: read-only catalog reconciliation worker;
- `media`: read-only media/document processor;
- `postgres`: pinned PostgreSQL image and durable volume;
- separate named staging, derivative, model, quarantine, and frontier-outbox volumes;
- one explicit bind/volume mount per configured root.

Root mounts are read-only in the gateway, indexer, media, and AI roles. Only the operations role receives a read/write mount, and only for roots configured writable. The web role receives no original-root mount.

Optional profiles add `tls` for Caddy, `ai-cpu` or `ai-gpu`, and the separately authorized `ai-frontier` connector. The GPU profile documents the required host runtime and fails clearly when unavailable. CPU/GPU local AI remains on an internal no-egress network.

Images are pinned by version or digest for releases. Startup applies explicit, reversible-aware Django migrations. Workers remain unready and do not claim jobs until the database schema version matches the application.

### 16.2 Health and observability

Liveness checks answer only whether a process is alive. Readiness verifies schema compatibility and required dependencies without performing destructive recovery.

Operator-visible status includes:

- database and root availability;
- root mount identity/capability changes;
- worker version and heartbeat;
- queue depth, age, retries, and quarantined jobs;
- scan generation, checkpoint, rate, and estimated progress;
- derivative and staging disk pressure;
- request latency/error metrics;
- recent failed operations with safe diagnostic IDs.

Logs are structured and correlate request, operation, job, user, and root IDs. Aegis has no phone-home telemetry.

### 16.3 Backup and restore

Mandatory recoverable state consists of:

1. PostgreSQL;
2. secrets, encryption keys, and the deployment/root-slot manifest;
3. every original root, including its hidden Aegis trash.

Staging is conditionally mandatory. A backup that preserves resumable upload sessions captures the staging volume at the same boundary as PostgreSQL. A backup that omits staging must first drain or explicitly invalidate incomplete upload sessions and record that decision in the backup manifest. Derivative, model-cache, scratch, and quarantine volumes are optional because their successful outputs can be regenerated or their failed jobs can be restarted. A nonempty frontier outbox is either drained/canceled or captured with its matching database state.

Originals alone do not restore accounts, grants, albums, tags, ratings, audit history, or trash manifests. PostgreSQL alone does not restore file bytes. The quiesced backup mode refuses new uploads/mutations, lets active operations reach safe checkpoints, fences workers, and records scan/operation epochs. Application quiescence does not stop host, SMB, or NFS writers: an application-consistent backup also requires those writers to stop or coordinated filesystem/database snapshots at one boundary.

If external writers cannot be quiesced, the backup is labeled crash-consistent rather than application-consistent. Restore then verifies configuration and root identities, invalidates incomplete operations/uploads whose staged bytes are absent, and completes a full reconciliation before declaring readiness. Derivatives may be deleted and rebuilt.

### 16.4 Upgrades

Release upgrades document supported source versions, database backup, migration duration, rollback limitations, and derivative invalidation. Web and worker releases cannot mix incompatible job/schema versions. Destructive migrations require an earlier compatibility release or an explicit operator checkpoint.

## 17. Failure behavior

| Failure | Required behavior |
| --- | --- |
| PostgreSQL unavailable | Reject new mutations and authorization-dependent delivery; show a degraded state; do not touch files |
| Root missing or replaced | Pause its mutation and destructive reconciliation work; alert operator; preserve catalog state |
| Disk or quota full | Abort cleanly, preserve originals, clean safe temporaries, return HTTP 507, and alert operator |
| Worker crash | Lease expires; an idempotent worker resumes after inspecting durable and filesystem state |
| Database failure after filesystem success | Operation enters reconciliation and repairs catalog/audit state |
| Watcher overflow or lost events | Focused hints may lag; checkpointed scan converges |
| Poison media/document | Bounded retries then quarantine processor job; original stays browsable/downloadable |
| Preview failure | Show fallback icon/original-download path and allow explicit retry |
| AI unavailable | Pause or fail enrichment only; file, media, and human organization remain functional |
| Stale browser mutation | Return 409 or 412 with current safe metadata and explicit resolution choices |
| Cross-filesystem move cleanup failure | Preserve verified destination, preserve/report source, and require cleanup rather than lose data |

## 18. Verification strategy

### 18.1 Unit and property tests

- raw name encoding and Unicode normalization;
- component validation and descriptor-relative root containment;
- symlink and mount-boundary behavior;
- additive grant calculation and permission-query construction;
- signed cursor binding, ordering, expiration, and tamper rejection;
- operation state-machine transitions and idempotency;
- permission matrices, authorization epochs, attempt fencing, and stale-worker rejection;
- source fingerprints and derivative invalidation;
- retry, lease, and scan-generation invariants.

Property tests generate adversarial names, directory shapes, cursor ties, and operation interruption points.

### 18.2 Integration tests

Integration tests use real PostgreSQL and temporary filesystems to cover:

- same- and cross-filesystem operations;
- upload resume/finalization/conflict;
- trash/restore/retention;
- user/group/root permission isolation;
- cross-root copy/move permission combinations and content-export boundaries;
- grant/account/root-mode revocation after enqueue and during a checkpointed operation;
- event plus scan convergence;
- focused-event versus scan-finalization races;
- external changes and missing mounts;
- path/object replacement between acceptance and the irreversible primitive;
- concurrent mutations, expired-lease overlap, and stale preconditions;
- duplicate/nested mount-slot aliases and cross-role root-identity mismatch;
- per-role database privileges, volume visibility, and internal-network egress denial;
- frontier outbox scoping, provider allowlisting, and absence of root mounts in the connector;
- crash points between every filesystem/database transition;
- file/directory synchronization and copy-to-trash durability sequencing;
- gateway authorization and byte ranges.

### 18.3 Hostile content tests

A maintained corpus covers malformed images, videos, PDFs, encodings, huge dimensions, deep metadata, decompression bombs, long CSV rows, formula-like cells, MIME mismatches, and processor timeouts. Tests assert resource bounds, job-scoped sandbox mounts, lack of database/network access in tool subprocesses, and that failures do not grant content access or corrupt originals.

### 18.4 Browser and accessibility tests

Playwright runs mobile-size Chromium and WebKit journeys for login, root isolation, 50,000-entry virtual browsing, upload interruption/resume, file commands, trash/restore, photo gestures, video playback/fallback, PDF/text/CSV viewing, transfers, dark theme, keyboard navigation, and accessibility semantics. Cache-isolation journeys explicitly cover user A viewing private thumbnails/originals, then logout, session expiry, grant revocation, back/forward navigation, and user B login; no A content may render without successful B authorization.

### 18.5 Performance tests

The generated 1M/50K fixture measures:

- cold and warm directory pages for every supported sort;
- filename and metadata search;
- timeline queries;
- API payload size and query count;
- indexer throughput and memory;
- browse latency during scan, thumbnail, transcode, and AI load;
- virtual list/grid browser memory and responsiveness;
- range delivery and first-preview latency.

Query plans and index sizes are captured with results.

### 18.6 Failure injection and recovery drills

Tests restart PostgreSQL and workers, revoke grants and filesystem permissions, expire a lease while its old worker is paused, fill staging/derivative destinations, lose watcher events, interrupt every copy/publish/sync step, replace or alias a root mount, corrupt job payloads, and restore from both application-consistent and crash-consistent backups. Phase 6 requires a documented operator-run recovery drill, not only mocked tests.

## 19. Delivery roadmap

### Phase 0 — Product design and legacy checkpoint

Deliver this specification, the central README/feature matrix, the named legacy tag, and an approved Phase 1 plan.

Gate: reviewed documents, recoverable legacy state, and a clean documentation checkpoint.

### Phase 1 — Secure platform foundation

Create the Django/React/PostgreSQL/Compose skeleton, same-origin credential sessions, bootstrap administration, users/groups, deployment mount slots, root activation, additive grants, opaque IDs, authorization epochs, fenced job/operation primitives, per-role database credentials and volumes, no-egress networks, health, migrations, and CI. The UI is the approved dark mobile shell, not yet a live large-directory browser.

Gate: through the deployed gateway, a user can sign in and see only authorized root shells; unauthorized metadata and delivery attempts fail; core checks pass.

### Phase 2 — Scalable drive core

Build checkpointed indexing, watcher ingestion, cursor browsing, filename search, resumable uploads, full file commands, root-local trash, reconciliation, audit, progress, the functional mobile Files/Transfers UI, and the performance fixture.

Gate: complete UI and API drive workflows meet correctness and server-latency budgets with 1M total entries and a 50K-entry folder.

### Phase 3 — Mobile media and documents

Complete installable PWA navigation and build thumbnails, timeline, photo/video viewers, range/HLS delivery, and PDF/text/CSV viewers on the Files/Transfers foundation from Phase 2.

Gate: mobile Chromium/WebKit, accessibility, payload, LCP, and INP journeys pass under representative data.

### Phase 4 — Search and human organization

Add EXIF/GPS/date/type/size filtering, document text, albums, tags, ratings, favorites, saved filters, and duplicate candidates.

Gate: combined search and organization remain permission-safe and stable through external reconciliation.

### Phase 5 — Local intelligence and optional frontier providers

Add local CPU embeddings/OCR/labels/captions, optional GPU execution, semantic search, provenance/confidence, optional face grouping, virtual smart albums, and explicit frontier connectors.

Gate: all core features pass with AI disabled and failed, local CPU is useful on baseline hardware, and network tests prove no unapproved content egress.

### Phase 6 — Release hardening

Complete backup/restore drills, hostile-media coverage, failure injection, authentication/rate-limit review, optional TOTP, deployment and upgrade documentation, resource tuning, and the v1 release checklist.

Gate: documented installation, upgrade, backup, restore, and recovery exercises pass on the supported deployment.

### Later work

WebDAV/sync, historical versions, controlled sharing, 10M+ certification, storage adapters, and collaboration each require a separate approved design. They may not weaken root authorization or mounted-filesystem ownership established here.

## 20. Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Database catalog drifts from external changes | Treat watchers as hints; use generation-safe checkpointed scans and operator-visible root health |
| Python becomes a file-delivery bottleneck | Authorize in Django and deliver ranges through Nginx |
| A 50K-entry directory overwhelms API or browser | Compound indexes, keyset cursors, bounded DTOs, no exact counts, and UI virtualization |
| Media/AI workers starve interactive traffic | Separate roles, priority queues, concurrency/I/O limits, and browse-under-load gates |
| Filesystem/database split creates half-completed commands | Durable idempotent journal, filesystem-first recorded transitions, destination-local temporary publication, and reconciliation |
| A queued or expired-lease operation outlives its authority | Execution-time grant/root/object checks, authorization epochs, attempt fencing, and short live advisory locks |
| Cross-filesystem move loses a concurrent source change | Verified/synchronized destination, source re-read, recoverable trash move, and copy-only degradation |
| Network filesystem semantics differ | Capability detection, conservative fallbacks, periodic scans, explicit documentation, and integration fixtures |
| Multi-user path escape or metadata leak | Opaque IDs, descriptor-relative containment, query-bound permissions, protected delivery, and adversarial tests |
| Browser cache exposes one user's media to another | No-store for private data, forced thumbnail revalidation, session/authorization cache namespaces, and A-to-B browser tests |
| Hostile processor reaches unrelated data or network | Per-job sandboxes, role-scoped mounts/database principals, internal networks, and an isolated frontier connector |
| Cloud AI violates privacy expectations | Disabled by default, separate egress profile, per-capability opt-in, payload minimization, and audit |
| Derivative growth consumes storage | Content-addressed manifests, quotas/watermarks, eviction, and reproducibility |
| All-at-once rewrite stalls | Phase-specific plans, first-usable drive gate in Phase 2, and acceptance criteria in the canonical README |

## 21. Resolved decisions

- Django modular monolith, not a microservice architecture.
- React/TypeScript mobile-first PWA.
- PostgreSQL, including the initial durable job queue.
- No required Redis.
- Mounted filesystem originals remain authoritative.
- Multiple accounts with additive user/group whole-root grants.
- Full file operations and recycle bin in the first usable drive phase.
- Root-local trash; Aegis-volume upload chunks and destination-local atomic publication.
- Indexed/keyset directory browsing; no synchronous browse scans.
- Watchers plus periodic reconciliation.
- Nginx authorized byte delivery and optional Caddy TLS profile.
- Dark-first design with optional system/light themes.
- Local CPU AI first, optional GPU, explicit frontier providers, and no silent fallback.
- AI organization is virtual and never automatically moves originals.
- Browser/API first; WebDAV and collaboration later.

## 22. Reference material

These sources inform implementation details but do not override this specification:

- [Django authentication documentation](https://docs.djangoproject.com/en/5.2/topics/auth/)
- [Django security documentation](https://docs.djangoproject.com/en/5.2/topics/security/)
- [Python os.scandir documentation](https://docs.python.org/3/library/os.html#os.scandir)
- [PostgreSQL multicolumn indexes](https://www.postgresql.org/docs/current/indexes-multicolumn.html)
- [PostgreSQL indexes and ORDER BY](https://www.postgresql.org/docs/current/indexes-ordering.html)
- [PostgreSQL table partitioning](https://www.postgresql.org/docs/current/ddl-partitioning.html)
- [tus resumable upload protocol](https://tus.io/protocols/resumable-upload)
- [pgvector](https://github.com/pgvector/pgvector)

## 23. Change control

The README feature matrix is the canonical public status. Every feature change updates its stable ID row and relevant phase status. Material changes to authorization, source-of-truth rules, mutation ordering, cloud egress, or performance budgets require an explicit design amendment before implementation.

The immediate next artifact after review is a Phase 1 implementation plan. No later-phase code should be pulled into that plan merely because this platform specification describes it.
