import uuid
from collections.abc import Callable

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.roots.models import Root
from django.db import IntegrityError, connection, transaction

pytestmark = pytest.mark.django_db(transaction=True)


def test_source_uniqueness_does_not_use_normalized_names(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry]
) -> None:
    first = entry_factory(root=catalog_root, raw=b"A.txt")
    second = entry_factory(root=catalog_root, raw=b"a.txt")
    assert first.pk != second.pk
    assert first.name_key == second.name_key
    with pytest.raises(IntegrityError), transaction.atomic():
        entry_factory(root=catalog_root, raw=b"A.txt")


def test_catalog_delete_is_disabled(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry]
) -> None:
    entry = entry_factory(root=catalog_root, raw=b"keep.txt")
    with pytest.raises(PermissionError):
        entry.delete()
    with pytest.raises(PermissionError):
        CatalogEntry.objects.filter(pk=entry.pk).delete()
    assert CatalogEntry.objects.filter(pk=entry.pk).exists()


@pytest.mark.parametrize("parent_field", ["source_parent_id", "logical_parent_id"])
def test_sql_rejects_cross_root_parent(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry], parent_field: str
) -> None:
    other_root = Root.objects.create(
        slot_id="synthetic-other",
        display_name="Synthetic other",
        mode="read_only",
        active=True,
    )
    other_anchor = CatalogEntry.objects.create(
        root=other_root,
        raw_name=b"",
        display_name="",
        name_key=b"",
        kind="directory",
    )
    entry = entry_factory(root=catalog_root, raw=b"child")
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE catalog_catalogentry SET {parent_field} = %s WHERE id = %s",
            [other_anchor.pk, entry.pk],
        )
        cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


@pytest.mark.parametrize("parent_field", ["source_parent_id", "logical_parent_id"])
def test_sql_rejects_self_parent(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry], parent_field: str
) -> None:
    entry = entry_factory(root=catalog_root, raw=b"child")
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE catalog_catalogentry SET {parent_field} = id WHERE id = %s",
            [entry.pk],
        )


def test_one_anchor_per_root(catalog_root: Root) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        CatalogEntry.objects.create(
            root=catalog_root,
            raw_name=b"",
            display_name="",
            name_key=b"",
            kind="directory",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("raw_name", None),
        ("raw_name", b""),
        ("raw_name", b"."),
        ("raw_name", b".."),
        ("raw_name", b"bad/name"),
        ("raw_name", b"bad\0name"),
        ("raw_name", b"a" * 256),
        ("name_key", b"a" * 2049),
        ("kind", "unknown"),
        ("source_state", "unknown"),
        ("size", -1),
        ("device", -1),
        ("inode", -1),
        ("source_revision", -1),
        ("catalog_version", -1),
        ("children_version", -1),
        ("observation_epoch", -1),
        ("seen_generation", -1),
        ("seen_attempt", -1),
    ],
)
def test_sql_rejects_invalid_child_metadata(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry], field: str,
    value: int | str | bytes | None,
) -> None:
    entry = entry_factory(root=catalog_root, raw=b"valid")
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE catalog_catalogentry SET {field} = %s WHERE id = %s", [value, entry.pk]
        )


@pytest.mark.parametrize("field,value", [("kind", "file"), ("raw_name", b"name")])
def test_sql_rejects_invalid_anchor(catalog_root: Root, field: str, value: str | bytes) -> None:
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE catalog_catalogentry SET {field} = %s WHERE root_id = %s",
            [value, catalog_root.pk],
        )


def test_source_location_is_parent_scoped(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry]
) -> None:
    folder = entry_factory(root=catalog_root, raw=b"folder", kind="directory")
    first = entry_factory(root=catalog_root, raw=b"same")
    second = entry_factory(root=catalog_root, raw=b"same", parent=folder)
    assert first.pk != second.pk


def test_precise_source_numbers_and_organization(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry]
) -> None:
    entry = entry_factory(
        root=catalog_root,
        raw=b"\xff.txt",
        size=18446744073709551615,
        mtime_ns=1730000000123456789,
        ctime_ns=-1234567890123456789,
        device=18446744073709551615,
        inode=18446744073709551615,
        logical_name="User title",
        source_revision=7,
        catalog_version=8,
        children_version=9,
        observation_epoch=10,
        seen_generation=11,
        seen_attempt=12,
    )
    entry.refresh_from_db()
    assert bytes(entry.raw_name) == b"\xff.txt"
    assert entry.display_name == r"\xff.txt"
    assert entry.logical_name == "User title"
    assert entry.size == 18446744073709551615
    assert entry.mtime_ns == 1730000000123456789
    assert entry.ctime_ns == -1234567890123456789
    assert entry.device == entry.inode == 18446744073709551615
    assert (
        entry.source_revision,
        entry.catalog_version,
        entry.children_version,
        entry.observation_epoch,
        entry.seen_generation,
        entry.seen_attempt,
    ) == (7, 8, 9, 10, 11, 12)


def test_root_and_parent_sql_deletion_are_protected(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry]
) -> None:
    entry_factory(root=catalog_root, raw=b"keep")
    with pytest.raises(PermissionError):
        catalog_root.delete()
    for statement in (
        "DELETE FROM roots_root WHERE id = %s",
        "DELETE FROM catalog_catalogentry WHERE root_id = %s AND source_parent_id IS NULL",
    ):
        with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(statement, [catalog_root.pk])
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_composite_parents_are_deferred(
    catalog_root: Root, entry_factory: Callable[..., CatalogEntry]
) -> None:
    entry = entry_factory(root=catalog_root, raw=b"child")
    new_id = uuid.uuid4()
    with transaction.atomic():
        CatalogEntry.objects.filter(pk=entry.pk).update(
            source_parent_id=new_id, logical_parent_id=new_id
        )
        entry_factory(root=catalog_root, raw=b"later", kind="directory", id=new_id)
    entry.refresh_from_db()
    assert entry.source_parent_id == entry.logical_parent_id == new_id
