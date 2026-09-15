# Phase 2A.1 — Indexed browser design

**Date:** September 14, 2026

**Status:** Written specification approved on September 14, 2026. The [18-task implementation plan](../plans/2026-09-14-phase-2a-indexed-browser.md) is in progress; no package or benchmark acceptance is claimed.

**Parent:** [Complete-v1 Phase 2 delivery design](2026-09-14-phase-2-v1-delivery-design.md)

**Runtime baseline:** Phase 1 source through `f7c8800`; the `5b5e50b` checkpoint changes documentation only.

## 1. Outcome and package boundary

An authenticated user opens an assigned root and browses real indexed files and directories in the dark mobile UI. Results support bounded sorting and basic file-metadata filters. Scans run independently of browser requests, expose safe progress, recover from interruption, and never modify originals. A reproducible 1M-entry/50K-folder fixture tests the actual query and browser paths from the first package.

This is the first work package inside milestone 2A, not all of 2A or Phase 2. The remaining 2A packages add event ingestion, broader filename/path search, reconnectable event delivery, and authorized file downloads/ranges. Their byte-delivery and event-ingestion designs must be reviewed before those routes or handlers are enabled. No ordinary file can yet be opened, downloaded, or previewed by this package.

Managed uploads/versions/copies remain in 2B; photo/video thumbnails and viewers in 2C; document viewing/editing in 2D; rich organization/search in 2E; optional models in 2F; integrated release acceptance in 2G. These remain required within the expanded Phase 2, not deferred beyond it.

## 2. Architecture and module ownership

The existing Django/React/PostgreSQL/Compose deployment remains. No new network service, search engine, or required Redis is introduced.

| Module | Responsibility | Boundary |
| --- | --- | --- |
| `aegis_apps.catalog` | Entry identity, source metadata, logical projection, query/filter/cursor contracts, and read API | No original-root filesystem access in the web process |
| `aegis_apps.indexing` | Root scan policy/state, scan runs, directory work, safe enumeration, and fenced reconciliation | Runs in the existing indexer role with read-only roots |
| Existing roots/identity | Root activation, grants, epochs, session and CSRF policies | Reused, not duplicated in catalog code |
| Existing operations/common | Shared lease/heartbeat utilities, bounded execution, audit, and explicit database-role grants | Existing actor-bound operation authorization remains intact |
| Frontend Files feature | Virtualized directory browsing, basic filter panel/chips, navigation, details, and index state | Server order/authorization are authoritative; private state is session-scoped |
| Verification tooling | Synthetic source/catalog generation, query plans, latency/memory measurement, and browser journeys | Isolated resources only; no operator files or existing database |

The current worker only executes `foundation.probe` synchronously. This package adds supervised long-running scan execution with independent lease renewal and heartbeat publication. It does not simply place a long scan inside that probe loop and assume the lease stays valid.

## 3. Source identity and catalog model

`CatalogEntry` has an opaque UUID, root UUID, source-parent UUID, exact raw source-name bytes, safe display name, entry kind, indexed size/timestamps, optional device/inode identity, source revision, catalog version, observation epoch, scan marker, and source availability state. A root has one synthetic directory anchor with an empty internal source name. Child names cannot be empty, NUL, slash, `.` or `..`.

Source ancestry and logical ancestry are distinct fields. In this package their default projections match, but filesystem resolution uses only source-parent/raw-name fields. Future logical renames and moves set explicit overrides; scanner updates must never overwrite such overrides or infer a filesystem rename from them. Human metadata and derived assets attach through stable opaque IDs and explicit revisions, not display-name strings.

Database constraints enforce same-root parent relationships, one root anchor, unique exact source location within a directory, valid kinds/states, and nonnegative versions/counters. A changed source at an existing location increments the source revision and catalog version. Normalized names are sort/search keys, never uniqueness or source-identity keys. Case-adjacent and normalization-equivalent names must remain distinct when the source filesystem permits them.

The first package does not guess cross-directory rename identity from an inode alone: a disappearance/reappearance can produce a missing location and a new location. Later focused reconciliation may correlate a move only with unambiguous evidence. Hard links are separate locations, and there is no speculative content hashing during the initial metadata scan.

