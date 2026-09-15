import uuid
from collections.abc import Callable
from datetime import datetime

import pytest
from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind, Observation, SourceState
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.catalog.names import source_name
from aegis_apps.indexing.checkpoints import BatchResult, FinalizeResult
from aegis_apps.indexing.database import ScanLease
from aegis_apps.indexing.protocol import ReaderBatch, ReaderComplete
from aegis_apps.roots.models import Root

from tests.support.database_roles import RoleDatabase


@pytest.fixture
def catalog_root() -> Root:
    root = Root.objects.create(
        slot_id=f"synthetic-{uuid.uuid4().hex}",
        display_name="Synthetic catalog",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    CatalogEntry.objects.create(
        root=root,
        raw_name=b"",
        display_name="",
        name_key=b"",
        kind=EntryKind.DIRECTORY,
    )
    return root


@pytest.fixture
def entry_factory() -> Callable[..., CatalogEntry]:
    def create(
        *,
        root: Root,
        raw: bytes,
        parent: CatalogEntry | None = None,
        kind: str = EntryKind.FILE,
        state: str = SourceState.PRESENT,
        size: int | None = None,
        mtime_ns: int | None = None,
        ctime_ns: int | None = None,
        device: int | None = None,
        inode: int | None = None,
        logical_name: str | None = None,
        logical_parent: CatalogEntry | None = None,
        source_revision: int = 0,
        catalog_version: int = 0,
        children_version: int = 0,
        observation_epoch: int = 0,
        seen_generation: int = 0,
        seen_attempt: int = 0,
        observed_at: datetime | None = None,
        id: uuid.UUID | None = None,
    ) -> CatalogEntry:
        name = source_name(raw)
        parent = parent or CatalogEntry.objects.get(root=root, source_parent__isnull=True)
        return CatalogEntry.objects.create(
            id=id or uuid.uuid4(),
            root=root,
            source_parent=parent,
            logical_parent=logical_parent or parent,
            raw_name=name.raw,
            display_name=name.display,
            name_key=name.order_key,
            type_hint=name.type_hint,
            kind=kind,
            source_state=state,
            size=size,
            mtime_ns=mtime_ns,
            ctime_ns=ctime_ns,
            device=device,
            inode=inode,
            logical_name=logical_name,
            source_revision=source_revision,
            catalog_version=catalog_version,
            children_version=children_version,
            observation_epoch=observation_epoch,
            seen_generation=seen_generation,
            seen_attempt=seen_attempt,
            observed_at=observed_at,
        )

    return create


class ScanFixture:
    def __init__(self, database: RoleDatabase) -> None:
        from aegis_apps.indexing.scheduling import schedule_root_scan

        from tests.deployment.test_database_roles import _create_scan_fixture

        self.database = database
        with database.as_django_role("aegis_migrator"):
            self.root, self.worker, digest = _create_scan_fixture()
        with database.as_django_role("aegis_indexer"):
            run_id = schedule_root_scan(self.root.pk, self.worker, digest)
        assert run_id is not None
        self.run_id = run_id

    def claim(self) -> ScanLease:
        from aegis_apps.indexing.database import claim_directory

        with self.database.as_django_role("aegis_indexer"):
            lease = claim_directory(self.run_id, self.worker)
        assert lease is not None
        return lease

    def expire(self, lease: ScanLease) -> None:
        with self.database.connect("aegis_migrator") as caller:
            caller.execute(
                "UPDATE public.indexing_directorywork SET lease_expires_at="
                "clock_timestamp()-interval '30 seconds' WHERE id=%s", [lease.work_id],
            )

    def batch(self, raw: bytes, sequence: int = 1, *, inode: int = 2) -> ReaderBatch:
        return ReaderBatch(sequence, (Observation(
            source_name(raw), EntryKind.FILE, SourceState.PRESENT, 12, 100, 100, 1, inode,
        ),))

    def record(self, lease: ScanLease, batch: ReaderBatch) -> BatchResult:
        from aegis_apps.indexing.checkpoints import record_batch

        with self.database.as_django_role("aegis_indexer"):
            return record_batch(lease, batch)

    def observe(self, lease: ScanLease, raw: bytes, sequence: int = 1) -> BatchResult | None:
        result = self.record(lease, self.batch(raw, sequence))
        return result if result.observed else None

    def seal(self, lease: ScanLease) -> bool:
        from aegis_apps.indexing.checkpoints import seal_directory

        with self.database.as_django_role("aegis_indexer"):
            return seal_directory(lease, ReaderComplete(DirectoryIdentity(1, 1, 100, 100)))

    def finalize(self, lease: ScanLease, limit: int = 500) -> FinalizeResult:
        from aegis_apps.indexing.checkpoints import finalize_directory

        with self.database.as_django_role("aegis_indexer"):
            return finalize_directory(lease, limit)

    def fail(self, lease: ScanLease, code: str) -> bool:
        from aegis_apps.indexing.checkpoints import fail_directory

        with self.database.as_django_role("aegis_indexer"):
            return fail_directory(lease, code)


@pytest.fixture
def scan_fixture(role_database: RoleDatabase) -> ScanFixture:
    return ScanFixture(role_database)
