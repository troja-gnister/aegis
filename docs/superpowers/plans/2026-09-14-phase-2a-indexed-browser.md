# Phase 2A.1 Indexed Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a permission-scoped, read-only indexed file browser with recoverable scanning, basic metadata filters, bounded mobile paging, and reproducible 1M-entry/50K-folder measurements.

**Architecture:** Add catalog and indexing modules to the existing Django monolith. PostgreSQL owns catalog queries, scan checkpoints, and database-enforced maintenance fences; a supervised reader enumerates originals only in the existing indexer container. React uses authenticated, bidirectional cursor pages and a bounded virtual list, with no filesystem work in web requests.

**Tech Stack:** Existing Python 3.13, Django 5.2 LTS, DRF 3.16, psycopg 3, PostgreSQL 18, React 19.2, TypeScript, TanStack Query 5, Node.js 24, Vite 8, pytest/Hypothesis, Vitest, Playwright, and Compose. Add `@tanstack/react-virtual@3.14.13` for list virtualization and benchmark-only dev dependency `web-vitals@6.2.1`, both with the npm lock; the metrics library is not imported into the application bundle.

**Spec:** [Approved 2A.1 indexed-browser design](../specs/2026-09-14-phase-2a-indexed-browser-design.md), under the [approved complete-v1 Phase 2 scope](../specs/2026-09-14-phase-2-v1-delivery-design.md). Both written specifications were approved September 14, 2026.

## Global Constraints

- Work continues on `main`, with scoped verified commits and regular pushes. No automatic release tag or production deployment follows from a milestone being accepted.
- Every original root stays read-only in every mounted role. Web receives no originals. No API, job, editor, duplicate action, or recovery path may create, unlink, overwrite, move, or rename original files.
- Roots containing descendant filesystem mounts remain rejected. Ordinary subdirectories are allowed, and a selected root may itself be a mountpoint.
- Physical source identity and logical organization are separate. Reconciliation updates observed source metadata, never user-authored organization or published-version history.
- Only regenerable derivatives and unpublished, operation-owned temporary artifacts outside original roots may be cleaned. Source files, published managed versions, and existing operator databases are never test-cleanup targets.
- No ordinary file can yet be opened, downloaded, or previewed by this package.
- No new network service, search engine, or required Redis is introduced.
- All new product endpoints remain under `/api/v1/` and use the existing session/problem-response conventions.
- Default page size is 100, maximum 250.
- Initial cursor expiry is 15 minutes. Cursors bind session namespace, user/root epochs, root/parent IDs, normalized filters, sort/direction, last sort tuple, and cursor/sort-key versions.
- Validation caps the request to eight supported fields, 32 selected values in a field, and 8 KiB of normalized filter data.
- Start with 500 observations per batch and a 1 MiB encoded payload limit, flushing when either limit is reached. Configurable record counts stay between 100 and 2,000; the byte limit remains fixed. The result channel holds at most two batches per reader, excluding fixed-size control messages.
- The no-progress timeout defaults to 120 seconds, with operator overrides from 30 to 3,600 seconds; a timed-out pass cannot finalize missing entries.
- Root scan intervals range from one minute to seven days, defaulting to one hour after the preceding run settles; one active run and one reader per root.
- Initially retain at most five pages for the active directory. Bound inactive queries, navigation records, deduplication, and selection as well as the DOM.
- Existing login, logout, epoch invalidation, dark tokens, reduced motion, and 44-by-44 minimum controls remain.
- No catalog model/admin route exposes permanent file deletion.
- Preserve the foundation dependency floors: Python `>=3.13,<3.14`, Django `>=5.2.17,<5.3`, DRF `>=3.16.1,<3.17`, PostgreSQL major 18, Node.js major 24, React `>=19.2,<20`, Vite `>=8.1,<9`; frozen installs use the tracked locks.
- Use real PostgreSQL, actual runtime-role logins, and the existing canonical verification commands. Do not use SQLite or inherited application database settings as test substitutes.
- Do not operate on the existing `aegis_postgres-data` volume or any operator deployment. Large fixtures require explicitly owned synthetic resources; cleanup validates exact identities and preserves reports.
- Keep Phase 1 tests and historical evidence. Updating a plan is not implementation evidence. This package cannot complete milestone 2A or advance media/document/transfer features to Verified.

---

## Status and task ledger

Planning baseline: `842ba2a` on `main`, with unchanged application code from the verified Phase 1 foundation; execution begins from the committed plan at `6169402`. This plan defines **18 tasks: 1 complete, 17 remaining**. Task 1 passed verification and review; Task 2 is next. The ledger is authoritative; checkboxes below record the execution recipe and subsequent evidence, not a second task count.

| Task | Independently testable deliverable | Depends on | Status |
| --- | --- | --- | --- |
| 1 | Lossless filename/order domain and safe focused verification | Foundation | Complete (`104208f`) |
| 2 | Catalog schema, constraints, and explicit role grants | 1 | Planned |
| 3 | Deployment-bound root maintenance schema and configuration | 2 | Planned |
| 4 | Database-enforced scheduling, claims, renewal, and rescan authority | 3 | Planned |
| 5 | Descriptor-relative, bounded read-only directory reader | 1 | Planned |
| 6 | Atomic observations, checkpoint finalization, and stale-work rejection | 4, 5 | Planned |
| 7 | Supervised scan execution and independent worker liveness | 6 | Planned |
| 8 | Typed filters and signed cursor contracts | 1, 2 | Planned |
| 9 | Permission-bound indexed keyset queries and details | 3, 8 | Planned |
| 10 | List/details/status/rescan HTTP endpoints | 4, 9 | Planned |
| 11 | Validated browser API and bounded private query window | 10 | Planned |
| 12 | Virtualized mobile file navigation | 11 | Planned |
| 13 | Filter panel, details, and scan-state interactions | 12 | Planned |
| 14 | Real-stack browser and original-preservation regressions | 7, 13 | Planned |
| 15 | Deterministic catalog fixture and authenticated query benchmark | 10 | Planned |
| 16 | Owned filesystem fixture, scan/recovery, and resource benchmark | 7, 15 | Planned |
| 17 | 50K-folder mobile journey and package workload evidence | 14–16 | Planned |
| 18 | Fresh-checkout acceptance, upgrade runbook, and reconciled roadmap | 1–17 | Planned |

Later 2A plans still own watcher/event ingestion, broader filename/path search, reconnectable events, and authorized downloads/ranges. All 2B–2G milestones remain required. Their task counts have not been assigned, so 18 is not the remaining-task count for the full rewrite.

### Completed-task evidence

| Task | Tested revision | Verification and review | Remaining boundary |
| --- | --- | --- | --- |
| 1 | `104208f` (initial implementation `7489be9`) | 673 backend tests; 35 filename cases; Ruff and mypy clean (161 files). The unchanged verifier's actual owned-cleanup regression passed at `7489be9`. Independent review and one scoped fix review passed. All disposable databases were removed. | Domain and verification tooling only; no catalog schema, browsing API, UI or scale acceptance. |

Task 1's review corrected the recipe's supplementary-Unicode escape ambiguity with a failing collision regression and fixed-width escapes. Raw identity and the proven 1,530-byte maximum key remain intact; no persisted catalog/cursor existed during this pre-release correction.

## File and responsibility map

| Location | Ownership |
| --- | --- |
| `backend/aegis_apps/catalog/{domain,names,models}.py` | Stable types, name safety/order, source versus logical catalog identity |
| `backend/aegis_apps/catalog/{filters,cursors,authorization,queries,serializers,api}.py` | Typed query input, cursor signatures, grant-bound metadata reads, DTOs and HTTP |
| `backend/aegis_apps/catalog/migrations/` | Catalog constraints and measured indexes; no filesystem migration |
| `backend/aegis_apps/indexing/{config,models,binding}.py` | Deployment binding, bounded scan policy, durable root/run/directory state |
| `backend/aegis_apps/indexing/{database,scheduling,checkpoints}.py` | Small typed wrappers around fixed maintenance SQL functions |
| `backend/aegis_apps/indexing/sql/{schedule,lease,observations,finalize}.sql` | Role-checked, fixed-search-path database transactions and fences |
| `backend/aegis_apps/indexing/{reader,protocol,supervisor,runner}.py` | Read-only FD operations, bounded IPC, reader lifecycle, worker integration |
| `backend/aegis_apps/indexing/{services,selectors,serializers}.py` | Audited manual requests and safe scan summaries |
| `backend/aegis_apps/common/database_privileges.py` | Exact schema/column/function allowlists; integrate new SQL without relaxing old ones |
| `backend/aegis_apps/common/management/commands/deploy_database.py` | Atomic installation of current index binding alongside deployment metadata |
| `backend/aegisctl/mounts.py` | Mount-manifest configuration for migrator, never original-root mounts for it |
| `backend/aegis_apps/operations/management/commands/run_role.py` | Preserve foundation-role behavior; dispatch indexer maintenance through its supervised runner |
| `frontend/src/features/files/` | Types/API, bounded query state, navigation, virtual list, filters, details, progress |
| `frontend/src/features/roots/RootCard.tsx`, `frontend/src/app/router.tsx` | Replace root explanation with real authenticated browse navigation |
| `scripts/verify.py`, `conftest.py`, `tests/support/database_roles.py`, `backend/tests/conftest.py` | Isolated focused checks, shared actual-login fixtures, and synthetic catalog fixtures |
| `scripts/benchmarks/`, `backend/aegis_apps/catalog/management/commands/seed_catalog_benchmark.py` | Isolated fixture ownership, deterministic data, HTTP/scan measurements and sanitized reports |
| `frontend/e2e/phase2a.spec.ts`, `frontend/e2e/phase2a-scale.spec.ts` | Functional mobile journeys and separately opted-in long scale run |
| `tests/deployment/test_database_roles.py`, new focused deployment tests | Actual-login denial/fencing, read-only containers, upgrade and fixture safety |
| `docs/operations/phase-2a-indexing.md`, `docs/verification/phase-2a.md` | Tested operator instructions, measured evidence, remaining certification limits |

Use focused modules; do not append the scanner, query builder, or new SQL bodies to the existing large worker/privilege files. Migration filenames and column order below are contracts: if Django generates a different ordinal layout, update the explicit allowlist and its tests in the same task.

## Cross-task contracts

These names and field spellings are shared interfaces. Task implementations import them instead of inventing parallel DTOs.

```python
# catalog/domain.py — implemented in Task 1
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

class EntryKind(StrEnum):
    DIRECTORY = "directory"
    FILE = "file"
    SYMLINK = "symlink"
    SPECIAL = "special"

class SourceState(StrEnum):
    PRESENT = "present"
    MISSING = "missing"
    INACCESSIBLE = "inaccessible"
    UNSUPPORTED = "unsupported"

@dataclass(frozen=True, slots=True)
class SourceName:
    raw: bytes
    display: str
    order_key: bytes
    type_hint: str | None

@dataclass(frozen=True, slots=True)
class Observation:
    name: SourceName
    kind: EntryKind
    state: SourceState
    size: int | None
    mtime_ns: int | None
    ctime_ns: int | None
    device: int | None
    inode: int | None

@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int

# indexing/database.py — implemented in Task 4
@dataclass(frozen=True, slots=True)
class ScanLease:
    run_id: UUID
    work_id: UUID
    root_id: UUID
    directory_id: UUID
    worker_id: str
    attempt: int
    generation: int
    start_epoch: int
    binding_epoch: int
    policy_epoch: int
    root_epoch: int
    parent_revision: int
    manifest_identity: str
```

`ScanLease` is authority input, not a guarantee of live authority: every mutation checks its fields and database time while holding the relevant database locks. IPC contains observations, never a database connection, session, or an executable Python pickle supplied by a reader.

### Maintenance database contract

The function registry has fixed names and signatures. Calls use bound values, never caller-selected SQL identifiers. `session_user` proves the authenticated database login; `current_user` inside a security-definer function is not that proof.

| Function | Identity arguments | Result | Execute authority |
| --- | --- | --- | --- |
| `aegis_schedule_root_scan` | `uuid, text, text` | run UUID or null | indexer only; root ID, worker ID, current manifest digest |
| `aegis_request_root_scan` | `uuid, uuid, bigint, text` | run UUID | web only; root ID, actor ID, actor epoch, request ID; live `ROOT_ADMIN` |
| `aegis_claim_scan_directory` | `uuid, text` | bounded lease JSON or null | indexer only; run ID, worker ID |
| `aegis_renew_scan_directory` | `jsonb` | boolean | indexer only; typed lease |
| `aegis_record_scan_batch` | `jsonb, jsonb` | inserted/changed/observed counters | indexer only; lease, bounded observation batch |
| `aegis_seal_scan_directory` | `jsonb, jsonb` | boolean | indexer only; lease, successful end-of-directory identity |
| `aegis_finalize_scan_directory` | `jsonb, integer` | bounded affected count and completion boolean | indexer only; lease, limit at most 500 |
| `aegis_fail_scan_directory` | `jsonb, text` | boolean | indexer only; lease, enumerated safe error code |

Scheduling may settle an exhausted/degraded run and start its next due generation. There is no general SQL executor, caller-selected target table, or actorless insertion into `operations_operation`.

### HTTP and browser wire contract

All endpoints omit a trailing slash to match existing APIs. Filters are one JSON-valued `filters` query parameter, with `v: 1` and optional `kind`, `type`, `size`, `modified`, `availability`, `prefix`; root and directory are URL/query boundaries, not an arbitrary cross-root filter. Reject duplicate query parameters. `sort=name|modified|size`, `order=asc|desc`, `limit=1..250`, `parent=<UUID>`, and `cursor=<signed value>` are the other allowlisted fields. Unknown parameters fail with `400` after authentication and root authorization.

```typescript
// features/files/types.ts — implemented in Task 11
export type EntryKind = "directory" | "file" | "symlink" | "special";
export type SourceState = "present" | "missing" | "inaccessible" | "unsupported";
export type FileFilters = {
  v: 1;
  kind?: EntryKind[];
  type?: string[];
  size?: {min?: string; max?: string; unknown?: true};
  modified?: {from?: string; before?: string; unknown?: true};
  availability?: SourceState[];
  prefix?: string;
};
export type EntrySummary = {
  id: string; rootId: string; displayName: string; kind: EntryKind;
  typeHint: string | null; size: string | null; modifiedNs: string | null;
  sourceState: SourceState; version: string;
};
export type IndexStatus = {
  state: "not_indexed" | "queued" | "scanning" | "ready" | "degraded" | "unavailable";
  generation: string; observedEntries: string; completedDirectories: string;
  degradedDirectories: string; updatedAt: string | null; lastCompletedAt: string | null;
};
export type DirectoryPage = {
  entries: EntrySummary[]; nextCursor: string | null; previousCursor: string | null;
  directoryId: string; directoryVersion: string; contractVersion: 1;
  indexStatus: IndexStatus;
};
export type EntryDetails = EntrySummary & {
  parentId: string | null;
  ancestors: {id: string; displayName: string}[];
  ancestorsTruncated: boolean;
};
export type BrowseInput = {
  namespace: string; rootId: string; rootEpoch: number; parentId: string | null;
  filters: FileFilters; sort: "name" | "modified" | "size"; order: "asc" | "desc";
  cursor?: string | null;
};
```