Source names are retained byte-for-byte. Invalid UTF-8, control characters, and bidirectional formatting characters use an escaped, non-executable display representation; the raw bytes never become trusted browser paths or shell arguments. Sorting uses a versioned NFC/case-folded key with deterministic binary collation and UUID ties; it does not depend on the host's locale or a browser's client-side sort. The implementation must prove index-key bounds for the supported filesystem name limits and must not silently truncate identity or ordering keys.

File size and nanosecond timestamps retain full precision. Values that may exceed JavaScript's safe integer range are decimal strings in JSON. Filesystem modification time is not labeled as capture time. Coarse file type is an extension-derived catalog hint, not proof that content is safe to render.

Kinds include directory, regular file, symlink, and unsupported special entry. Symlinks are inert. Devices, sockets, and FIFOs are metadata-only and are never opened for content. Source state (`present`, `missing`, `inaccessible`, `unsupported`) is separate from future user archive/hide state.

## 4. Root maintenance authority

The catalog is maintained for an administratively activated root; it is not owned by whichever user last viewed that root. `RootIndexState` binds the root, active mount-manifest identity, scan-policy epoch, reconciliation epoch, due time, active scan generation, and last safe health/progress state.

Indexing uses dedicated root-maintenance records (`ScanRun` and `DirectoryWork`) rather than inserting actorless entries into the existing `Operation` journal. Shared lease/attempt/heartbeat mechanisms may be extracted for reuse, but their authority checks remain separate:

- User operations require the current actor, grants, expected versions, root identity, and operation fence as before.
- Root-maintenance work requires the actual indexer database role, active configured root, unchanged policy/manifest/authorization epoch, current directory attempt, and unexpired lease.
- Root deactivation, mount identity change, or policy replacement fences maintenance commits. Ordinary user logout does not stop a configured root's periodic catalog maintenance.
- Manual rescan requests require `ROOT_ADMIN`, CSRF, a client request ID, and an audit event. Superuser status alone is insufficient. The request coalesces existing work rather than spawning duplicate scans. Its requesting actor is recorded for audit, not used as a permanent scan credential.

There is no synthetic administrator account, arbitrary-root path parameter, blanket system bypass, or relaxation of the existing actor authorization function. Indexer credentials do not gain password, session, or group-membership access.

The default root policy schedules an initial scan after activation and another scan one hour after the prior run settles; operators can set a bounded interval from one minute to seven days. At most one scan run is active per root, with one directory reader per root initially. Extra demand coalesces into a dirty/rescan request. Scan workers have separate bounded concurrency so a large root cannot monopolize all active roots.

## 5. Safe and checkpointed scanning

Source enumeration starts from the configured read-only root descriptor. Under the September 15 approved runtime adjustment, the supervised child acquires that descriptor from the fixed current-attested slot, checks its mount ID/path, observer-derived fingerprint and read-only leaf-mount state, then uses descriptor-relative no-follow resolution. A browser path never chooses the slot. Host filesystem/inode values are not substituted for container-observed identity on Docker Desktop. The reader validates root and directory stability, rejects mount transitions, and does not follow symlinks. Existing host preflight and runtime mount attestation remain mandatory; a matching path prefix is not containment proof.

Each run captures a scan generation and the root reconciliation epoch. Directory work is durable and claimed with an expiring lease and monotonic attempt token. Only the indexer role can claim or settle this work. No queue row is created for every regular file.

For one directory:

1. Verify live root/policy/manifest state and the claimed attempt, then open and identify the directory safely.
2. Enumerate through `os.scandir` and obtain no-follow metadata in bounded batches; never materialize the complete directory or tree. Recover raw names using the filesystem encoding's lossless byte conversion when enumeration returns strings.
3. Bulk-upsert observations and enqueue child-directory work in short transactions. Start with 500 observations per batch and a 1 MiB encoded payload limit, flushing when either limit is reached. Configurable record counts stay between 100 and 2,000; the byte limit remains fixed. The result channel holds at most two batches per reader, excluding fixed-size control messages.
4. Renew the attempt and publish heartbeat/progress independently of slow metadata I/O. Every catalog commit rechecks the live lease, root epochs, and directory attempt.
5. Reach successful end-of-directory, recheck directory/root identity and stability, and mark that attempt eligible for finalization.
6. Finalize unseen locations in bounded batches under the same per-directory coordination lock used by later focused reconciliation. Mark missing only rows not observed by this completed generation and whose observation epoch is no newer than its captured start epoch.
7. Mark the directory complete and continue through its durable children. Settle the root run only after every directory has a recorded complete or degraded result.

