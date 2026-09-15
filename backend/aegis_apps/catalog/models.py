from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models
from django.db.models.lookups import Exact, GreaterThan, LessThanOrEqual

from aegis_apps.roots.models import Root

from .domain import EntryKind, SourceState


class CatalogEntryQuerySet(models.QuerySet["CatalogEntry"]):
    def delete(self) -> tuple[int, dict[str, int]]:
        raise PermissionError("catalog deletion is disabled")


class CatalogEntry(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    root = models.ForeignKey(Root, on_delete=models.PROTECT)
    source_parent = models.ForeignKey(
        "self",
        null=True,
        on_delete=models.PROTECT,
        related_name="source_children",
    )
    logical_parent = models.ForeignKey(
        "self",
        null=True,
        on_delete=models.PROTECT,
        related_name="logical_children",
    )
    raw_name = models.BinaryField(max_length=255)
    display_name = models.TextField()
    name_key = models.BinaryField(max_length=2048)
    logical_name = models.TextField(null=True)
    kind = models.CharField(max_length=16, choices=[(kind.value, kind.value) for kind in EntryKind])
    type_hint = models.CharField(max_length=16, null=True)
    source_state = models.CharField(
        max_length=16,
        default=SourceState.PRESENT,
        choices=[(state.value, state.value) for state in SourceState],
    )
    size = models.DecimalField(max_digits=20, decimal_places=0, null=True)
    mtime_ns = models.DecimalField(max_digits=30, decimal_places=0, null=True)
    ctime_ns = models.DecimalField(max_digits=30, decimal_places=0, null=True)
    device = models.DecimalField(max_digits=20, decimal_places=0, null=True)
    inode = models.DecimalField(max_digits=20, decimal_places=0, null=True)
    source_revision = models.PositiveBigIntegerField(default=0)
    source_parent_revision = models.PositiveBigIntegerField(null=True)
    catalog_version = models.PositiveBigIntegerField(default=0)
    children_version = models.PositiveBigIntegerField(default=0)
    observation_epoch = models.PositiveBigIntegerField(default=0)
    seen_generation = models.PositiveBigIntegerField(default=0)
    seen_attempt = models.PositiveBigIntegerField(default=0)
    observed_at = models.DateTimeField(null=True)

    objects = CatalogEntryQuerySet.as_manager()

    class Meta:
        base_manager_name = "objects"
        default_manager_name = "objects"
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(fields=("root", "id"), name="catalog_root_id_uniq"),
            models.UniqueConstraint(
                fields=("root",),
                condition=models.Q(source_parent__isnull=True),
                name="catalog_one_anchor_per_root",
            ),
            models.UniqueConstraint(
                fields=("root", "source_parent", "raw_name"),
                condition=models.Q(source_parent__isnull=False),
                name="catalog_source_location_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(source_parent__isnull=True, raw_name=b"", kind=EntryKind.DIRECTORY)
                    | (
                        models.Q(source_parent__isnull=False)
                        & models.Q(
                            GreaterThan(
                                models.Func(
                                    "raw_name",
                                    function="octet_length",
                                    output_field=models.IntegerField(),
                                ),
                                0,
                            )
                        )
                    )
                ),
                name="catalog_anchor_or_child",
            ),
            models.CheckConstraint(
                condition=(
                    LessThanOrEqual(
                        models.Func(
                            "raw_name", function="octet_length", output_field=models.IntegerField()
                        ),
                        255,
                    )
                    & ~models.Q(raw_name__in=(b".", b".."))
                    & Exact(
                        models.Func(
                            "raw_name",
                            template="position(decode('00', 'hex') in %(expressions)s)",
                            output_field=models.IntegerField(),
                        ),
                        0,
                    )
                    & Exact(
                        models.Func(
                            "raw_name",
                            template="position(decode('2f', 'hex') in %(expressions)s)",
                            output_field=models.IntegerField(),
                        ),
                        0,
                    )
                ),
                name="catalog_raw_name_valid",
            ),
            models.CheckConstraint(
                condition=LessThanOrEqual(
                    models.Func(
                        "name_key", function="octet_length", output_field=models.IntegerField()
                    ),
                    2048,
                ),
                name="catalog_name_key_bounded",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=tuple(kind.value for kind in EntryKind)),
                name="catalog_kind_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(source_state__in=tuple(state.value for state in SourceState)),
                name="catalog_source_state_valid",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_parent=models.F("id")), name="catalog_source_not_self"
            ),
            models.CheckConstraint(
                condition=~models.Q(logical_parent=models.F("id")), name="catalog_logical_not_self"
            ),
            *[
                models.CheckConstraint(
                    condition=models.Q(**{f"{field}__gte": 0}), name=f"catalog_{field}_nonnegative"
                )
                for field in ("size", "device", "inode")
            ],
        ]

    def delete(self, *args: object, **kwargs: object) -> tuple[int, dict[str, int]]:
        raise PermissionError("catalog deletion is disabled")