Whole-number sizes, nanosecond timestamps, counters, and versions are decimal strings; the frontend uses `BigInt` for comparisons and never coerces them into an unsafe `number`. Root-shell epoch encoding is unchanged; new cursor contexts use server-side integers. Details return at most 64 nearest ancestor labels with an explicit truncation flag and a usable parent link, never an unbounded complete path.

### Verification rhythm

Each task has a red/green cycle, local review, and a scoped commit. Run the task's named command before its implementation; a failure must exercise the intended missing behavior, not an unrelated environment error. Save sanitized red/green command results with the task ledger. On any actual failure, investigate before changing code.

Task 1 adds `--test-target` to the isolated runner; until that commit, use its existing whole-backend command. Focused selectors stay beneath the chosen `backend/tests` or `tests/deployment` tree and may include a pytest `::test_name`; no raw pytest-option passthrough is added. Every backend selection still runs Django and migration drift checks. Canonical commands stay unchanged:

```bash
make verify
make verify-compose
make test-e2e
git diff --check
git status --short
```

Do not run the browser harness concurrently with itself. Final clean-checkout verification uses an exact committed revision with fresh frozen installs; no `.env`, manifest, secrets, or test database is copied from another checkout.

## Task 1: Lossless filename and ordering domain

**Files:** Create `backend/aegis_apps/catalog/__init__.py`, `backend/aegis_apps/catalog/domain.py`, `backend/aegis_apps/catalog/names.py`, `backend/tests/unit/catalog/test_names.py`, `backend/tests/unit/common/test_verify_targets.py`. Modify `scripts/verify.py` and `docs/development.md`.

**Interfaces:** Consumes Python filesystem byte semantics. Produces the domain types above and `source_name(raw: bytes) -> SourceName`; `SORT_KEY_VERSION = 1`. The root anchor's empty name is created by Task 2, never accepted by this child-name function.

- [x] **Step 1: Write failing name and runner tests.**

```python
import pytest
from aegis_apps.catalog.names import source_name

@pytest.mark.parametrize("raw", [b"", b".", b"..", b"a/b", b"a\x00b"])
def test_invalid_child_names_are_rejected(raw: bytes) -> None:
    with pytest.raises(ValueError, match="invalid source name"):
        source_name(raw)

def test_raw_identity_survives_display_escaping_and_normalization() -> None:
    irregular = source_name(b"photo-\xff.jpg")
    assert irregular.raw == b"photo-\xff.jpg"
    assert "\\xff" in irregular.display
    assert irregular.type_hint == "jpg"
    assert source_name("É.TXT".encode()).order_key == source_name("e\u0301.txt".encode()).order_key
    assert source_name(b"A.txt").raw != source_name(b"a.txt").raw
```

Add property tests over non-NUL/non-slash byte components, literal backslashes, controls/bidi characters, normalization collisions, case folding, no executable HTML, and maximum Linux component lengths. Runner tests use `runpy.run_path`, injected argument vectors and a captured `run()` to reject `../`, absolute paths, symlink escapes, options masquerading as paths, empty selectors, and cross-mode test paths before creating a database.

- [x] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend`. Expect collection failure for the absent catalog module or explicit missing target-parser assertions; keep the unrelated foundation tests passing.

- [x] **Step 3: Implement the domain, escaping, and focused runner.**

```python
import unicodedata
from .domain import SourceName

SORT_KEY_VERSION = 1

def source_name(raw: bytes) -> SourceName:
    if type(raw) is not bytes or not 1 <= len(raw) <= 255:
        raise ValueError("invalid source name")
    if raw in (b".", b"..") or b"/" in raw or b"\0" in raw:
        raise ValueError("invalid source name")
    decoded = raw.decode("utf-8", "surrogateescape")
    display = "".join(
        f"\\x{ord(c) - 0xDC00:02x}" if 0xDC80 <= ord(c) <= 0xDCFF else
        "\\\\" if c == "\\" else
        f"\\U{ord(c):08x}" if unicodedata.category(c).startswith("C") and ord(c) > 0xFFFF else
        f"\\u{ord(c):04x}" if unicodedata.category(c).startswith("C") else c
        for c in decoded
    )
    key = unicodedata.normalize("NFC", display.casefold()).encode("utf-8")
    if len(key) > 2048:
        raise ValueError("invalid source name")
    suffix = raw.rpartition(b".")[2].lower() if b"." in raw else b""
    hint = suffix.decode("ascii") if 1 <= len(suffix) <= 16 and suffix.isascii() and suffix.isalnum() else None
    return SourceName(raw, display, key, hint)
```

Prove the maximum encoded sort key is below PostgreSQL's chosen index-tuple budget using the supported 255-byte component domain, including case-fold expansion and escaping; enforce a 2,048-byte defensive stored-key ceiling and fail visibly if it is exceeded, never truncate. Extension hints deliberately accept only ASCII alphanumerics. Escape representation remains separate from raw uniqueness. Use fixed-width `\uXXXX` for BMP controls and `\UXXXXXXXX` for supplementary controls: U+E0001 must not display like U+E000 followed by literal `1`. Normalize the case-folded key with NFC as shown. This escaping clarification was added during Task 1 review before any catalog or cursor was deployed.

Use `argparse` in the runner with `mode` choices unchanged and repeated `--test-target`; resolve the filename portion beneath the allowed test tree with `Path.resolve().is_relative_to()`, require an existing file/directory, validate identifier-only `::` selectors, then construct the fixed pytest argv list. Preserve the sanitized environment, private password file, owned-container checks and final cleanup.

- [x] **Step 4: Verify green and regressions.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/unit/catalog --test-target backend/tests/unit/common/test_verify_targets.py`, `uv run --locked ruff check backend scripts tests`, and `uv run --locked mypy backend`. Run the existing deployment verification-runner test through its isolated `deployment --test-target` mode to prove cleanup on failure.

- [x] **Step 5: Review and commit.** Inspect exact changed files and `git diff --check`; update Task 1's ledger evidence, then `git add` only this task's paths and commit `feat: define lossless catalog names and ordering`. Push `origin main` after the scoped checks pass.

## Task 2: Catalog schema and immutable source boundary

**Files:** Create `backend/aegis_apps/catalog/apps.py`, `backend/aegis_apps/catalog/models.py`, `backend/aegis_apps/catalog/migrations/__init__.py`, `backend/aegis_apps/catalog/migrations/0001_initial.py`, `backend/tests/conftest.py`, `backend/tests/integration/catalog/test_models.py`, `conftest.py`, `tests/__init__.py`, and `tests/support/{__init__,database_roles}.py`. Modify `backend/aegis/settings/base.py`, `pyproject.toml`, `backend/aegis_apps/common/database_privileges.py`, `backend/tests/unit/common/test_database_privileges.py`, and `tests/deployment/test_database_roles.py`.

**Interfaces:** Consumes Task 1's names/enums. Produces `CatalogEntry`, with protected root/source/logical parent relationships, and test fixtures `catalog_root` and `entry_factory`. No production insertion route exists yet; migrator/test fixtures may seed records, runtime roles cannot mutate them directly.

- [ ] **Step 1: Write failing database invariants.**

```python
import pytest
from django.db import IntegrityError, transaction
from aegis_apps.catalog.models import CatalogEntry

pytestmark = pytest.mark.django_db(transaction=True)

def test_source_uniqueness_does_not_use_normalized_names(catalog_root, entry_factory):
    first = entry_factory(root=catalog_root, raw=b"A.txt")
    second = entry_factory(root=catalog_root, raw=b"a.txt")
    assert first.pk != second.pk
    assert first.name_key == second.name_key
    with pytest.raises(IntegrityError), transaction.atomic():
        entry_factory(root=catalog_root, raw=b"A.txt")

def test_catalog_delete_is_disabled(catalog_root, entry_factory):
    entry = entry_factory(root=catalog_root, raw=b"keep.txt")
    with pytest.raises(PermissionError):
        entry.delete()
    with pytest.raises(PermissionError):
        CatalogEntry.objects.filter(pk=entry.pk).delete()
```

Cover cross-root source/logical parents, self-parenting, duplicate anchors, non-directory anchor, null/empty child name, invalid states, negative sizes/revisions, full-precision timestamps/inodes and protected root deletion. Check direct SQL constraints, not only `full_clean`. Test web SELECT and denial of runtime INSERT/UPDATE/DELETE/TRUNCATE; operations/media get no new catalog grant.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/integration/catalog/test_models.py` and capture the absent-model failure.

- [ ] **Step 3: Add the model, migrations, and exact privilege map.**

```python
class CatalogEntryQuerySet(models.QuerySet["CatalogEntry"]):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise PermissionError("catalog deletion is disabled")

class CatalogEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    root = models.ForeignKey(Root, on_delete=models.PROTECT)
    source_parent = models.ForeignKey("self", null=True, on_delete=models.PROTECT,
                                      related_name="source_children")
    logical_parent = models.ForeignKey("self", null=True, on_delete=models.PROTECT,
                                       related_name="logical_children")
    raw_name = models.BinaryField(max_length=255)
    display_name = models.TextField()
    name_key = models.BinaryField(max_length=2048)
    logical_name = models.TextField(null=True)
    kind = models.CharField(max_length=16)
    type_hint = models.CharField(max_length=16, null=True)
    source_state = models.CharField(max_length=16, default="present")
    size = models.DecimalField(max_digits=20, decimal_places=0, null=True)
    mtime_ns = models.DecimalField(max_digits=30, decimal_places=0, null=True)
    ctime_ns = models.DecimalField(max_digits=30, decimal_places=0, null=True)
    device = models.DecimalField(max_digits=20, decimal_places=0, null=True)
    inode = models.DecimalField(max_digits=20, decimal_places=0, null=True)
    source_revision = models.PositiveBigIntegerField(default=0)
    catalog_version = models.PositiveBigIntegerField(default=0)
    children_version = models.PositiveBigIntegerField(default=0)
    observation_epoch = models.PositiveBigIntegerField(default=0)
    seen_generation = models.PositiveBigIntegerField(default=0)
    seen_attempt = models.PositiveBigIntegerField(default=0)
    observed_at = models.DateTimeField(null=True)
    objects = CatalogEntryQuerySet.as_manager()

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise PermissionError("catalog deletion is disabled")
```

Import `uuid`, Django `models`, and `Root` in that file. Define the migration's exact checks from the red tests: anchor iff `source_parent_id IS NULL`, with empty raw name and directory kind; child raw names nonempty, without NUL/slash/dot/dot-dot; one anchor per root; `(root_id, source_parent_id, raw_name)` unique for children; root+ID unique for composite references. Use `DEFERRABLE INITIALLY DEFERRED` composite foreign keys `(root_id, source_parent_id)` and `(root_id, logical_parent_id)` to `(root_id, id)` so SQL cannot introduce a cross-root parent. Foreign-key deletion action is restrictive, never cascading. Add self-parent checks and reject ancestry cycles in the future logical mutation boundary; scanner insertion accepts only a verified existing directory parent, so it cannot create source cycles.

Implement the fixtures in `backend/tests/conftest.py`: `catalog_root` creates one active `Root` and synthetic anchor; `entry_factory` takes `root`, `raw`, optional `parent`, `kind`, `state`, `size`, `mtime_ns`, defaults parent to that root's anchor, calls `source_name`, and creates matching source/logical parents and precise numeric values. It accepts explicitly named overrides for the model's version/organization fields; never default to an operator root or filesystem path.

Install the catalog app, generated migration, and its Ruff migration-file exception. Add actual column order to `MANAGED_TABLE_COLUMNS`; web/indexer initially receive SELECT only on the new table, no new identity-table access. No new sequence is needed because IDs are UUIDs. Do not register the model in Django admin.

Move the existing `RoleDatabase`, role-login/setup/ownership/teardown helpers and module-scoped `role_database` fixture out of the large deployment test into `tests/support/database_roles.py`; expose the same fixture through root `conftest.py`. Preserve the disposable `test_` database, PostgreSQL 18, and no-pre-existing-roles guards. The existing deployment test imports the shared helper instead of maintaining a second implementation. Add `RoleDatabase.as_django_role(role: str)` as a context-manager wrapper around its current `_django_login` helper, so backend scan/binding fixtures exercise real logins too; never make maintenance functions accept `postgres` merely to simplify tests.

Snapshot existing managed function definitions/owners as well as table/sequence owners before role setup. Restore pre-existing functions after the module and drop only functions created by that fixture. This is required when Task 6's migration-owned trigger depends on a managed function: teardown must not drop that function or cascade away its trigger. Only test-owned temporary roles/resources are removed.

- [ ] **Step 4: Verify green.** Run the focused model tests, `backend --test-target backend/tests/unit/common/test_database_privileges.py`, and `deployment --test-target tests/deployment/test_database_roles.py` through `scripts/verify.py`. Run Ruff/mypy and the runner's migration-drift check.

- [ ] **Step 5: Review and commit.** Record schema/role evidence, inspect the generated SQL and diff, and commit `feat: add protected source and logical catalog schema`; push `origin main`.

## Task 3: Deployment-bound maintenance state

**Files:** Create `backend/aegis_apps/indexing/{__init__,apps,config,models,binding}.py`, `backend/aegis_apps/indexing/migrations/{__init__,0001_initial}.py`, `backend/tests/unit/indexing/test_config.py`, `backend/tests/integration/indexing/test_binding.py`. Modify `backend/aegis/settings/base.py`, `backend/aegis_apps/common/{database_privileges.py,management/commands/deploy_database.py}`, `backend/aegisctl/mounts.py`, `backend/tests/unit/aegisctl/test_render.py`, `backend/tests/unit/common/test_deploy_database.py`, `tests/deployment/{test_database_roles,test_rendered_mounts}.py`, `compose.yaml`, `.env.example`, and `pyproject.toml`.

**Interfaces:** Produces `ScanPolicy`, `IndexDeployment`, `RootIndexState`, `ScanRun`, `DirectoryWork`, `ScanRequest`, and `install_index_binding(manifest: MountManifest | None, policy: ScanPolicy) -> None`. Installation requires the actual migrator login. Indexer reads the binding but cannot replace it.

- [ ] **Step 1: Write failing configuration/binding tests.**

```python
import pytest
from aegis_apps.indexing.config import ScanPolicy

def test_scan_defaults_and_bounds() -> None:
    assert ScanPolicy.from_environment({}) == ScanPolicy(3600, 120, 500, 2)
    for values in (
        {"AEGIS_SCAN_INTERVAL_SECONDS": "0"},
        {"AEGIS_SCAN_IDLE_TIMEOUT_SECONDS": "3601"},
        {"AEGIS_SCAN_BATCH_RECORDS": "2001"},
        {"AEGIS_SCAN_READERS": "0"},
    ):
        with pytest.raises(ValueError, match="invalid scan policy"):
            ScanPolicy.from_environment(values)
```

Binding integration tests create a protected manifest fixture, install it under the migrator test login, repeat installation without advancing epochs, then replace the manifest/policy and assert an epoch advance. Test web/indexer binding writes are denied, an absent manifest disables indexing without erasing catalog rows, and generated migrate mounts contain only the manifest—not any source root. Upgrade tests preserve existing accounts, roots, grants, jobs, and catalog rows.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/unit/indexing/test_config.py --test-target backend/tests/integration/indexing/test_binding.py`.