A reader is supervised separately from the control loop and returns observations through the bounded result channel. It receives no database handle and opens no database connection; the supervisor owns database work, lease renewal, heartbeat, and cancellation. Both processes remain inside the indexer's existing trust boundary: process separation is a liveness mechanism, not an additional security sandbox. A stalled reader, lost database connection, shutdown, or expired attempt closes further commit admission. Local cancellation is ordered against a final per-attempt commit-admission point: cancellation winning before that point rolls back the transaction, including idle and between-statement windows. A commit already admitted can resolve after a later stop request; the worker remains stopping until its outcome resolves, without admitting another transaction or claiming that commit was undone. This ordering does not relax database lease, root, attempt, or manifest fences. If a blocked filesystem call cannot be terminated promptly, mark the root degraded and stop launching replacement readers for that root until the previous reader is reaped; retries must not accumulate hung processes.

The approved coordination volume is local, fixed at `/srv/aegis/indexer-coordination`, and mounted only by indexer. Opaque per-root locks and a deployment-admission lock share inherited open-file descriptions between supervisor and reader; they are never removed/replaced or explicitly unlocked while source access may survive. One coordinator owns the bounded reader pool. A coordinator crash with an uninterruptible child can delay replacement admission until that child's source access ends. Missing/unwritable/unsupported storage fails closed, and all indexers sharing the deployment/database must share it. Independent volumes or arbitrary multi-host failover are not supported by this contract. Owned children are actually reaped; runtime init/reaping and source-access exclusion need separate tests. Only coordination metadata may be created in this volume; original-root writes remain forbidden. Details and implementation status are in the [runtime coordination decision](../plans/2026-09-14-phase-2a-indexed-browser.md#task-7-runtime-coordination-decision).

The reader has a progress timeout, not a small fixed total duration that would make every large folder fail. A directory with 50,000 children may take minutes while continuing to produce bounded batches and renew its lease. The no-progress timeout defaults to 120 seconds, with operator overrides from 30 to 3,600 seconds; a timed-out pass cannot finalize missing entries.

On interruption, completed directories remain complete. An unfinished directory is re-enumerated from the beginning with idempotent upserts and a fresh attempt; a saved `scandir` offset or last filename is not treated as a portable resume cursor. This trades some repeated metadata work for correctness without holding the folder in memory. A bounded directory queue and batches remain durable across process restart.

## 6. Reconciliation and failure semantics

An enumeration error, source identity mismatch, lost mount, permission failure, incomplete pass, or stale attempt cannot finalize a directory as empty. Prior catalog records remain intact. Individual metadata errors produce an inaccessible observation or a degraded directory, not evidence that the whole subtree was removed.

The root reconciliation epoch is monotonic. Both scan commits and future focused observations record their epoch. Finalization uses compare-and-swap predicates so a newer observation cannot be overwritten by an older scan. Directory completion and finalization must also verify the expected parent location/version; an external parent replacement cannot apply old scan results to its replacement.

Missing entries are retained as catalog tombstones, never byte-deletion jobs. Missing/inaccessible ancestor state is checked when resolving a directory or direct entry ID, so a direct URL cannot incorrectly expose descendants as currently available. The UI distinguishes last-indexed metadata from current source availability and never invents an empty successful result for an unavailable root. Existing service readiness checks are not weakened to conceal a missing dependency.

The catalog is eventually consistent, not a cross-request filesystem snapshot. Changes concurrent with enumeration may require a subsequent pass. Periodic scans establish convergence when changes settle. Event-driven reconciliation is a later 2A package and must use the same epoch/locking boundary rather than writing catalog truth directly from watcher hints.

## 7. Query and API contract

All new product endpoints remain under `/api/v1/` and use the existing session/problem-response conventions.

