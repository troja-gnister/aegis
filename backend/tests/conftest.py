import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from aegis_apps.catalog.domain import DirectoryIdentity, EntryKind, Observation, SourceState
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.catalog.names import source_name
from aegis_apps.identity.models import User
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


class BrowseFixture:
    """Synthetic catalog and request principal; queries always use the public interface."""

    def __init__(self, root: Root, user: User, factory: Callable[..., CatalogEntry]) -> None:
        self.root, self.user, self.factory = root, user, factory
        self.anchor = CatalogEntry.objects.get(root=root, source_parent__isnull=True)
        self.namespace = "a" * 40
        self.statements: list[str] = []

    def entry(self, raw: bytes = b"item", **kwargs: Any) -> CatalogEntry:
        entry = self.factory(root=self.root, raw=raw, **kwargs)
        assert entry.source_parent is not None
        CatalogEntry.objects.filter(pk=entry.pk).update(
            source_parent_revision=entry.source_parent.source_revision,
        )
        entry.refresh_from_db()
        return entry

    def page(self, **kwargs: Any) -> dict[str, Any]:
        from aegis_apps.catalog.authorization import browse_context
        from aegis_apps.catalog.filters import FileFilter
        from aegis_apps.catalog.queries import directory_page
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        options: dict[str, Any] = dict(
            parent_id=None, filters=FileFilter(), sort="name", order="asc", limit=100, cursor=None,
        )
        options.update(kwargs)
        with (
            CaptureQueriesContext(connection) as captured,
            browse_context(self.user, self.root.pk, self.namespace) as context,
        ):
            page = directory_page(context, **options)
        self.statements.extend(query["sql"] for query in captured)
        return page

    def details(self, entry_id: uuid.UUID) -> dict[str, Any]:
        from aegis_apps.catalog.queries import entry_details

        return entry_details(self.user, entry_id, self.namespace)

    def seed_ties(self, count: int) -> None:
        entries = []
        for index in range(count):
            name = source_name(f"{index:04d}.txt".encode())
            entries.append(CatalogEntry(
                root=self.root, source_parent=self.anchor, logical_parent=self.anchor,
                source_parent_revision=self.anchor.source_revision,
                raw_name=name.raw, display_name=name.display, name_key=name.order_key,
                type_hint=name.type_hint, size=None if index % 5 == 0 else index % 3,
                mtime_ns=None if index % 7 == 0 else index % 3,
                kind=EntryKind.DIRECTORY if index % 4 == 0 else EntryKind.FILE,
            ))
        CatalogEntry.objects.bulk_create(entries)

    def walk(self, **kwargs: Any) -> list[dict[str, Any]]:
        pages = [self.page(**kwargs)]
        while pages[-1]["nextCursor"]:
            pages.append(self.page(cursor=pages[-1]["nextCursor"], **kwargs))
            assert len(pages) < 100
        return pages


@pytest.fixture
def browse_fixture(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry],
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> BrowseFixture:
    from aegis_apps.indexing.models import IndexDeployment
    from aegis_apps.roots.models import RootGrant

    path = tmp_path / "browse-manifest.json"
    raw = json.dumps({
        "version": 1, "generatedAt": "2026-09-15T12:00:00Z", "slots": [{
            "slotId": catalog_root.slot_id,
            "containerPath": f"/srv/aegis/roots/{catalog_root.slot_id}",
            "mode": "read_only", "filesystemId": 100, "rootInode": 200,
            "expectedIdentity": "remote:synthetic.invalid:/browse",
            "mountFingerprint": "a" * 64,
        }],
    }).encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST", str(path))
    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", digest)
    IndexDeployment.objects.create(
        manifest_identity=digest, slot_ids=[catalog_root.slot_id],
        interval_seconds=3600, idle_timeout_seconds=120, batch_records=500, readers=1,
    )
    user = User.objects.create_user(username=f"browse-{uuid.uuid4().hex}")
    RootGrant.objects.create(root=catalog_root, user=user, permissions=1)
    return BrowseFixture(catalog_root, user, entry_factory)