- [ ] **Step 3: Implement persisted binding and durable models.**

```python
from collections.abc import Mapping
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class ScanPolicy:
    interval_seconds: int
    idle_timeout_seconds: int
    batch_records: int
    readers: int

    @classmethod
    def from_environment(cls, values: Mapping[str, str]) -> "ScanPolicy":
        limits = (
            ("AEGIS_SCAN_INTERVAL_SECONDS", 3600, 60, 604800),
            ("AEGIS_SCAN_IDLE_TIMEOUT_SECONDS", 120, 30, 3600),
            ("AEGIS_SCAN_BATCH_RECORDS", 500, 100, 2000),
            ("AEGIS_SCAN_READERS", 2, 1, 4),
        )
        result: list[int] = []
        for name, default, lower, upper in limits:
            raw = values.get(name, str(default))
            if not raw.isascii() or not raw.isdecimal() or len(raw) > 6:
                raise ValueError("invalid scan policy")
            value = int(raw)
            if not lower <= value <= upper:
                raise ValueError("invalid scan policy")
            result.append(value)
        return cls(*result)
```

Use these exact model fields, UUIDs except the singleton/one-to-one keys, restrictive foreign keys, checked states/counters, and no admin registrations:

| Model | Fields beyond its key | Constraints/indexes |
| --- | --- | --- |
| `IndexDeployment` (`id=1`) | `epoch`, `manifest_identity`, sorted `slot_ids` JSON, four `ScanPolicy` integer fields, `updated_at` | Singleton check; bounded policy; migrator-only writes; at most the existing manifest `MAX_SLOTS` |
| `RootIndexState` (`root_id` primary key) | `binding_epoch`, `policy_epoch`, `reconciliation_epoch`, `next_generation`, `due_at`, `active_run_id` nullable, `rescan_requested`, `status`, `observed_entries`, `completed_directories`, `degraded_directories`, `updated_at`, `last_completed_at` | Status from wire contract; nonnegative counters; due-time index; protected root/run references |
| `ScanRun` | `root_id`, `binding_epoch`, `policy_epoch`, `root_epoch`, `manifest_identity`, `generation`, `start_epoch`, `state`, `started_at`, `settled_at` nullable | `(root_id,generation)` unique; one queued/running run per root; `queued/running/complete/degraded/fenced` |
| `DirectoryWork` | `run_id`, `directory_id`, `parent_revision`, `state`, `attempt`, `lease_owner`, `lease_expires_at`, `available_at`, `last_batch_sequence`, `observed_count`, `eof_identity` nullable JSON, `error_code`, `updated_at` | `(run_id,directory_id)` unique; bounded claim index `(run_id,state,available_at,id)`; pending/reading/finalizing/complete/degraded; attempt and batch sequence monotonic |
| `ScanRequest` | `root_id`, `actor_id`, `client_request_id`, `run_id`, `created_at` | Unique `(actor_id,client_request_id)`; replay with another root rejects; request IDs follow existing 8–64 character request-ID contract |

The `IndexDeployment` row is the authoritative current configuration, not whichever worker heartbeat arrived last. `deploy_database` loads/validates the mounted manifest and policy, then installs this row in its existing durable deployment transaction. Same configuration is idempotent; changes increment its epoch and fence old runs. A missing manifest records no active slots. Preserve existing schema-comment keys and privilege synchronization; no background worker may roll the binding back to an earlier manifest.

On a changed existing binding, advance affected root and reachable user authorization epochs using the foundation's ordered root/user locking and post-commit cache invalidation; this invalidates sessions/cursors tied to the previous source configuration. Preserve accounts/grants/content rows. Resolve affected users through direct/group grants under the existing membership-discovery discipline. Initial installation and exact repeats are separately tested; upgrade snapshots compare stable identity/grant fields and assert epochs never decrease. Configuration metadata is read without acquiring a later conflicting lock in an already root-locked browse transaction.

Extend the generated mount override to pass the protected manifest file and digest to `migrate`; it receives no original mounts. Config defaults appear in `.env.example` and the shared backend environment. Existing active roots are discovered by scheduling after migration; roots activated later become eligible without a fake user or schema migration. Root-specific state is created lazily only for active roots in the current binding, not during browse requests.

Add exact table columns and SELECT grants for web/indexer; no runtime DML on these tables. Add `slot_id` SELECT only for indexer in the existing roots column map. `ScanRequest` is web-readable only; indexer does not gain access to actors, passwords, sessions, or group membership. Migrator installation is the only direct binding writer. Deferred `RootIndexState.active_run` FK resolves the schema cycle with `ScanRun`.

- [ ] **Step 4: Verify green.** Run the focused config/binding/render/deploy tests, the actual database-role suite, Ruff/mypy, and generated Compose validation. Compare generated service mount sets with Phase 1; only migrator configuration-file access may expand.

- [ ] **Step 5: Review and commit.** Record upgrade/mount/grant evidence; commit `feat: bind indexing policy and checkpoints to deployments` and push `origin main`.

## Task 4: Scheduling, lease, and manual-rescan authority

**Files:** Create `backend/aegis_apps/indexing/{database,scheduling,services}.py`, `backend/aegis_apps/indexing/sql/{schedule,lease}.sql`, `backend/tests/integration/indexing/{test_scheduling,test_leases,test_rescan}.py`. Modify `backend/aegis_apps/common/database_privileges.py` and `tests/deployment/test_database_roles.py`.

**Interfaces:** Produces `ScanLease`, `schedule_root_scan(root_id: UUID, worker_id: str, manifest_identity: str) -> UUID | None`, `claim_directory(run_id: UUID, worker_id: str) -> ScanLease | None`, `renew_directory(lease: ScanLease) -> bool`, and `request_root_scan(actor: User, root_id: UUID, request_id: str) -> UUID`. Initial directory leases last 60 seconds; renewal interval is at most 15 seconds. Database time is authoritative.

- [ ] **Step 1: Write failing actual-login tests.** Extend the existing `RoleDatabase` fixture and helpers in `tests/deployment/test_database_roles.py`; do not duplicate its credential/role teardown. Define `_create_scan_fixture()` there to seed an active bound root/anchor, publish a current indexer heartbeat, and return `(root, worker_id, manifest_digest)`.

```python
def test_only_indexer_can_schedule_and_renew(role_database):
    root, worker_id, digest = _create_scan_fixture()
    with role_database.connect("aegis_web") as web:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            web.execute("SELECT public.aegis_schedule_root_scan(%s,%s,%s)",
                        (root.pk, worker_id, digest))
    with role_database.connect("aegis_indexer") as indexer:
        run_id = indexer.execute(
            "SELECT public.aegis_schedule_root_scan(%s,%s,%s)",
            (root.pk, worker_id, digest),
        ).fetchone()[0]
        lease = indexer.execute(
            "SELECT public.aegis_claim_scan_directory(%s,%s)", (run_id, worker_id),
        ).fetchone()[0]
        assert indexer.execute(
            "SELECT public.aegis_renew_scan_directory(%s)", (Jsonb(lease),),
        ).fetchone()[0] is True
```

Test expired/future/malformed leases, stale binding/root/policy epochs, wrong worker, inactive root, missing slot, concurrent scheduling, root-admin versus browse-only grants, ungranted superuser, account deactivation, audit rollback, idempotent replay, and cross-root replay mismatch. A logged-out requester must not stop an already administratively configured periodic scan. Existing actorless operation rejection tests remain unchanged.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py deployment --test-target tests/deployment/test_database_roles.py::test_only_indexer_can_schedule_and_renew` and the new focused indexing backend tests; expect absent SQL/service failures.

- [ ] **Step 3: Implement fixed SQL functions and typed wrappers.** Each security-definer function uses PL/pgSQL, an empty search path, actual `session_user` checks, a strict field/size allowlist for lease JSON, parameter validation before casting, fixed error codes, and schema-qualified objects. Register its exact signature and identity arguments in the existing verification maps; revoke public EXECUTE in the installation transaction.

```python
# database.py — the generic helper is private to this fixed registry.
from django.db import connection
from psycopg.types.json import Jsonb

def renew_directory(lease: ScanLease) -> bool:
    payload = {
        field: str(value) if isinstance(value, UUID) else value
        for field, value in vars_from_lease(lease).items()
    }
    with connection.cursor() as cursor:
        cursor.execute("SELECT public.aegis_renew_scan_directory(%s)", [Jsonb(payload)])
        row = cursor.fetchone()
    return row is not None and row[0] is True
```

Define `vars_from_lease(lease: ScanLease) -> dict[str, object]` with `dataclasses.fields` and `getattr`; slots dataclasses do not have `__dict__`:

```python
from dataclasses import fields

def vars_from_lease(lease: ScanLease) -> dict[str, object]:
    return {field.name: getattr(lease, field.name) for field in fields(lease)}
```

`parse_scan_lease(value: object) -> ScanLease` rejects unknown/missing fields, noncanonical UUIDs, booleans as integers, counters outside signed bigint, worker-ID/digest violations, and payloads over 8 KiB. Wrappers translate SQL `22023` to safe validation errors, `42501` to denied authority, and stale attempts to false; never log raw JSON or database exception details. Include the fixed SQL resources in wheel/image packaging and test that a clean built image can install them; no runtime download or absolute developer path is allowed.

Lock order is deployment binding in shared mode, root, user only for actor-bound requests, root-index state, scan run, then directory work and directory entry. Schedule/claim must not lock a work row first and then seek its root. Use bounded candidate discovery followed by root-first locking and rechecking; skip busy roots and schedule by due time/last service, so one large root does not monopolize the reader pool. Claim transitions pending/expired work to reading, increments attempt, resets batch sequence and EOF state, and captures the current directory revision. `RootIndexState` serializes one reader per root; a fresh lease for one directory excludes another live directory lease on that root.

Create the synthetic anchor once under its unique root constraint. Runs capture binding/root/policy/reconciliation epochs, enqueue only directory work, and coalesce additional demand. Never create one job per regular file. Schedule the next periodic run after completion/degradation, not from the old start time. Bound retries with backoff rather than a busy loop.

Manual requests take live root/user locks, compare the supplied user epoch and active state, and compute the existing additive permission mask from direct/group grants. Require `ROOT_ADMIN` even for a platform superuser. Record `index.scan.requested` with actor/root/run/request IDs in the same transaction as the idempotency record; no filenames or paths. Replays return the original run ID only after current authorization. Limit new manual requests to 20 per user/root per minute under the same lock; return a safe retry interval, without blocking periodic maintenance.

- [ ] **Step 4: Verify green.** Run all new scheduling/lease/rescan tests and the complete actual-role suite. Add two-connection barriers proving old attempts cannot renew after takeover and binding/root revocation cannot race a valid commit. Keep the original operations authorization/locking suite green.

- [ ] **Step 5: Review and commit.** Inspect SQL caller checks, lock order and grant maps; record evidence and commit `feat: enforce database-scoped scan scheduling and leases`; push `origin main`.

## Task 5: Read-only descriptor-relative directory reader

**Files:** Create `backend/aegis_apps/indexing/{reader,protocol}.py`, `backend/tests/unit/indexing/{test_reader,test_protocol,test_reader_preservation}.py`, and `tests/deployment/test_index_reader.py`. This task does not enable a worker loop.

**Interfaces:** Produces `read_directory(root_fd: int, components: tuple[bytes, ...], batch_records: int) -> Iterator[ReaderMessage]`, `encode_message(message: ReaderMessage) -> bytes`, and `decode_message(raw: bytes) -> ReaderMessage`. `ReaderMessage` is a tagged union of `ReaderBatch(sequence, observations)`, `ReaderComplete(identity)`, and `ReaderFailure(code)` frozen dataclasses in `protocol.py`; codes are `source_unavailable`, `identity_changed`, `permission_denied`, `unsupported_entry`, `reader_timeout`, or `reader_protocol_error`.

- [ ] **Step 1: Write failing reader and preservation tests.**

```python
import os
from aegis_apps.indexing.reader import read_directory
from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete

def test_reader_never_follows_symlinks_or_opens_file_content(tmp_path):
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / "keep.txt").write_bytes(b"preserve exactly")
    (root / "link").symlink_to(tmp_path / "outside")
    (tmp_path / "outside").mkdir()
    before = (root / "keep.txt").stat()
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        messages = list(read_directory(descriptor, (), 100))
    finally:
        os.close(descriptor)
    entries = [entry for message in messages if isinstance(message, ReaderBatch)
               for entry in message.observations]
    assert {entry.name.raw for entry in entries} == {b"keep.txt", b"link"}
    assert next(entry for entry in entries if entry.name.raw == b"link").kind == "symlink"
    assert isinstance(messages[-1], ReaderComplete)
    after = (root / "keep.txt").stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
    assert (root / "keep.txt").read_bytes() == b"preserve exactly"
```

Monkeypatch forbidden content-open, `unlink`, `rename`, `replace`, `remove`, `chmod`, and write flags inside the reader tests; fixture creation occurs before guards. Add Linux FIFO/socket/device metadata tests, dangling/ancestor symlinks, 50K synthetic iteration without `list(scandir)`, byte-irregular names, per-entry permission failures, a replaced directory, nested/bind mounts with the same device ID, and bounded malformed IPC frames. Deployment tests run with the actual read-only bind and dropped capabilities.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/unit/indexing/test_reader.py --test-target backend/tests/unit/indexing/test_protocol.py --test-target backend/tests/unit/indexing/test_reader_preservation.py`.

- [ ] **Step 3: Implement safe enumeration and framing.**

```python
def open_child_directory(parent_fd: int, raw: bytes) -> int:
    source_name(raw)
    return os.open(raw, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=parent_fd)

def directory_identity(descriptor: int) -> DirectoryIdentity:
    value = os.fstat(descriptor)
    if not stat.S_ISDIR(value.st_mode):
        raise ValueError("directory identity unavailable")
    return DirectoryIdentity(value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)
```

`reader.py` imports `os`, `stat`, the Task 1 name/domain types, and the protocol dataclasses. Walk one no-follow directory component at a time, closing replaced descriptors in `finally` blocks; never join an absolute caller path or use `DirEntry.path` as an authority. The configured root descriptor comes only from attested `ManifestSlot.container_path`. On Linux compare each opened descriptor's mount ID from bounded `/proc/self/fdinfo` to the root descriptor's mount ID, not only `st_dev`; revalidate bounded mount metadata before and after the pass. Failure to establish identity or topology fails closed. Keep the descriptor identity captured from the container; do not equate macOS host inode/device numbers to container ones.

Use `os.scandir(fd)` with `entry.stat(follow_symlinks=False)` and `os.fsencode(entry.name)`. Yield before the next item would cross the configured record count or the fixed 1 MiB encoded-byte bound. A single invalid/oversized record degrades the pass rather than allocating without bounds. Emit missing metadata as inaccessible, with nullable stat fields; never infer whole-subtree absence from `OSError`. Classify symlinks/special entries without opening them. Successful EOF includes before/after directory identity stability; changed identity produces failure, not `ReaderComplete`.