| Endpoint | Contract | Permission |
| --- | --- | --- |
| `GET /api/v1/roots/{root_id}/entries` | One indexed directory page; omitted `parent` selects its root anchor | `BROWSE` on the active root |
| `GET /api/v1/entries/{entry_id}` | Bounded details and authorized ancestor IDs/display labels; no content or absolute source path | `BROWSE` on the entry's active root |
| `GET /api/v1/roots/{root_id}/index-status` | Safe availability, generation, timestamps, observed counters, and degraded state | `BROWSE` on the root |
| `POST /api/v1/roots/{root_id}/scans` | Idempotent/coalesced rescan request returning `202` and an opaque scan ID | `ROOT_ADMIN` plus CSRF; no implicit browse permission |

Directory responses contain `entries`, `nextCursor`, `previousCursor`, a directory/catalog contract version, and safe index-state information. Entry summaries contain only IDs, display name, kind/type hint, size/modified time, source state, and version. Detailed metadata is separate. There is no exact total count, host path, raw filename byte string, slot ID, file content, or unsigned delivery URL.

Default page size is 100, maximum 250. The cursor is signed, expiring, opaque to the client, and bound to session namespace, user/root authorization epochs, root/parent IDs, normalized filters, sort/direction, the last sort tuple, and cursor/sort-key versions. Initial expiry is 15 minutes. The encoded cursor is capped at 8 KiB and uses only the uncompressed representation emitted by the server; compressed signed objects are rejected before decoding. Tampering, malformed payloads, context mismatch, or expiration returns a safe restartable cursor error after authorization; a cursor never substitutes for current authorization.

Every query starts with the active user and effective permitted root, then the directory boundary. Parent and entry UUIDs are resolved inside that boundary. Authorization is part of the query/transaction boundary, not filtering after a global result has been fetched. Anonymous requests receive the existing authentication error; unknown and unauthorized objects are indistinguishable. Grant changes invalidate the session/cursor context. Results already in flight are fenced by the frontend's existing authentication transition ownership.

Three initial sorts are name, modified time, and size, ascending or descending, with directories first, explicit null ordering, and a UUID tie-breaker. Previous-page navigation uses the matching reverse keyset, not offset arithmetic. The client deduplicates IDs when the catalog changes; no cross-request database snapshot is promised.

