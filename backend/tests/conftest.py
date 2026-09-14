import uuid
from collections.abc import Callable
from datetime import datetime

import pytest
from aegis_apps.catalog.domain import EntryKind, SourceState
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.catalog.names import source_name
from aegis_apps.roots.models import Root


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