Reopen/revalidate the component chain beneath the root before accepting EOF, comparing it with the held directory descriptor; a directory moved/replaced during the pass cannot pass based only on the old open FD's unchanged inode. Any per-entry metadata failure changes the terminal message to a safe degraded failure after its observations, not a successful completion. The contract remains eventual consistency under trusted external writers, not an atomic filesystem snapshot.

Protocol frames are a four-byte unsigned length followed by strict UTF-8 JSON; reject a length above 1 MiB before allocation. Encode raw names as canonical base64, integers losslessly, and only the fixed tags/fields. No pickle, shell command, exception text, path string, or arbitrary Python object crosses IPC. Use two frame credits per reader, returning credit only after the supervisor commits or discards a batch; this bounds in-flight data including pipe buffering. A sequence starts at one and increases; the supervisor detects duplicate/reordered frames. After success/failure, any additional frame is a protocol error.

- [ ] **Step 4: Verify green.** Run reader/protocol/preservation tests, Ruff/mypy, and `deployment --test-target tests/deployment/test_index_reader.py`. Confirm reads do not mutate bytes, names, size or modification time; qualify host access-time behavior as the approved spec does.

- [ ] **Step 5: Review and commit.** Record FD-leak, IPC-size and actual-mount evidence; commit `feat: add bounded read-only directory enumeration`; push `origin main`.

## Task 6: Atomic observations and safe directory finalization

**Files:** Create `backend/aegis_apps/indexing/checkpoints.py`, `backend/aegis_apps/indexing/sql/{observations,finalize}.sql`, `backend/aegis_apps/indexing/migrations/0002_commit_fence.py`, `backend/tests/integration/indexing/{test_observations,test_finalization,test_recovery}.py`. Modify `backend/aegis_apps/indexing/{models.py,sql/lease.sql}`, `backend/tests/conftest.py`, `backend/aegis_apps/common/database_privileges.py`, and `tests/deployment/test_database_roles.py`.

**Interfaces:** Produces `record_batch(lease: ScanLease, batch: ReaderBatch) -> BatchResult`, `seal_directory(lease: ScanLease, complete: ReaderComplete) -> bool`, `finalize_directory(lease: ScanLease, limit: int = 500) -> FinalizeResult`, and `fail_directory(lease: ScanLease, code: str) -> bool`. Define frozen `BatchResult(observed: int, inserted: int, changed: int)` and `FinalizeResult(affected: int, complete: bool)` in `checkpoints.py`. All results describe catalog metadata, not filesystem writes.

- [ ] **Step 1: Write failing preservation and stale-commit tests.**

```python
def test_failed_directory_keeps_unseen_locations_present(scan_fixture, entry_factory):
    lease = scan_fixture.claim()
    original = entry_factory(root=scan_fixture.root, raw=b"unseen.jpg")
    assert scan_fixture.fail(lease, "permission_denied") is True
    original.refresh_from_db()
    assert original.source_state == "present"
    assert scan_fixture.finalize(lease).complete is False

def test_retry_cannot_accept_the_old_attempt(scan_fixture):
    old = scan_fixture.claim()
    scan_fixture.expire(old)
    current = scan_fixture.claim()
    assert current.attempt > old.attempt
    assert scan_fixture.observe(old, b"old.txt") is None
    assert scan_fixture.observe(current, b"current.txt") is not None
```

Define `scan_fixture` in `backend/tests/conftest.py` for these tests: it consumes the shared `role_database` fixture, installs binding as migrator, calls Task 4 wrappers through `as_django_role("aegis_indexer")`, exposes `root`, builds bounded `ReaderBatch` observations, and expires leases by migrator-owned SQL. No production time injection or bypass is added. The direct SQL suite separately performs these transitions through `RoleDatabase` connections.

Add cases for partial committed batches then crash/restart, duplicate batch sequence/hash, mismatched replay, newer observation epoch, logical parent/name overrides, changed source revisions, hard links as separate entries, parent replacement, successful empty directory, interrupted finalization, descendant direct-URL availability, and foreign-root batch data. A two-connection test pauses a transaction past lease expiry, attempts COMMIT, and proves it cannot publish observations after its fence becomes invalid.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/integration/indexing/test_observations.py --test-target backend/tests/integration/indexing/test_finalization.py --test-target backend/tests/integration/indexing/test_recovery.py`.

- [ ] **Step 3: Implement fenced upserts and bounded reconciliation.** The observation function validates the entire batch before writing: fixed keys, at most configured records, serialized payload at most 1 MiB, valid source names/kinds/states, integer domains, consistent null metadata, and a sequence/hash bound to the current attempt. Store the most recent sequence/hash on `DirectoryWork`; exact replay is an idempotent acknowledgement, a changed payload with that sequence rejects, and a gap rejects. Extend Task 3's model with `last_batch_hash` in this migration and the exact column allowlist.

```sql
-- Core unseen-row predicate, inside the root-first locked finalization function.
WITH candidates AS (
    SELECT entry.id
      FROM public.catalog_catalogentry AS entry
     WHERE entry.root_id = root_uuid
       AND entry.source_parent_id = directory_uuid
       AND (entry.seen_generation, entry.seen_attempt) <> (run_generation, work_attempt)
       AND entry.observation_epoch <= captured_epoch
       AND entry.source_state <> 'missing'
     ORDER BY entry.id
     LIMIT batch_limit
     FOR UPDATE
)
UPDATE public.catalog_catalogentry AS entry
   SET source_state = 'missing', catalog_version = entry.catalog_version + 1
  FROM candidates
 WHERE entry.id = candidates.id
   AND entry.observation_epoch <= captured_epoch;
```

The PL/pgSQL function declares `root_uuid`, `directory_uuid`, `run_generation`, `work_attempt`, and `captured_epoch` from the validated, locked lease/run/work rows—not batch-supplied locations—and validates `batch_limit` as 1–500. It can execute this predicate only after a successful, stable EOF sealed that exact attempt. Check missing candidates again under the same lock before recording completion; no negative conclusion is drawn from a failed scan.

Bulk `INSERT ... ON CONFLICT` on exact source location updates only observed-source columns and scan markers. Compare nullable source identity/size/mtime/ctime with `IS DISTINCT FROM`; increment `source_revision` and `catalog_version` only on change, retain opaque entry ID at an existing location, and invalidate stale descendants by their ancestor's revision/state. Do not overwrite `logical_parent_id` or `logical_name`; insertion sets the initial logical parent, later source observations do not clear overrides. Observation epochs newer than the captured run epoch win. Enqueue child-directory work in the same transaction, uniquely per run/directory. Update bounded progress deltas, not a whole-root recount; replay must not increment counters twice.

An inaccessible observation updates availability and scan markers but retains last-known source size/timestamps/identity instead of replacing them with null guesses. A pass containing metadata failures settles degraded, not as a successful EOF eligible to mark unseen entries missing.

Use one root/directory coordination boundary for upsert, sealing, finalization, and future event reconciliation. All functions verify binding, root activity/epoch, run/policy epoch, work owner/attempt, current directory revision and database lease time after locking. Add a deferred constraint trigger on each changed `DirectoryWork` record to recheck the live fence at transaction commit, including a retained lease on a terminal transition. Every batch updates its work record atomically, so this is one commit check per batch, not one per catalog entry. Runtime direct table DML remains denied. Register the internal `aegis_guard_directory_commit()` PL/pgSQL trigger function for ownership/search-path verification, with no runtime/public EXECUTE grant. No caller-controlled session variable alone authorizes a write.

On expiry/root/policy change, discard stale work and return a safe false result or rollback with a fixed code. EOF failure, mount loss or per-entry errors cannot mark unseen locations missing. Completed work survives process restart; unfinished work restarts at entry one with a fresh attempt and no saved `scandir` offset. Retain missing records; no physical or catalog-delete job is produced.

- [ ] **Step 4: Verify green.** Run all new checkpoint/recovery tests and actual-login boundary tests. Exercise finalization over more than 500 missing entries with a newer-epoch survivor and a crash between batches. Run existing operations immutability tests, Ruff and mypy.

- [ ] **Step 5: Review and commit.** Record retry, rollback, original-preservation, and commit-fence results; commit `feat: reconcile scan observations with transactional fences`; push `origin main`.

## Task 7: Supervised scanner and worker liveness

**Files:** Create `backend/aegis_apps/indexing/{supervisor,runner}.py`, `backend/tests/unit/indexing/test_supervisor.py`, `backend/tests/integration/indexing/test_runner.py`, and `tests/deployment/test_indexer_runtime.py`. Modify `backend/aegis_apps/operations/management/commands/run_role.py` at dispatch/lifecycle boundaries; extend `backend/aegis_apps/operations/serializers.py`, the heartbeat SQL in `backend/aegis_apps/common/database_privileges.py`, and existing role/heartbeat tests for the explicit counter keys below.

**Interfaces:** Produces `ScanSupervisor`, with `start(lease: ScanLease)`, `tick()`, `stop()`, and `has_unreaped_reader(root_id: UUID) -> bool`; `run_indexer(identity: WorkerIdentity, stop_requested: Callable[[], bool]) -> None`. The existing startup path still verifies runtime login, schema/release, manifest attestation and heartbeat before any scan can start.

- [ ] **Step 1: Write failing lifecycle tests with injected clocks/readers.**

```python
def test_reader_progress_does_not_block_renewal(supervisor_fixture):
    control = supervisor_fixture.reader_that_blocks_after_one_batch()
    control.advance(16)
    assert control.renewals >= 1
    assert control.heartbeats >= 1
    control.advance(120)
    assert control.root_state == "degraded"
    assert control.finalizations == 0

def test_unreaped_reader_prevents_replacement(supervisor_fixture):
    control = supervisor_fixture.unreapable_reader()
    control.cancel()
    control.tick()
    assert control.started_readers == 1
    assert control.supervisor.has_unreaped_reader(control.root_id)
```

Define the fixture in `test_supervisor.py` with a monotonic fake clock, a fake bounded channel, counted heartbeat/lease callbacks and a fake process whose join/liveness is controllable; use the real protocol decoder and coordinator state machine. Integration/deployment tests use real processes for normal exit, SIGTERM, reader kill, paused reader, database interruption, schema/manifest replacement, and two roots of very different sizes. Keep `foundation.probe` tests for all roles; indexer maintenance cannot impersonate an actor-bound operation.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/unit/indexing/test_supervisor.py --test-target backend/tests/integration/indexing/test_runner.py`.

- [ ] **Step 3: Implement the supervision state machine.**

```python
from enum import StrEnum

class ReaderState(StrEnum):
    STARTING = "starting"
    READING = "reading"
    FINALIZING = "finalizing"
    STOPPING = "stopping"
    UNREAPED = "unreaped"
    SETTLED = "settled"

def progress_expired(*, now: float, last_progress: float, timeout: int) -> bool:
    return now - last_progress >= timeout
```

Spawn a reader with an explicit argument vector, closed unrelated descriptors and only its read-only root/control/result descriptors. It imports reader/protocol/domain code, not Django startup; pass no database handle or session. This remains the indexer trust boundary, not a hostile-processor sandbox. Obtain source components through indexed parent traversal in bounded database batches; hold only the current root-relative component chain and bounded reader packets. If the supported protocol/resource bounds cannot represent a directory, report a degraded unsupported traversal explicitly and do not finalize it missing. Never concatenate a browser-supplied path.

Keep one separate supervisor control loop for lease renewal/heartbeat/cancellation, and bounded database mutation execution with separate connections and statement/lock timeouts. A blocked source read or database statement cannot consume the renewal loop. Use monotonic time for process deadlines, PostgreSQL time for leases. Heartbeats at most every 15 seconds use `current_job_id=None` during scans because `DirectoryWork` is not an `Operation` job; safe metrics contain scan counters, never filenames. Preserve stopping-state monotonicity.

Extend both the Python and database heartbeat allowlists with `scanObservedEntries`, `scanCompletedDirectories`, and `scanDegradedDirectories`, each an integer from zero through PostgreSQL's signed-bigint maximum within the existing 1 KiB metrics payload cap. Test exact parity and reject unknown/boolean/negative values. Leave `scanProgress` absent while no denominator exists and leave disk-pressure fields unclaimed. Per-root API counters come from `RootIndexState`, never a role-wide heartbeat aggregate.

The loop admits at most the configured global reader count (default two, maximum four), one per root, honors two-frame credits, and services active roots fairly. Renewal failure closes the commit gate before any queued batch is applied. Successful EOF transitions to bounded finalization ticks, not a long uninterruptible loop. No-progress timeout applies to observation progress rather than total directory duration; healthy large directories can run longer than 120 seconds.

On stop/timeout, stop accepting observations, close credits, terminate the exact child, wait a bounded grace period, then kill only that child if needed. If it remains unreaped, retain its root slot in `UNREAPED`, publish degraded state and start no replacement for that root. Do not spawn endless retries or claim recovery while a stuck reader persists. On container/process restart, the runtime must reap prior children before admitting replacement readers. The scanner makes no filesystem write in any state.

- [ ] **Step 4: Verify green.** Run lifecycle and runner tests, existing run-role/readiness/heartbeat suites, and `deployment --test-target tests/deployment/test_indexer_runtime.py`. Demonstrate a minute-plus scan retaining a live lease and heartbeat, a second root making progress, and no stale writes after cancellation.

- [ ] **Step 5: Review and commit.** Record real-process evidence and resource bounds; commit `feat: supervise recoverable scans without blocking worker liveness`; push `origin main`.

## Task 8: Typed filters and signed cursors

**Files:** Create `backend/aegis_apps/catalog/{filters,cursors}.py`, `backend/tests/unit/catalog/{test_filters,test_cursors}.py`. No HTTP route is enabled by this task.

**Interfaces:** Produces frozen `FileFilter` with normalized `kind`, `type`, `size`, `modified`, `availability`, `prefix` fields; `parse_filters(value: object) -> FileFilter`; `canonical_filter_bytes(filters: FileFilter) -> bytes`; frozen `CursorContext(user_id, cache_namespace, user_epoch, root_id, root_epoch, parent_id, parent_revision, filter_digest, sort, order, limit)`; `encode_cursor(context, key, travel) -> str` and `decode_cursor(value, context) -> CursorPosition`, whose fields are `key` and `travel`. `key` is a typed tuple of kind rank, null rank, sort value, normalized name key and UUID. `travel` is `next` or `previous`.

- [ ] **Step 1: Write failing validation/context tests.**

```python
import pytest
from aegis_apps.catalog.filters import parse_filters, canonical_filter_bytes

def test_equivalent_multiselects_have_one_cursor_context() -> None:
    first = parse_filters({"v": 1, "kind": ["file", "directory", "file"]})
    second = parse_filters({"v": 1, "kind": ["directory", "file"]})
    assert canonical_filter_bytes(first) == canonical_filter_bytes(second)

@pytest.mark.parametrize("value", [
    {"v": 1, "sql": "SELECT 1"},
    {"v": 1, "size": {"min": "20", "max": "10"}},
    {"v": 1, "modified": {"from": "2026-01-01"}},
    {"v": 1, "kind": ["file"] * 33},
])
def test_invalid_filters_fail_clearly(value) -> None:
    with pytest.raises(ValueError):
        parse_filters(value)
```