Use narrow list projections and compound indexes beginning with root/directory/visibility and matching the selected ordering. Keep index variants intentional and measure their combined size. PostgreSQL documents how [leading columns constrain multicolumn index scans](https://www.postgresql.org/docs/18/indexes-multicolumn.html) and why [large offsets still incur skipped-row work](https://www.postgresql.org/docs/18/queries-limit.html). These mechanisms motivate the design; benchmark evidence must establish actual latency.

## 8. Basic filter contract

The first package supports root/current directory, entry kind, extension-derived type, file size range, modified-date range, source availability, and a literal filename prefix within the current directory. Default results omit missing tombstones; an explicit availability filter can inspect indexed missing/inaccessible locations without claiming bytes are available.

Accept a versioned typed schema with allowlisted fields. Multi-select values within a field are OR; different fields combine with AND. The unknown extension-derived type uses the reserved token `__unknown__`, distinct from every lowercase alphanumeric extension, including `.unknown`. Size bounds are inclusive; date ranges use an inclusive lower and exclusive upper bound with an explicit offset/time zone. Missing values do not match a numeric/date range; finding missing values requires the corresponding explicit state. Filename text is literal, not a regex, wildcard program, or arbitrary field expression.

Validation caps the request to eight supported fields, 32 selected values in a field, and 8 KiB of normalized filter data. Invalid bounds or unknown fields fail clearly. Only comparisons advertised by this package are accepted. A changed filter resets the cursor. The implementation tests every advertised sort/filter combination against adversarial/selective fixture distributions; a slow combination is an optimization/acceptance failure, not permission to ignore the filter or load the whole folder in the client.

The UI uses static kind/state options and permission-scoped, bounded type suggestions where needed. It does not run an exact global count for each option. Rich camera/capture/video/location controls are added to the same schema when 2C extraction is available, and human organization/saved-search controls arrive in 2E. Their absence is a named package boundary, not fake empty metadata or a silently inactive filter.

## 9. Mobile Files behavior

Root cards navigate to authenticated `/files/{root_id}` and directory views use opaque directory IDs. Existing login, logout, epoch invalidation, dark tokens, reduced motion, and 44-by-44 minimum controls remain. File details open a metadata panel; content actions are not exposed until the corresponding delivery package passes.

Render a virtualized list using a focused virtual collection utility, keeping the server's order. Initially retain at most five pages for the active directory; retain only a bounded set of recent directory navigation records and evict their data when the budget is exceeded. Bidirectional keysets fetch evicted neighbors as needed and restore a stable visible-row anchor. An unbounded ID-deduplication set, selection set, or query cache is not an acceptable workaround for a bounded DOM.

TanStack Query is already installed. Its [bounded infinite-query support](https://tanstack.com/query/latest/docs/framework/react/guides/infinite-queries#what-if-i-want-to-limit-the-number-of-pages) informs this page-window design. The implementation must also cap inactive query retention and cancel obsolete requests. Virtualization alone is not browser-memory acceptance.

The Filters button opens a focus-managed panel with grouped controls, draft values, Apply/Cancel, clear-all, and removable active chips. The user can reach and operate every control with keyboard and screen reader. Loading more rows announces status without continually stealing focus. A visible load-more control provides an accessible fallback to scroll-triggered fetching.

Private filter/navigation state is scoped to the current authenticated namespace, not global local storage. This package does not add persistent transfer metadata or a service worker. A revoked/expired session closes the private list before reauthentication; late browse responses cannot reopen it. Browser back/forward restoration revalidates the session before showing cached content.

Indexing, not-yet-indexed, empty, inaccessible, missing, and failed-request states are distinguishable. Scanning progress displays observed entries/directories and a timestamp, not an invented exact percentage before a denominator exists. The status poll is bounded, backs off when idle, pauses in hidden/offline tabs, and is replaced or supplemented by reconnectable events in a later 2A package.

Photos and videos display type icons in this package. Milestone 2C replaces them with real responsive thumbnails/video posters through the shared revision-based derivative contract. Placeholder icons do not count as implementing `MEDIA-001`.

## 10. Database, deployment, and audit integration

Use real PostgreSQL migrations and extend the existing explicit schema/column/function allowlists. Migrator owns schema and privileged maintenance functions; runtime roles never become table owners or database superusers. Functions have a fixed empty search path, exact caller-role checks, bounded arguments, and no public execute grant.

Web reads catalog metadata through authorized queries and invokes narrowly scoped rescan-request services. Indexer may observe/maintain catalog source columns and directory work only through transactions/functions that validate the current root-maintenance attempt. It cannot modify user organization fields or invoke user-publication operations. Operations/media gain no broad catalog writes merely because a new app was added. Tests connect as the real roles and prove denied accesses as well as successful work.

The database itself must reject a stale attempt's observation/finalization commit in the supported scanner-owned transaction flow; a Python precheck alone leaves a race. Each checkpoint wrapper owns a single top-level transaction, refuses caller-owned outer transactions, explicitly leaves the named commit guard deferred, and returns only after commit. Maintenance writes, source revisions, and child-work insertion commit atomically with the attempt/root checks.

On September 15, 2026, the user explicitly approved stock PostgreSQL with this controlled transaction model. Deliberately tampered indexer SQL sessions that force deferred checks early are excluded only from the absolute lease-deadline guarantee; ordinary pauses, timeouts, crashes and restarts remain covered. Root/epoch/attempt/revocation serialization, organization permissions and original-file protections remain database-enforced or read-only as before. The [decision and reproduced limitation](../plans/2026-09-14-phase-2a-indexed-browser.md#task-6-commit-fence-decision) are explicit, not a claim that the early-check bypass was fixed. No extension, new service or broader role authority is introduced.

No catalog model/admin route exposes permanent file deletion. SQL cleanup of disposable work records, if introduced, is distinct from content deletion and cannot cascade-delete entries, originals, audit, or future published-version history.

The existing indexer container receives the same read-only original mounts, plus the explicitly approved indexer-only coordination volume and runtime init/reaping. No writable-original override, Docker socket, privileged mode, new public port, or outbound route is added. Live scan counters extend the existing safe status/heartbeat surface. Audit records root scan requests and safe outcomes with opaque IDs, not raw paths, filename lists, filter text, or credentials.

## 11. Scale fixture and measurement

Add separate deterministic fixture modes:

1. A catalog fixture bulk-loads at least 1,000,000 realistic metadata rows, with a 50,000-child folder, multiple roots/users/groups, sort ties/nulls, varied types, inaccessible/missing records, and selective/common filter distributions. It measures the real authenticated API and query plans.
2. A synthetic filesystem fixture creates disposable tiny/sparse test content with the same target entry counts to exercise actual enumeration, checkpoints, naming, restart, and scan memory. It never uses or copies a user library. The manifest records the seed, shape, name cases, source sizes, and resulting counts; catalog-only seeding is not scanner throughput evidence.

Keep fixtures isolated from the application database and from each other. The large filesystem fixture is opt-in, checks its explicit destination/available space and ownership before creation, and records exactly which synthetic resources it owns. Cleanup cannot target a supplied original root, workspace root, or existing operator volume. Preserve benchmark reports outside fixture cleanup.

The [reference hardware/workload](2026-08-31-aegis-platform-rewrite-design.md#53-reference-benchmark-profile) remains the certification target. Local and CI runs identify their actual environment and are labeled regression/provisional evidence when not on the calibrated reference host.

For this package, measure first/next/previous pages, all supported sorts/basic filters, details, and scan status. Report this package-specific mix separately from the final full-v1 workload. The initial worker-load profile uses an actual scan; preview, transfer, transcode, and model work are not yet represented. Later packages extend the workload, and 2G repeats the complete contract.

Gate values inherited from the platform include p95 browse latency at most 300 ms unloaded and 500 ms with the applicable worker load; indexer RSS at most 750 MiB; catalog tables plus indexes at most 8 GiB; catalog-only initial scan within four hours on the calibrated root; mobile LCP at most 2.5 s, INP at most 200 ms, and heap at most 250 MiB during the defined long journey. Capture cold/warm results separately, image/schema identity, query plans, query counts, payload sizes, CPU/memory limits, storage calibration, and browser versions. Do not mark the reference gate passed based on a faster development machine or a small fixture.

## 12. Acceptance checklist

- A clean, locked Compose deployment upgrades the existing Phase 1 schema without recreating its database or weakening role privileges.
- Direct/group-granted users browse only their roots; an ungranted superuser sees no catalog. Foreign parent/entry IDs, altered cursors, stale sessions/epochs, and unauthorized facet options fail safely.
- List/details/status API tests prohibit original filesystem enumeration in the web role and use bounded query counts, projections, page sizes, and filter schemas.
- Real source enumeration preserves raw names, source locations, content bytes, size, and modification timestamps across success, error, cancellation, mount loss, and restart. Tests isolate these from external writers; host-controlled access-time bookkeeping is not an application write guarantee. Symlinks and special files stay inert.
- Interrupted/failed directories cannot finalize missing records. Paused old workers using the supported scanner-owned transactions cannot commit after lease/root fencing, including terminal transitions. Actual-role tests separately demonstrate normal expiry rejection and the documented deliberate constraint-timing limitation; the latter is not a skip, expected failure or security-fix claim. Complete directory checkpoints survive restart; unfinished passes safely re-enumerate.
- Logical organization overrides and newer observation epochs survive scanner updates, even though user organization routes are not implemented in this package.
- Mobile Chromium/WebKit journeys cover paging both directions, sorting/filter reset, directory navigation/restoration, accessibility, empty/degraded states, logout, account switching, and late-response suppression.
- The deterministic 1M/50K fixture exercises the actual API/browser path. Query/scan/memory evidence states the hardware and exact package workload. Reference certification remains open if the reference host was not exercised.
- The existing canonical backend/frontend/deployment/browser checks pass with original Phase 1 regressions retained; README, package task ledger, migration/runbook instructions, and sanitized evidence match the tested revision.

No acceptance of this package advances thumbnails, document editing, transfers, full event reconciliation, or file delivery to Verified. `FILE-001` may advance when its full cursor API gate passes; features spanning packages retain partial status until complete. Actual task counts are assigned by the subsequent implementation plan, not inferred from the number of sections in this design.

## 13. Review and next step

The user approved this focused design together with the Phase 2 umbrella scope on September 14, 2026. Create the executable 2A.1 implementation plan with exact files, migrations, test-first tasks, role-privilege changes, and verification commands, then implement it on `main` with scoped commits and regular pushes. The next 2A work packages retain the broader filename/event/progress/delivery obligations before milestone 2A can be accepted.