Test empty versus omitted selections, all unknown states, integer precision, signed nanosecond dates, timezone normalization, one-sided ranges, boolean/NaN rejection, 8 KiB/field/value caps before canonicalization, literal `%`, `_`, backslash and Unicode prefixes. Cursor tests alter each context field, signature, travel direction, tuple type/length, page limit, version, and signing timestamp; all fail with a restartable cursor error. No plaintext session key enters a cursor.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/unit/catalog/test_filters.py --test-target backend/tests/unit/catalog/test_cursors.py`.

- [ ] **Step 3: Implement canonical filters and cursor signatures.**

```python
import hashlib
import json
from django.core import signing

CURSOR_SALT = "aegis.catalog.cursor.v1"
CURSOR_MAX_AGE = 900

def context_digest(fields: dict[str, object]) -> str:
    raw = json.dumps(fields, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(raw).hexdigest()

def signed_cursor_payload(payload: dict[str, object]) -> str:
    return signing.dumps(payload, salt=CURSOR_SALT, compress=False)

def unsigned_cursor_payload(value: str) -> object:
    if not isinstance(value, str) or len(value) > 8192:
        raise ValueError("cursor_restart_required")
    try:
        return signing.loads(value, salt=CURSOR_SALT, max_age=CURSOR_MAX_AGE)
    except signing.BadSignature:
        raise ValueError("cursor_restart_required") from None
```

Include cursor/sort-key version, context digest, typed boundary tuple and travel in the signed payload. Decode with a strict shape and scalar validator; use constant-time comparison for expected context digest. Name boundaries carry the complete binary order key in canonical base64 plus UUID, never a truncated key. A signed cursor is opaque by API convention, not encrypted; include no raw source bytes, absolute paths or secrets.

Canonicalize selections by validating their original count, deduplicating and sorting allowed strings. `kind`/`availability` use the fixed enums; `type` allows lowercase ASCII alphanumerics of 1–16 characters plus an explicit unknown value. Empty arrays mean no restriction and normalize away. `size.unknown=true` or `modified.unknown=true` excludes simultaneous bounds. Size decimal bounds are inclusive and within unsigned 64-bit; date bounds must carry an offset, normalize to UTC nanoseconds, and use `[from,before)`. Prefix is literal display-name text, normalized with the same case-fold/NFC rule as name ordering; cap its UTF-8 input at 2 KiB. Escape SQL LIKE metacharacters if a LIKE implementation is used; do not change literal meaning.

- [ ] **Step 4: Verify green.** Run filter/cursor unit/property tests, Ruff and mypy. Verify equivalent filters reuse a digest but authorization, page-size or sorting changes invalidate it. Add boundary tests at 899/901 seconds using the signing clock, not sleeps.

- [ ] **Step 5: Review and commit.** Record schema/expiry/tampering evidence; commit `feat: define bounded metadata filters and signed browse cursors`; push `origin main`.

## Task 9: Authorized keyset queries and bounded details

**Files:** Create `backend/aegis_apps/catalog/{authorization,queries,serializers}.py`, `backend/aegis_apps/catalog/migrations/0002_browse_indexes.py`, `backend/tests/integration/catalog/{test_authorization,test_queries,test_query_bounds}.py`. Modify the catalog model/privilege column maps if stored/generated ordering columns are added; extend `backend/tests/conftest.py` with `browse_fixture`.

**Interfaces:** Produces `browse_context(user: User, root_id: UUID, namespace: str) -> ContextManager[BrowseContext]`, `directory_page(context: BrowseContext, parent_id: UUID | None, filters: FileFilter, sort: str, order: str, limit: int, cursor: str | None) -> dict[str, object]`, and `entry_details(user: User, entry_id: UUID, namespace: str) -> dict[str, object]`. `BrowseContext` holds the locked active root/user, epochs, and namespace. `CatalogNotFound`/`CatalogNotReady` live in `authorization.py`; `CursorRestartRequired` lives in `cursors.py`. All are fixed safe exceptions.

- [ ] **Step 1: Write failing order, authorization, and query-budget tests.**

```python
@pytest.mark.parametrize("sort", ["name", "modified", "size"])
@pytest.mark.parametrize("order", ["asc", "desc"])
def test_bidirectional_keysets_cover_ties_without_offset(browse_fixture, sort, order):
    browse_fixture.seed_ties(count=603)
    pages = browse_fixture.walk(sort=sort, order=order, limit=100)
    ids = [entry["id"] for page in pages for entry in page["entries"]]
    assert len(ids) == len(set(ids)) == 603
    assert browse_fixture.previous(pages[-1]) == pages[-2]["entries"]
    assert all(" OFFSET " not in sql.upper() for sql in browse_fixture.statements)
    assert all("COUNT(" not in sql.upper() for sql in browse_fixture.catalog_statements)
```

`browse_fixture` creates only synthetic users/direct/group grants and catalog rows, opens the new browse context, captures SQL, and invokes the actual query functions. Add every allowed filter with ties/nulls/missing values; identical displays with different raw bytes; a folder with only tombstones; unknown/foreign root-parent-entry IDs; a missing/inaccessible ancestor; and simultaneous grant revocation using two connections. Assert details never return raw names, absolute paths or source-inode hints and are bounded even in deeply nested trees.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/integration/catalog/test_authorization.py --test-target backend/tests/integration/catalog/test_queries.py --test-target backend/tests/integration/catalog/test_query_bounds.py`.

- [ ] **Step 3: Implement shared read authorization, indexes and lexicographic predicates.**

```python
from functools import reduce
from operator import or_
from django.db.models import Q

def keyset_predicate(fields: tuple[str, ...], ascending: tuple[bool, ...],
                     boundary: tuple[object, ...], *, previous: bool) -> Q:
    if not fields or len(fields) != len(ascending) or len(fields) != len(boundary):
        raise ValueError("invalid keyset")
    equal = Q()
    branches: list[Q] = []
    for field, is_ascending, value in zip(fields, ascending, boundary, strict=True):
        comparison = "gt" if is_ascending != previous else "lt"
        branches.append(equal & Q(**{f"{field}__{comparison}": value}))
        equal &= Q(**{field: value})
    return reduce(or_, branches)
```

Only the server's fixed sort map supplies `fields` and directions. Materialize/annotate kind rank (`directory=0`, other kinds `1`), null rank (known `0`, null `1`), and a nonnull comparison value. Directories and null ranks remain ascending for both advertised directions; reverse all comparator directions only to fetch a previous page, then reverse that bounded result before returning it. Append normalized name key and UUID as deterministic tie-breakers; for name sorting the repeated name key need not appear twice in SQL. Extend the typed cursor boundary consistently to include this secondary name key.

Indexes begin with root, logical parent and missing/live visibility, then the exact rank/value/name/UUID ordering. Create named ascending and descending variants for name/modified/size where a backwards scan cannot preserve directories-first semantics. A default browse constrains live visibility, not an unbounded post-fetch tombstone filter. If explicit availability spans live/missing branches, apply the same keyset in each branch with `limit+1`, then merge their bounded candidates in one SQL statement. Add a root/parent/type/name support index only if the selective-filter query plans require it; measure all index variants together in Task 15. Database sorts may use bounded work memory, but the application never reads a folder into Python to sort/filter it.

Inside `transaction.atomic`, lock the active root and user rows `FOR SHARE` in root-before-user order, then recheck active state, manifest binding and effective additive grants. Shared read locks permit concurrent browsers while serializing with foundation grant/epoch mutations; do not use a root `FOR UPDATE` lock for every browse. No `select_for_update()` on the grouped `authorized_roots()` queryset: PostgreSQL cannot lock aggregate result rows that way. Query catalog entries only inside the authorized root/parent predicate. Direct entry lookup first resolves a candidate through a permission-bound query, then rechecks under the same read boundary. No superuser shortcut.

Compare the locked user's epoch with the request/session epoch captured before authorization; a mid-request change rejects the session rather than minting a new cursor under an old browser namespace. A changed binding must likewise reject an old web process's configured manifest. Add two-connection tests for both races.

Fetch at most `limit+1`, return narrow list projections and direction-aware cursors, and do not promise a snapshot across requests. Return at most 64 nearest ancestor labels plus `ancestorsTruncated`; parent traversal is constrained to the same root, detects cycles, and streams additional availability checks rather than returning an unbounded path. Missing/inaccessible ancestors make descendant availability stale/unavailable; retain authorized last-indexed metadata but never label it a current empty directory.

For an authorized newly activated root whose synthetic anchor has not yet been created by maintenance, raise `CatalogNotReady`; do not invent an entry UUID, create an anchor during GET, or report an empty successful folder. The separate status endpoint can return `not_indexed` without root-index state. Once scheduling creates the anchor, normal bounded pages are available even while scanning.

Budget at most eight catalog/authorization data statements for an ordinary page; measure session middleware and transaction-control statements separately with a total request budget of 16. Add database statement/lock timeouts with fixed safe error mapping, no raw SQL in responses. The actual scale gate, not `EXPLAIN` alone, decides whether an index/filter combination is acceptable.

- [ ] **Step 4: Verify green.** Run query/authorization tests with all six sorts, next/previous traversal, filters and revocation barriers; inspect captured SQL. Run actual-role SELECT tests, migration drift, Ruff and mypy.

- [ ] **Step 5: Review and commit.** Record query count, authorization, precision and ordering evidence; commit `feat: query indexed directories with authorized keysets`; push `origin main`.

## Task 10: Catalog and scan HTTP endpoints

**Files:** Create `backend/aegis_apps/catalog/api.py`, `backend/aegis_apps/indexing/{selectors,serializers}.py`, `backend/tests/integration/catalog/test_api.py`, `backend/tests/integration/indexing/test_status_api.py`. Modify `backend/aegis/urls.py`, `backend/aegis_apps/roots/middleware.py`, and the existing common error-response tests where route coverage grows.

**Interfaces:** Produces `DirectoryListView`, `EntryDetailView`, `IndexStatusView`, `RootScanView` and the four endpoints in the approved spec. `index_status(user: User, root_id: UUID) -> dict[str, object]` produces the wire `IndexStatus` under `BROWSE`; `ROOT_ADMIN` alone authorizes only the rescan request, not metadata reads.

- [ ] **Step 1: Write failing HTTP tests.**

```python
def test_browse_is_private_and_never_enumerates_source(api_catalog, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("web touched original filesystem")
    monkeypatch.setattr("os.scandir", forbidden)
    response = api_catalog.client.get(f"/api/v1/roots/{api_catalog.root.pk}/entries")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    payload = response.json()
    assert len(payload["entries"]) <= 100
    assert "total" not in payload
    assert "/srv/aegis" not in response.content.decode()
```

Define `api_catalog` in `backend/tests/conftest.py` with a protected temporary manifest, synthetic direct/group fixtures, and `Client(enforce_csrf_checks=True)` logging in through the existing authentication endpoint. Do not disable the real session policy. Test anonymous 401, indistinguishable 404s, stale/malformed cursor 409, bad filters/limits/duplicate parameters 400, CSRF failure 403, wrong method 405, schema/database outage 503, generic 500, and `private,no-store` for every framework/error path. Reject delete/move/rename actions, absolute-path inputs and content/delivery parameters.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/integration/catalog/test_api.py --test-target backend/tests/integration/indexing/test_status_api.py`.

- [ ] **Step 3: Implement thin views and no-store coverage.**

```python
# aegis/urls.py additions; use Django's UUID converters.
path("api/v1/roots/<uuid:root_id>/entries", DirectoryListView.as_view(), name="entry-list"),
path("api/v1/entries/<uuid:entry_id>", EntryDetailView.as_view(), name="entry-detail"),
path("api/v1/roots/<uuid:root_id>/index-status", IndexStatusView.as_view(), name="index-status"),
path("api/v1/roots/<uuid:root_id>/scans", RootScanView.as_view(), name="root-scan"),
```

Follow `RootListView`: `SessionAuthentication`, `JSONRenderer`, explicit authenticated `User`, generic problem response, no implicit DRF/admin permission fallback. Build the response completely inside the authorized query boundary. Request bodies/parameters have strict count/length checks before JSON parsing, and normalized filters are capped again afterward. Require a valid client-supplied `X-Request-ID` for a rescan and use it consistently as the idempotency/audit request ID. A successful request returns `202 {"scanId": "<opaque UUID>"}` with no root content. Apply CSRF before mutation and return 429 with bounded `Retry-After` for the Task 4 manual-request limit.

Use fixed errors `catalog_not_found`, `catalog_not_ready`, `cursor_restart_required`, `invalid_catalog_query`, `catalog_unavailable`, and `scan_request_denied`; titles are static safe text. Map `CatalogNotReady` to 503 with `Retry-After: 3`, distinct from dependency outage; the UI uses authorized index status to explain not-yet-indexed state. Cursor errors are emitted only after current authorization so they cannot reveal a foreign root. Unknown/unauthorized object bodies/statuses are identical. Extend outer no-store middleware to these exact path families, including resolver/method/unhandled errors, without adding filesystem checks there.

Status reads stored root/run counters and freshness only, never enumerates roots or computes global counts. Stale/mismatched worker/binding state produces unavailable/degraded, not false ready. Do not fake a percentage or implement capacity metrics as zero: storage capacity and byte-delivery progress remain later package work.

- [ ] **Step 4: Verify green.** Run focused API/status tests, all existing auth/root APIs, role privileges, Ruff/mypy and migration checks. Assert successful bodies stay at most 1 MiB at the maximum page/name bounds.

- [ ] **Step 5: Review and commit.** Record HTTP status/cache/CSRF/permission evidence; commit `feat: expose private catalog and scan status APIs`; push `origin main`.

## Task 11: Validated browser API and bounded private pages

**Files:** Create `frontend/src/features/files/{types,api,queries,navigation}.ts`, `frontend/src/features/files/{api,queries,navigation}.test.tsx`. Modify `frontend/src/api/{http,http.test}.ts` and `frontend/src/features/auth/{cache,cache.test}.ts` only for bounded successful bodies and private-state cleanup registration.

**Interfaces:** Produces the shared wire types; `fetchDirectory(input: BrowseInput, signal: AbortSignal) -> Promise<DirectoryPage>`, `fetchEntry(id: string, signal: AbortSignal) -> Promise<EntryDetails>`, `fetchIndexStatus(rootId: string, signal: AbortSignal) -> Promise<IndexStatus>`, `requestScan(rootId: string, requestId: string, csrfToken: string) -> Promise<{scanId: string}>`, and `directoryQueryOptions(input: BrowseInput)`. `BrowseInput` contains namespace, root ID/epoch, parent ID, filters, sort and order, never a filesystem path.

- [ ] **Step 1: Write failing validation/cache tests.**

```typescript
it("bounds the active directory and rejects foreign-root rows", async () => {
  const setup = createBrowseQueryFixture();
  for (let page = 0; page < 12; page += 1) await setup.fetchNext();
  expect(setup.pages()).toHaveLength(5);
  expect(setup.entryIds().length).toBeLessThanOrEqual(500);
  setup.respondWithForeignRoot();
  await expect(setup.fetchNext()).rejects.toMatchObject({status: 502});
});
```

Define `createBrowseQueryFixture()` in the test file with a real `QueryClient` and MSW handlers producing deterministic valid `DirectoryPage` objects and signed-looking opaque test cursors; it calls the actual query functions, not a separate cache implementation. Test next/previous fetch, filter changes, namespace changes, account switch during response parsing, abort, malformed/oversized success bodies, nulls/unsafe integer encodings, duplicate IDs, root/parent mismatch, invalid cursors and extra private fields. Verify one-million-step navigation input cannot grow history/query maps beyond their caps.

- [ ] **Step 2: Verify red.** Run `npm --prefix frontend test -- src/features/files/api.test.tsx src/features/files/queries.test.tsx src/features/files/navigation.test.tsx`.

- [ ] **Step 3: Implement response guards and a five-page query window.**

```typescript
import {infiniteQueryOptions} from "@tanstack/react-query";

export function directoryQueryOptions(input: BrowseInput) {
  return infiniteQueryOptions({
    queryKey: ["files", input.namespace, input.rootId, input.rootEpoch,
      input.parentId, input.filters, input.sort, input.order],
    initialPageParam: null as string | null,
    queryFn: ({pageParam, signal}) => fetchDirectory({...input, cursor: pageParam}, signal),
    getNextPageParam: (page) => page.nextCursor ?? undefined,
    getPreviousPageParam: (page) => page.previousCursor ?? undefined,
    maxPages: 5,
    gcTime: 0,
    staleTime: 5000,
    retry: false,
  });
}
```

Add optional `cursor: string | null` to the request form of `BrowseInput`, not to the stable query identity. Validate UUIDs, enums, string lengths, decimal domains, timestamps, IDs/root context, cursor length, at most 250 summaries and at most 64 ancestors. Extend `apiRequest` with an optional `maxResponseBytes` field consumed locally, never passed to `fetch`; file APIs set it to 1 MiB and use a streaming byte cap before `JSON.parse`. Preserve the existing 16 KiB error parser and same-origin/CSRF/redirect rules.

Active page data is the only retained directory dataset (`gcTime=0` for inactive file/details/status queries). Keep a separate namespace-owned LRU of at most 32 navigation records containing only root/parent/filter/sort/cursor/visible-anchor information, with at most 8 KiB per record. Never persist this in local storage, IndexedDB, a service worker or browser history state. History URLs contain opaque IDs only. Register a synchronous private-state cleanup callback so auth teardown empties navigation records before awaiting other cleanup. Unsubscribe callbacks on owner disposal to avoid another unbounded registry.

Use the existing auth transition ownership and query cancellation. A 401 closes private rendering, cancels file/status/detail queries, purges state, then routes to login. A 404 for a formerly visible root clears that root's records and reloads authorized roots. An obsolete namespace or aborted request cannot publish data after a newer transition. Dedupe only within the current bounded page window; this package has no bulk selection store.

- [ ] **Step 4: Verify green.** Run all new browser data/cache tests plus existing HTTP/auth/cache tests, `npm --prefix frontend run lint`, `npm --prefix frontend run typecheck`, and `npm --prefix frontend run build`.

- [ ] **Step 5: Review and commit.** Record bounded-cache, parsing and late-response evidence; commit `feat: add bounded session-scoped file query state`; push `origin main`.

## Task 12: Virtualized mobile file navigation

**Files:** Create `frontend/src/features/files/{FilesPage,VirtualFileList,FileRow}.tsx`, `frontend/src/features/files/files.css`, `frontend/src/features/files/test-fixtures.ts`, and `frontend/src/features/files/{FilesPage,VirtualFileList}.test.tsx`. Modify `frontend/src/features/roots/{RootCard.tsx,RootListPage.test.tsx}`, `frontend/src/app/router.tsx`, `frontend/package.json`, and `frontend/package-lock.json`.

**Interfaces:** Produces `FilesPage`, `VirtualFileList({entries, onOpen, onLoadNext, onLoadPrevious, hasNext, hasPrevious})`, and `FileRow({entry, onOpen})`. `onOpen(entry: EntrySummary)` navigates a directory or selects a file for Task 13's details panel. Before that panel lands, the selected file displays its bounded summary inline; there is no dead Open/Download button.

- [ ] **Step 1: Write failing navigation and bounded-render tests.**

```typescript
it("renders a bounded row window and offers keyboard paging", () => {
  const entries = Array.from({length: 500}, (_, index) => fixtureEntry(index));
  render(<VirtualFileList entries={entries} onOpen={vi.fn()}
    onLoadNext={vi.fn()} onLoadPrevious={vi.fn()} hasNext hasPrevious />);
  expect(screen.getAllByRole("listitem").length).toBeLessThanOrEqual(80);
  expect(screen.getByRole("button", {name: "Load more files"})).toBeEnabled();
  expect(screen.getByRole("button", {name: "Load previous files"})).toBeEnabled();
});
```

Define `fixtureEntry(index: number): EntrySummary` in a test-only `frontend/src/features/files/test-fixtures.ts`, using deterministic UUIDs, synthetic labels and decimal strings. Tests cover root links, nested opaque-ID routes, unknown roots, visible-anchor restoration after page eviction, filter/sort route changes, long names, RTL names, keyboard focus surviving eviction, narrow widths, reduced motion and pending/degraded/empty states. Update the old root-button explanation test to verify actual navigation; preserve all Phase 1 login/isolation assertions.

- [ ] **Step 2: Verify red.** Run `npm --prefix frontend test -- src/features/files/FilesPage.test.tsx src/features/files/VirtualFileList.test.tsx src/features/roots/RootListPage.test.tsx`.

- [ ] **Step 3: Implement the bounded list and authenticated routes.** Install `@tanstack/react-virtual@3.14.13` with `npm --prefix frontend install --save-exact @tanstack/react-virtual@3.14.13`; keep both package files in the commit. Its verified React peer range includes React 19; if execution finds this exact version unavailable or incompatible, stop the dependency change and document a reviewed substitute, not an unlocked latest install.

```tsx
const virtualizer = useVirtualizer({
  count: entries.length,
  getScrollElement: () => scrollRef.current,
  estimateSize: () => 80,
  getItemKey: (index) => entries[index].id,
  overscan: 8,
});

<ul aria-label="Files" style={{height: virtualizer.getTotalSize(), position: "relative"}}>
  {virtualizer.getVirtualItems().map((row) => (
    <li key={row.key} style={{position: "absolute", insetInline: 0, top: 0,
      height: 80, transform: `translateY(${row.start}px)`}}>
      <FileRow entry={entries[row.index]} onOpen={onOpen} />
    </li>
  ))}
</ul>
```

Use fixed 80-pixel rows with two-line ellipsis for names and full accessible labels; unknown totals are omitted from ARIA rather than fabricated. The scroll wrapper owns the ref, and the component imports `useVirtualizer`/React hooks. Type icons distinguish file/directory/image/video hints without requesting originals. Each row's native button/link fills the row; avoid nested interactive controls. The list and virtualizer's measurement/key caches must stay bounded when pages are evicted; test the actual installed library, not only the wrapper's array length.

Before prepending or evicting a page, capture the first visible entry ID and its viewport offset. After the bounded window changes, restore that surviving ID's offset in a layout effect; if it disappeared, choose the nearest surviving neighbor and announce the update. Keep a focused row in the rendered range or move focus to the nearest retained control; never retain unlimited offscreen rows to preserve focus. Visible previous/next controls are the accessible fallback and trigger the same cursor API as scrolling. Disable duplicate fetches while a direction is in flight.

Add `/files/:rootId` and `/files/:rootId/directories/:parentId` within the existing `AuthBoundary` and application shell. Root cards become real links. Reuse safe root display names and permissions from the authorized root query; a direct URL must resolve through the server, not a permissive client lookup. Back/forward restoration waits for the existing session revalidation. No filter text or raw name is placed in route paths.

Keep the deep-slate palette and existing tokens. All controls remain at least 44 by 44 CSS pixels; no horizontal page overflow at 320 pixels; visible focus and reduced motion remain. No photos/video posters or PWA service worker are claimed by this task.

- [ ] **Step 4: Verify green.** Run all file/root component tests, lint/types/build, and a local Playwright probe at 320/390/412-pixel widths using synthetic API fixtures. Count DOM rows and retained pages through repeated eviction; screenshot only synthetic names with the existing masking rules.

- [ ] **Step 5: Review and commit.** Record keyboard, viewport and bounded-render evidence; commit `feat: add virtualized dark mobile file browsing`; push `origin main`.

## Task 13: Filters, details, and scan-state interactions

**Files:** Create `frontend/src/features/files/{FilterPanel,FilterChips,EntryDetailsPanel,IndexStatusPanel}.tsx`, `frontend/src/features/files/filter-state.ts`, `frontend/src/features/files/{FilterPanel,EntryDetailsPanel,IndexStatusPanel}.test.tsx`. Modify `FilesPage.tsx`, `files.css`, and the file query/API tests.

**Interfaces:** Produces `FilterPanel({value, open, onApply, onCancel})`, `FilterChips({value, onChange})`, `EntryDetailsPanel({entryId, onClose})`, `IndexStatusPanel({rootId, canRequestScan})`; pure `removeFilter(filters: FileFilters, field: keyof Omit<FileFilters, "v">) -> FileFilters`. Applied filters live in namespace-owned page state; panel drafts do not change the query until Apply.

- [ ] **Step 1: Write failing touch/keyboard/draft tests.**

```tsx
it("does not query while editing or after Cancel", async () => {
  const apply = vi.fn();
  const cancel = vi.fn();
  render(<FilterPanel value={{v: 1}} open onApply={apply} onCancel={cancel} />);
  fireEvent.change(screen.getByLabelText("Filename starts with"), {target: {value: "IMG_"}});
  expect(apply).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", {name: "Cancel"}));
  expect(cancel).toHaveBeenCalledOnce();
  expect(apply).not.toHaveBeenCalled();
});
```

Test AND/OR groups, size/date validation, explicit unknown metadata, chip removal/clear-all, timezone conversion, Escape/Cancel focus restoration, screen-reader names, stale selected-file details after navigation, 401 purging, no scan button without `root_admin`, retry with the same request ID, 429 feedback, idle/hidden/offline polling and every index-state label. No viewer/editor control is exposed for a file summary.

- [ ] **Step 2: Verify red.** Run `npm --prefix frontend test -- src/features/files/FilterPanel.test.tsx src/features/files/EntryDetailsPanel.test.tsx src/features/files/IndexStatusPanel.test.tsx`.

- [ ] **Step 3: Implement explicit draft/apply behavior and bounded status.**

```typescript
export function removeFilter(
  filters: FileFilters, field: keyof Omit<FileFilters, "v">,
): FileFilters {
  const result = {...filters};
  delete result[field];
  return result;
}

export function statusPollInterval(state: IndexStatus["state"], visible: boolean,
                                   online: boolean): number | false {
  if (!visible || !online) return false;
  return state === "queued" || state === "scanning" ? 3000 : 30000;
}
```

Use a modal native dialog on mobile and the same semantics on larger screens: grouped kind/type/availability choices, prefix text, decimal size inputs, date range with an explicit displayed local timezone, Apply/Cancel and clear-all. Use static common extension options plus a validated literal extension input; no global facet query or count is needed. Maintain at most 32 selections per field and the shared request bounds. Date inputs map local day boundaries to offset-qualified instants without pretending modification time is capture time; reject invalid local times rather than silently shifting them.

Opening creates a fresh bounded draft from applied state. Apply validates once, closes the panel, resets the cursor and loads page one; Cancel/Escape discard changes. Chips are removable native buttons with full accessible labels. Return focus to Filters after closing. Use a live region for loading/results changes without announcing an exact count or moving focus on every row load.

Details fetch one authorized DTO only when selected, cancel on close/navigation, and display exact size, file-modified time, kind/type hint, source state, version and bounded ancestors. BigInt formatting avoids precision loss. Unsupported/symlink entries are informational. Distinguish not indexed, actively indexing, genuinely empty, stale/unavailable source, and failed HTTP request. Show observed counters and update time, never an invented percent. Status polling pauses when hidden/offline, uses a 3-second active/30-second idle interval, and backs off failures up to 60 seconds. These are metadata status calls, not SSE.

While the first page is not ready or has no observed entries during an initial scan, a progress change retries that page at most once per active status interval. Once a nonempty page window is visible, offer a Refresh indication for new catalog generations instead of refetching five pages on every batch. Preserve the visible anchor on refresh. A genuine zero-match filter result remains distinguishable from an unindexed directory.

Rescan uses a newly generated UUID as `X-Request-ID`, retains it across a network retry, obtains existing CSRF state and obeys the root's `root_admin` capability. Do not show this action just because the account is a platform administrator. The UI never offers a delete/rename/move action or labels an icon as a generated thumbnail.

- [ ] **Step 4: Verify green.** Run the new interaction tests and complete frontend lint/types/tests/build. Validate real dialog focus and date/timezone behavior in mobile Chromium and WebKit during Task 14; jsdom alone does not certify those interactions.

- [ ] **Step 5: Review and commit.** Record filter/reset/unknown-state/progress evidence; commit `feat: add mobile file filters details and scan progress`; push `origin main`.

## Task 14: Real-stack browser and source-preservation regressions

**Files:** Create `frontend/e2e/phase2a.spec.ts`, `frontend/e2e/auth.ts`, `tests/deployment/test_phase2a_harness.py`, and `backend/tests/integration/indexing/test_end_to_end_scan.py`. Modify `scripts/{test-e2e.sh,e2e_support.py}`, `frontend/e2e/phase1.spec.ts`, `tests/deployment/{test_e2e_harness,test_container_boundaries}.py`, and `.github/workflows/ci.yml` only as required to run the expanded existing suites.

**Interfaces:** Extends the existing `make test-e2e` contract, retaining its reserved project/port exclusion and safe teardown. `auth.ts` exports the existing `requiredSecret(name: string)` and `signIn(page: Page, username: string, password: string)` helpers. Adds real catalog requests and scan completion waits; does not seed fake browser responses as acceptance evidence.

- [ ] **Step 1: Write failing real-stack journeys and harness-safety tests.**

```typescript
import {expect} from "@playwright/test";
import {test} from "./safe-test";
import {requiredSecret, signIn} from "./auth";

test("Alice browses indexed files while Bob remains isolated", async ({page}) => {
  await signIn(page, "alice", requiredSecret("E2E_ALICE_PASSWORD"));
  await page.getByRole("link", {name: /Alice files/}).click();
  await expect(page.getByRole("list", {name: "Files"})).toBeVisible();
  await expect(page.getByText("alice-only.txt", {exact: true})).toBeVisible();
  await expect(page.getByText("bob-only.txt", {exact: true})).toHaveCount(0);
  await page.getByRole("button", {name: "Filters", exact: true}).click();
  await page.getByLabel("Filename starts with").fill("alice-only");
  await page.getByRole("button", {name: "Apply", exact: true}).click();
  await expect(page.getByText("alice-only.txt", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "Sign out"}).click();
  await page.goBack();
  await expect(page.getByText("alice-only.txt", {exact: true})).toHaveCount(0);
});
```

Add real next/previous paging beyond five windows, nested navigation, size/date/type filters, literal prefixes, details, unknown metadata, account switch and grant revocation, stale cursor restart, delayed responses after logout, empty/inaccessible roots, and keyboard/dialog focus at 320/390/412 pixels. Existing foundation assertions remain, except the deliberately replaced explanation-button behavior.

- [ ] **Step 2: Verify red.** Run `make test-e2e` against its disposable stack and the new focused harness tests. Expect the new fixture/journey behavior to fail; infrastructure or unavailable Docker is not the intended red result.

- [ ] **Step 3: Generate owned source fixtures and exercise real indexing.**

```python
# e2e_support.py: sources live under the already validated private work directory.
for owner in ("alice", "bob"):
    source = path / f"source-{owner}"
    source.mkdir(mode=0o700)
    with (source / f"{owner}-only.txt").open("xb") as handle:
        handle.write(f"Synthetic {owner} fixture\n".encode("ascii"))
```

Extend this deterministic fixture with at least 603 entries, nested directories, tied names/timestamps, a symlink to an unmounted synthetic sibling, and mixed extension hints. Record an exact filename/type/size/mtime/content-hash manifest before mounting. No user library or tracked original-root directory is modified. Generate preflight inputs for these owned source directories, pass the manifest to migrate as Task 3 requires, seed users/grants through the existing idempotent test command, and wait with a bounded deadline for the real indexer/status API to settle before browser assertions.

After workers stop and before deleting any fixture, compare the source manifest with a fresh descriptor-safe snapshot. Fail on changed bytes/names/size/mtime, followed symlinks or unexpected source files. Cleanup handles only the recorded fixture files/directories and exact labeled project resources, bottom-up without following links; unknown contents or identity changes cause a retained diagnostic fixture and failure, not recursive deletion. Reports stay outside cleanup. Test cleanup-after-failure and refusal of pre-existing project resources.

Run both mobile Chromium and WebKit with the existing safe-test/reporter. No raw browser trace, session storage, cookie, credential, uncontrolled screenshot or unfiltered container log is retained. CI keeps the foundation suites and adds the catalog/scanner cases through normal test discovery; full million-entry runs remain opt-in rather than slowing every commit by hours.

- [ ] **Step 4: Verify green.** Run `make verify`, `make verify-compose`, and `make test-e2e` sequentially. Report exact counts, skipped platform-specific cases, cleanup outcome, and source-preservation evidence. Do not call this the scale gate.

- [ ] **Step 5: Review and commit.** Record real-stack evidence; commit `test: cover indexed mobile browsing and read-only scan recovery`; push `origin main`.

## Task 15: Catalog fixture and authenticated query benchmark

**Files:** Create `scripts/__init__.py`, `scripts/benchmarks/{__init__,run,resources,dataset,http_workload,report}.py`, `scripts/benchmarks/phase2a-profile.json`, `backend/aegis_apps/catalog/management/{__init__,commands/__init__,commands/seed_catalog_benchmark}.py`, `backend/tests/unit/catalog/test_benchmark_dataset.py`, `backend/tests/integration/catalog/test_benchmark_seed.py`, and `tests/deployment/test_benchmark_resources.py`. Modify `Makefile`, `docs/development.md`, and add measured catalog index migrations only if query evidence requires them.

**Interfaces:** Produces `DatasetShape(entries: int, wide_folder: int, seed: int)`, `catalog_records(shape: DatasetShape) -> Iterator[dict[str, object]]`, owned-resource context `benchmark_environment(profile, report_dir)`, and module CLI `python -m scripts.benchmarks.run` with `catalog`, `filesystem`, and `mobile` modes as they land. It creates its own database/stack and never accepts an arbitrary database URL. The concrete full-dataset invocation is below.

- [ ] **Step 1: Write failing determinism, resource-ownership, and real-query tests.**

```python
from itertools import islice
from scripts.benchmarks.dataset import DatasetShape, catalog_records

def test_catalog_generation_is_deterministic_and_streamed() -> None:
    shape = DatasetShape(entries=1000000, wide_folder=50000, seed=20260914)
    first = list(islice(catalog_records(shape), 1000))
    second = list(islice(catalog_records(shape), 1000))
    assert first == second
    assert len({row["id"] for row in first}) == 1000
```

Add validation for impossible shapes, unexpected databases/roles, double seeding, inherited production environment, report paths inside roots, unowned/replaced Docker resources, incomplete cleanup and credential redaction. Seed a small integration dataset with actual users/groups and verify pages against the generator's expected order and membership; do not benchmark an unauthenticated ORM loop and call it API latency.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py backend --test-target backend/tests/unit/catalog/test_benchmark_dataset.py --test-target backend/tests/integration/catalog/test_benchmark_seed.py` and `uv run --locked python scripts/verify.py deployment --test-target tests/deployment/test_benchmark_resources.py`. Expect missing benchmark modules/command behavior, not a connection to any operator service.

- [ ] **Step 3: Implement a versioned, reproducible workload.**

```json
{
  "version": 1,
  "package": "2A.1",
  "seed": 20260914,
  "entries": 1000000,
  "wideFolderChildren": 50000,
  "users": 10,
  "warmupSeconds": 300,
  "measurementSeconds": 900,
  "pageSize": 100,
  "mix": {"directoryPages": 60, "alternateSorts": 15, "details": 10,
          "basicFilters": 10, "indexStatus": 5},
  "wideFolderListShareMinimum": 0.25,
  "coldRestartRecordedSeparately": true,
  "workerLoad": "none_for_catalog_seed_mode",
  "referenceHost": {"cpuClass": "x86_64_Intel_N100", "cores": 4, "ramGiB": 16},
  "memoryLimitsMiB": {"postgres": 3072, "web": 1024, "operations": 1024,
                      "indexer": 1024, "media": 2048, "gateway": 256},
  "storageCalibration": {"databaseRandomReadIops": 20000,
                         "databaseSequentialMBps": 300,
                         "originalSequentialMBps": 150,
                         "originalMetadataOpsPerSecond": 150},
  "referenceCertification": "requires_calibrated_reference_host"
}
```

Implement deterministic UUIDs with a fixed UUID namespace and numbered seed keys. Stream batches through psycopg COPY or bounded bulk inserts; never hold 1M Python model instances. Distribute rows across multiple roots/directories and users with direct/group grants and an ungranted administrator. Include exactly reported file/directory counts, a 50K direct-child folder, Unicode/raw-byte/long/case-adjacent names, ties/nulls, common/rare types, and missing/inaccessible source states. Keep metadata-only seeding distinct from the filesystem fixture in Task 16.

The test-only seed command refuses production settings and requires a freshly owned empty benchmark database with a per-run ownership record; no destructive truncate/reset option is offered. `resources.py` creates uniquely named/labeled Docker resources, sanitized environment, protected generated secrets, loopback-only ephemeral ingress and SSD-backed disposable database storage with sufficient capacity. Do not reuse the ordinary verification runner's 384 MiB tmpfs for a dataset whose accepted catalog budget is 8 GiB. Verify exact resource IDs/labels before cleanup; never target `aegis_postgres-data` or broad project-name patterns. Write reports exclusively into a fresh, owned non-root directory and preserve them after teardown.

Catalog-only mode seeds `RootIndexState` with due times beyond the measurement window so the real indexer heartbeat can remain healthy without reconciling an empty synthetic mount over the seeded catalog. Record that fixture control explicitly; it is not a production skip-authentication or skip-fencing flag and not scan evidence. Do not run an initial/periodic scan against the catalog-only fixture. Task 16 uses a separate fresh physical fixture and real completed scans instead. Reference runs place all core services in the same four-CPU set and apply the listed memory limits; a faster host or uncalibrated root is labeled provisional even with these limits.

Use ten independent authenticated cookie jars, same-origin CSRF/login and actual HTTP list/details/filter/status routes. Bound concurrency and output queues. Warm and cold samples are separate; failed/denied requests count as failures rather than fast successes. Exercise six sorts and next/previous navigation with the fixed request mix, at least 25% of list traffic on the wide folder, and adversarial filter selectivity/ties. Add a matrix of each field alone and combined with each sort, including zero-hit/rare/common filters and all-field combinations. Capture `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` for representative actual parameterized queries, SQL counts, payload sizes, row counts and total catalog/index sizes.

Record p50/p95/p99, error rate, sample count and cold/warm/loaded labels; keep query plans sanitized of raw names, filter literals and private paths. Report hardware, schema/release/image/lock identities, PostgreSQL settings and actual memory/CPU limits. Index changes require the same correctness tests, migrations and new measurement—never disable a filter to meet the budget.

- [ ] **Step 4: Verify green.** Run small fixture tests and the full catalog workload using a newly created report directory:

```bash
benchmark_reports=$(mktemp -d /tmp/aegis-phase2a-reports.XXXXXXXX)
uv run --locked python -m scripts.benchmarks.run catalog --entries 1000000 --wide-folder 50000 --report-dir "$benchmark_reports"
```

The runner validates that variable's resolved target before any report write. Record `p95 <= 300 ms` unloaded, query/payload/index results and actual environment; leave reference certification open on an uncalibrated development/CI host. A small smoke run cannot complete this task's full dataset evidence.

- [ ] **Step 5: Review and commit.** Commit benchmark code, versioned profile, necessary index migrations and sanitized summary as `perf: add million-entry authenticated catalog benchmark`; push `origin main`. Keep generated secrets and raw fixture data out of Git.

## Task 16: Real filesystem scan, recovery, and resource benchmark

**Files:** Create `scripts/benchmarks/{filesystem,scan_workload}.py`, `tests/deployment/{test_benchmark_filesystem,test_scan_faults}.py`, and `docker/benchmark-fixture.Dockerfile`. Modify `scripts/benchmarks/{run,resources,report}.py`, `scripts/benchmarks/phase2a-profile.json`, and `docs/development.md`.

**Interfaces:** Produces `create_source_fixture(owned_target, shape) -> SourceFixtureManifest`, `verify_source_fixture(manifest) -> PreservationReport`, and `run_scan_workload(environment, profile) -> ScanReport`. Define these frozen report dataclasses in `report.py`: `SourceFixtureManifest` holds owner/resource identity, seed/shape/counts and an external streamed inventory-file reference; `PreservationReport` holds compared/changed/missing/unexpected counts and success; `ScanReport` holds elapsed seconds, observed/committed counts, aggregate RSS/container-memory peaks, catalog/index/work-table bytes, preservation report and hardware/workload identity. A manifest object never contains a million-entry in-memory list. The filesystem mode is explicitly opt-in, with `--allow-large-fixture` required above 10,000 source entries. Reports never expose operator data.

- [ ] **Step 1: Write failing ownership, streaming, and scan-recovery tests.**

```python
def test_fixture_refuses_operator_or_replaced_target(owned_fixture_target):
    target = owned_fixture_target
    target.replace_with_unowned_directory()
    with pytest.raises(ValueError, match="fixture ownership"):
        create_source_fixture(target, DatasetShape(1000, 100, 20260914))

def test_interrupted_scan_preserves_source_and_recovers(scan_benchmark_fixture):
    before = scan_benchmark_fixture.source_manifest()
    scan_benchmark_fixture.stop_reader_after_committed_batch(2)
    scan_benchmark_fixture.restart_indexer()
    scan_benchmark_fixture.wait_for_scan()
    assert scan_benchmark_fixture.source_manifest() == before
    assert scan_benchmark_fixture.missing_observed_files() == 0
```

Define `owned_fixture_target` and `scan_benchmark_fixture` locally in the deployment tests using Task 15's owned-resource context; their source replacement/fault actions affect only synthetic resources created by that context. Test insufficient space, unapproved size, file/directory symlink substitutions, wrong labels, interrupted generation, foreign files during cleanup, raw names/case-adjacent files on Linux, and mandatory report retention after errors.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py deployment --test-target tests/deployment/test_benchmark_filesystem.py --test-target tests/deployment/test_scan_faults.py` with small synthetic shapes.

- [ ] **Step 3: Implement the owned Linux filesystem builder and real workload.**

```python
def write_synthetic_file(directory_fd: int, raw_name: bytes, payload: bytes) -> None:
    source_name(raw_name)
    descriptor = os.open(raw_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("synthetic fixture write failed")
            view = view[written:]
    finally:
        os.close(descriptor)
```

This function belongs only to the test fixture builder, never to catalog/indexer runtime modules. The builder image uses the repository's pinned Python base, no database credentials or network, and one newly owned synthetic source volume. Its temporary writable mount exists only while creating the fixture; all application roles receive that volume read-only after the builder exits. This supports raw-byte/case-adjacent Linux names without assuming the developer's macOS filesystem has the same semantics. No original role gets a writable-root test override.

The runner allocates/checks disk capacity before generation, with at least 12 GiB free for catalog/scan state plus a separately measured source-inode/data budget and safety margin. Generate tiny deterministic payloads/sparse files in bounded batches, with a durable ownership inventory outside the source. Record source files/directories separately from synthetic catalog anchors. Do not create a million files in the checkout or an arbitrary user-provided mounted path. Cleanup walks only the exact inventory bottom-up with no-follow descriptors; unknown entries, aliases or ownership changes stop cleanup and preserve the fixture for inspection.

Run the deployed indexer from an empty owned catalog against at least 1M actual source entries with a 50K-child directory. Capture elapsed metadata-only scan time, observed/committed counts, database/index size and aggregate indexer-process-tree RSS, including reader children—not just parent RSS. Separately measure the container memory peak and database resource use. Verify source bytes/names/size/mtime before/after the run. Empty extension-labeled fixtures are metadata data, not photo/video decoder or thumbnail evidence.

Exercise worker/reader kill after a committed batch, interrupted finalization, lease expiry with a paused old process, database restart, read permission loss and source mount disappearance/replacement using isolated test resources. Restore source availability explicitly, rerun, and prove convergence without missing-mark cascades or original writes. Loaded browsing uses Task 15's actual HTTP workload concurrently with one active scan. Optional CPU/GPU models, preview jobs, transfers and transcodes remain absent and are named as such in the report.

For the loaded profile, finish the initial real scan, then request one repeat scan through a separately granted synthetic ROOT_ADMIN controller while ten browse-only users run the workload. Keep other roots' due times outside the window using fixture policy and configure one reader for this profile; report `scan_only` and the active root ID/count. Do not create a second seeded catalog and quietly benchmark a different dataset. Fixture generation and pre/post preservation snapshots are timed separately from scan throughput.

- [ ] **Step 4: Verify green and record full evidence.**

```bash
scan_reports=$(mktemp -d /tmp/aegis-phase2a-scan-reports.XXXXXXXX)
uv run --locked python -m scripts.benchmarks.run filesystem --entries 1000000 --wide-folder 50000 --allow-large-fixture --report-dir "$scan_reports"
```

Check p95 loaded browse at most 500 ms, aggregate indexer RSS at most 750 MiB, catalog tables/indexes at most 8 GiB, and metadata-only scan within four hours on the calibrated root. Include runtime work-table sizes as a separate total so a queue cannot hide unbounded storage outside the catalog metric. Report actual storage calibration and limits; uncalibrated hardware remains provisional evidence. Fault tests do not silently reuse the performance dataset after mutations—reset only by creating another owned fixture or explicit deterministic repair of that synthetic source.

- [ ] **Step 5: Review and commit.** Record full scan/fault/preservation/resource results and cleanup disposition; commit `perf: exercise real million-entry scans and recovery`; push `origin main`.

## Task 17: Long mobile scale journey and workload evidence

**Files:** Create `frontend/e2e/phase2a-scale.spec.ts`, `frontend/e2e/benchmark-metrics.ts`, `frontend/e2e/benchmark-observer.ts`, `frontend/e2e/benchmark-metrics.unit.ts`, `frontend/vite.benchmark.config.ts`, and `frontend/playwright.benchmark.config.ts`. Modify `scripts/benchmarks/{run,report}.py`, `scripts/benchmarks/phase2a-profile.json`, `frontend/{package.json,package-lock.json,vite.config.ts,playwright.config.ts}`, `frontend/e2e/auth.ts`, and `docs/development.md`.

**Interfaces:** Produces a separately opted-in benchmark Playwright configuration, `collectJourneyMetrics(page) -> Promise<JourneyMetrics>`, and `JourneyMetrics` with LCP/INP/JS-heap/DOM-row peaks plus sample counts and browser/profile identity. No secrets, query text, filenames, screenshot, trace or browser storage is part of that DTO. Default `make test-e2e` runs functional journeys only; the scale case requires the benchmark harness and its owned 1M/50K environment.

- [ ] **Step 1: Write failing metric/long-journey checks.**

```typescript
import {expect} from "@playwright/test";
import {test} from "./safe-test";

test("50K-folder browsing remains bounded across eviction and return", async ({page}) => {
  const journey = await runOwnedScaleJourney(page);
  expect(journey.catalogEntries).toBeGreaterThanOrEqual(1_000_000);
  expect(journey.wideFolderChildren).toBe(50_000);
  expect(journey.visitedForward).toBe(50_000);
  expect(journey.visitedBackward).toBe(50_000);
  expect(journey.peakRenderedRows).toBeLessThanOrEqual(80);
  expect(journey.peakHeapBytes).toBeLessThanOrEqual(250 * 1024 * 1024);
  expect(journey.lcpMs).toBeLessThanOrEqual(2500);
  expect(journey.inpMs).toBeLessThanOrEqual(200);
});
```

Define `runOwnedScaleJourney(page)` in `phase2a-scale.spec.ts`; it obtains only bounded fixture identifiers from the verified harness manifest, signs in with file-backed synthetic secrets through `auth.ts`, and calls the actual deployed UI/API. Test metric aggregation with synthetic interaction samples, absent browser support, zero observations, rounding and outliers. Missing metrics or an unvisited fixture are failures/not-measured, never zero-valued passes.

- [ ] **Step 2: Verify red.** Run `npm --prefix frontend test -- e2e/benchmark-metrics.unit.ts` after adding that explicit file to Vitest's include list, and `uv run --locked python -m scripts.benchmarks.run mobile --smoke` once the harness parser exists. Smoke mode creates at most 1,000 synthetic catalog entries, runs a distinct two-minute wiring check and labels all output `smoke`; it cannot execute/pass the full-dataset assertions or change acceptance status. Keep the million-entry command explicitly opted in and separate from default test discovery.

- [ ] **Step 3: Implement measured browser profiles and a fixed journey.**

```typescript
const client = await page.context().newCDPSession(page);
await client.send("Emulation.setCPUThrottlingRate", {rate: 4});
await client.send("Network.emulateNetworkConditions", {
  offline: false, latency: 40,
  downloadThroughput: 40_000_000 / 8,
  uploadThroughput: 10_000_000 / 8,
});
await page.setViewportSize({width: 412, height: 915});
```

The Chromium performance profile uses touch input, 412×915 CSS pixels, 4× CPU slowdown, 40 Mbps down, 10 Mbps up and 40 ms RTT. Record actual pinned browser versions and host CPU; CPU slowdown is not proof that the host equals the N100 reference. WebKit runs the same functional cache/paging/revocation journey separately, without claiming unsupported CDP measurements. Default `playwright.config.ts` excludes `phase2a-scale.spec.ts`; the separate benchmark configuration includes it and refuses to run without the harness ownership manifest.

Install the exact dev dependency with `npm --prefix frontend install --save-dev --save-exact web-vitals@6.2.1`. Bundle only `onLCP` and `onINP` plus `benchmark-observer.ts` into a temporary harness IIFE through `vite.benchmark.config.ts`; inject it with Playwright before navigation. It is not imported by `frontend/src` or shipped as an application asset, and it makes no network request. Retain only the latest numeric metric value and sample count; do not serialize a metric's DOM entries/attribution or retain an event history. Use the library's interaction grouping/outlier semantics rather than an independently approximated INP. Record unsupported metrics explicitly. Sample JS heap through Chromium's runtime protocol and DOM row counts at fixed intervals; do not retain heap dumps, raw trace events or private component state. Force no garbage collection during the measured journey to manufacture a low memory number.

```typescript
// benchmark-observer.ts — injected by the harness, never imported by the app.
import {onINP, onLCP} from "web-vitals";

const snapshot = {lcpMs: null as number | null, inpMs: null as number | null, updates: 0};
Object.defineProperty(window, "__aegisBenchmarkMetrics", {value: snapshot});
onLCP(({value}) => { snapshot.lcpMs = value; snapshot.updates += 1; }, {reportAllChanges: true});
onINP(({value}) => { snapshot.inpMs = value; snapshot.updates += 1; }, {reportAllChanges: true});
```

Authenticate the synthetic cookie jar through the existing CSRF/login endpoints before the measured full navigation to the folder URL; do not measure the login form's LCP and label it file-browser LCP. Add `signInRequest(request: APIRequestContext, username: string, password: string) -> Promise<void>` to the E2E auth helper for this setup. Start observers before that full folder navigation and wait for actual indexed rows. Record time to first usable file separately, with the same 2.5-second ceiling, so a quick skeleton cannot conceal a slow useful render. Unsupported/missing observations remain unmeasured, not zero.

The versioned journey lasts at least 30 minutes: traverse the full 50K-child folder forward in 100-entry pages, traverse back through evicted pages, then repeat nested navigation, all sorts, filter Apply/Cancel/clear, details open/close and scroll restoration for the remaining interval. Assert all 50K IDs were reached in each direction using a harness-owned bounded bitmap keyed by deterministic fixture ordinal; the application must not retain this bitmap or a full ID set. Include at least 100 discrete pointer/keyboard interactions for responsiveness measurement. Use a static dataset for exact traversal assertions and a separately labeled scan-active run for eventual-consistency behavior.

Check page-cache bounds in unit tests and heap/DOM behavior in the real browser. Grant revocation/logout/account-switch checks run at the end: previously visible content cannot return through delayed responses or history without a fresh authorized session. Report reference-profile measurements separately from ordinary CI functional runs and from unsupported WebKit performance fields.

- [ ] **Step 4: Verify green and record acceptance evidence.**

```bash
mobile_reports=$(mktemp -d /tmp/aegis-phase2a-mobile-reports.XXXXXXXX)
uv run --locked python -m scripts.benchmarks.run mobile --entries 1000000 --wide-folder 50000 --report-dir "$mobile_reports"
```

Require LCP ≤2.5 s, INP ≤200 ms and JS heap ≤250 MiB in the defined Chromium journey, plus mobile WebKit functional success. Include exact API/server workload reports from Tasks 15/16, cold/warm distinction, errors and sample counts. Do not hide failures behind averaged unloaded/loaded results.

- [ ] **Step 5: Review and commit.** Record sanitized mobile reports; commit `perf: verify bounded mobile browsing at fifty thousand entries`; push `origin main`.

## Task 18: Fresh-checkout acceptance and operator handoff

**Files:** Create `docs/operations/phase-2a-indexing.md`, `docs/verification/phase-2a.md`, and `tests/deployment/test_phase2a_upgrade.py`. Modify `README.md`, `docs/development.md`, this plan, and the Phase 2 umbrella delivery ledger. Preserve historical Phase 1 reports as historical evidence.

**Interfaces:** Produces tested activation/policy/rescan/degraded-root/restart/upgrade instructions and exact-revision acceptance evidence. The operator runbook never recommends deleting a database, rebuilding original storage, or volume-deleting shutdown to fix an indexing issue.

- [ ] **Step 1: Write the failing upgrade/recovery acceptance test and evidence checklist.**

```python
def test_phase1_upgrade_preserves_existing_state(owned_upgrade_stack):
    before = owned_upgrade_stack.seed_phase1_and_snapshot()
    owned_upgrade_stack.upgrade_to_checkout()
    after = owned_upgrade_stack.snapshot_existing_state()
    assert after == before
    assert owned_upgrade_stack.binding_is_current()
    assert owned_upgrade_stack.roles_match_allowlist()
    assert owned_upgrade_stack.can_browse_indexed_fixture()
    assert owned_upgrade_stack.original_manifest_unchanged()
```

Define `owned_upgrade_stack` in the test module using Task 15's owned resources and a separately built immutable Phase 1 source revision. Seed synthetic identities/roots/grants/audit/jobs, retain the same disposable database storage through the upgrade, and compare their stable fields afterward. Do not reuse the real application database or mutate a user's checkout. The test must reject a dirty/unidentified source build and preserve failure evidence.

- [ ] **Step 2: Verify red.** Run `uv run --locked python scripts/verify.py deployment --test-target tests/deployment/test_phase2a_upgrade.py`. Missing/mismatched binding or failed upgrade assertions are actionable failures; network/image setup errors are not passing upgrade evidence.

- [ ] **Step 3: Implement any acceptance integration corrections and write the runbook from exercised commands.** Documentation covers initial activation of already configured roots, default/overridden scan policy, read-only preflight/render/deployment, schema/binding installation, manual ROOT_ADMIN rescan, safe progress interpretation, missing/inaccessible states, recovering from reader/mount/database loss, and preserving logical metadata during reindexing. Explain that rescans are metadata operations, not physical file operations. A stuck unreaped reader requires an operator-controlled runtime/storage recovery, not repeated spawning or forced deletion.

The acceptance report records tested commit, lock/image/schema identities, migration and actual-role results, source-preservation proof, all test commands/counts/skips, query plans/budgets, 1M/50K catalog and physical scan evidence, mobile measurements, host calibration and cleanup disposition. Use separate columns for implemented, measured, reference-certified and remaining limitations. A design, test fixture or benchmark invocation alone cannot mark the package accepted.

```markdown
| Gate | Exact tested revision / report | Result | Limitation |
| --- | --- | --- | --- |
```

Populate the report table with measured results as the commands finish; never insert predicted passes. Update this task ledger and README from that evidence. `FILE-001` can become Verified only after its entire applicable API/scale gate; scanning/mobile/filter/metrics features spanning later packages retain partial status. Do not advance `MEDIA-001`, document editing, transfers, full event reconciliation, or authorized delivery. Milestone 2A still has required follow-up packages even after 2A.1 acceptance.

- [ ] **Step 4: Verify the complete package on an exact fresh checkout.** Commit the code checkpoint first, then create an isolated detached verification worktree using the required worktree workflow, install only tracked locks and run:

```bash
make verify
make verify-compose
make test-e2e
git diff --check
git status --short
```

Run the full catalog/filesystem/mobile benchmark modes on the exact code revision when the reference host is available; retain already collected provisional results separately. Check Linux CI for the same revision and report platform-specific skips honestly. If the calibrated reference host has not been exercised, keep reference certification and any dependent acceptance gate open; do not mark all 18 tasks complete merely because code exists or this session is ending.

- [ ] **Step 5: Review and commit.** Request the required implementation/code-quality review, resolve material findings with regression tests, and rerun affected checks before claiming completion. Commit the accurate runbook/ledger/evidence checkpoint as `docs: record indexed-browser verification and operating limits`, then push `origin main`. Report completed/remaining package tasks and still-required 2A work. No release tag, production deployment, original cleanup or operator-volume deletion is authorized by this step.

## Specification coverage and self-review

| Approved requirement | Delivering tasks | Acceptance evidence |
| --- | --- | --- |
| Package boundary and existing modular deployment | 1–4, 10, 18 | No added network service or content route; exact role/mount checks |
| Raw identity, deterministic names, source/logical separation | 1, 2, 5, 6 | SQL constraints, byte/property tests, override preservation |
| Root maintenance separate from user operations | 3, 4, 7, 10 | Actual-login function tests, current binding, live ROOT_ADMIN request |
| Bounded scans, independent heartbeat, checkpoints | 4–7, 16 | Real interrupted scans, lease/commit fence tests, aggregate process memory |
| Mount/error/newer-observation safety and convergence | 5–7, 14, 16 | No missing cascade, no stale commit, original manifest comparison |
| Authorized paged list/details/status/rescan endpoints | 8–10 | All error/CSRF/cache/foreign-ID/cursor/query-budget cases |
| Basic UI metadata filters, unknown values, literal prefixes | 8, 9, 11, 13, 14 | Typed parser and live Apply/Cancel/chip/filter journeys |
| Dark mobile virtual list and bounded private state | 11–14, 17 | Eviction/restoration, 44px controls, revocation, Chromium/WebKit |
| Database/deployment/audit integration | 2–7, 10, 14, 18 | Explicit allowlists, no original writes, unchanged actor authority, safe audit |
| Separate 1M catalog and physical filesystem fixtures | 15, 16 | Shape/count manifests, HTTP/query plans, real scan throughput and preservation |
| Reference workload and mobile performance limits | 15–18 | Cold/warm/scan-only labels, calibrated hardware, LCP/INP/heap evidence |
| README, task counts, runbooks and honest partial statuses | Every commit, 18 | Exact-revision ledger/evidence; no seven-milestones-equals-seven-tasks claim |

Before handing this plan to execution, review the table against all 13 focused-spec sections, scan for incomplete steps and inconsistent names, and verify local Markdown links. This plan's design choices refine implementation inside the approved boundaries; expanding original access, egress, delivery scope or acceptance budgets requires a separate explicit amendment.

## Primary implementation references

- [Python 3.13 filesystem names and directory iteration](https://docs.python.org/3.13/library/os.html#os.scandir) supplies the lossless byte/FD API contract used by Task 5.
- [PostgreSQL 18 row and advisory locking](https://www.postgresql.org/docs/18/explicit-locking.html) informs root-first write fences and concurrent shared browse locks; our concurrency tests establish the application behavior.
- [TanStack Query bounded infinite queries](https://tanstack.com/query/latest/docs/framework/react/guides/infinite-queries) supports the five-page request window in Task 11.
- [TanStack React Virtual adapter](https://tanstack.com/virtual/latest/docs/framework/react/react-virtual) supplies the virtualizer used by Task 12; actual memory tests remain mandatory.
- [Official web-vitals library](https://github.com/GoogleChrome/web-vitals) supplies benchmark-only LCP/INP measurement, following the documented [INP interaction/outlier semantics](https://web.dev/articles/inp).

## Execution handoff

The written specifications are approved. After this plan's inline self-review and documentation commit, offer task-by-task execution either with fresh implementer/reviewer agents or inline batches with checkpoints. Reuse the existing `main` workflow in either case. Implementation begins with Task 1, not with a full-v1 bulk change; task evidence and README counts advance only as work is actually delivered.
