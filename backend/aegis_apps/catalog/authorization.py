"""Transaction-scoped browser authorization, using the foundation lock order."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

from django.contrib.postgres.aggregates import BitOr
from django.db import OperationalError, connection, transaction
from django.db.models import Q

from aegis_apps.identity.models import User
from aegis_apps.indexing.models import IndexDeployment
from aegis_apps.roots.manifest import ManifestError, configured_manifest
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission


class CatalogNotFound(RuntimeError):
    def __init__(self) -> None:
        super().__init__("catalog_not_found")


class CatalogNotReady(RuntimeError):
    def __init__(self) -> None:
        super().__init__("catalog_not_ready")


class CatalogUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("catalog_unavailable")


class CatalogAuthenticationRequired(RuntimeError):
    def __init__(self) -> None:
        super().__init__("authentication_required")


@dataclass(slots=True)
class BrowseContext:
    root: Root
    user: User
    root_epoch: int
    user_epoch: int
    namespace: str
    _connection: object
    _active: bool = True

    def require_active(self) -> None:
        if not self._active or not connection.in_atomic_block or (
            self._connection is not connection.connection
        ):
            raise CatalogNotFound()


@contextmanager
def bounded_read() -> Iterator[None]:
    """Limit database waits, exposing only recognized timeout failures."""
    try:
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '5s'")
                cursor.execute("SET LOCAL lock_timeout = '1s'")
            yield
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlstate", None) in ("57014", "55P03"):
            raise CatalogUnavailable() from None
        raise


@contextmanager
def browse_context(user: User, root_id: UUID, namespace: str) -> Iterator[BrowseContext]:
    # Capture before waiting for locks. Never replace the request's identity epoch.
    requested_epoch = user.authorization_epoch
    with bounded_read():
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT id, slot_id, active, authorization_epoch FROM roots_root '
                'WHERE id = %s FOR SHARE', [root_id],
            )
            root_row = cursor.fetchone()
            if root_row is None or not root_row[2]:
                raise CatalogNotFound()
            cursor.execute(
                'SELECT id, is_active, authorization_epoch FROM identity_user '
                'WHERE id = %s FOR SHARE', [user.pk],
            )
            user_row = cursor.fetchone()
        if user_row is None or not user_row[1] or user_row[2] != requested_epoch:
            raise CatalogAuthenticationRequired()
        try:
            manifest = configured_manifest()
        except ManifestError:
            raise CatalogNotFound() from None
        binding = IndexDeployment.objects.filter(pk=1).values(
            "manifest_identity", "slot_ids",
        ).first()
        if (
            manifest is None or manifest.get(root_row[1]) is None or binding is None
            or binding["manifest_identity"] != manifest.digest
            or binding["slot_ids"] != sorted(manifest.slots)
        ):
            raise CatalogNotFound()
        mask = RootGrant.objects.filter(root_id=root_id).filter(
            Q(user_id=user.pk) | Q(group__user=user.pk),
        ).aggregate(mask=BitOr("permissions"))["mask"]
        if not (mask or 0) & int(Permission.BROWSE):
            raise CatalogNotFound()
        context = BrowseContext(
            Root(id=root_row[0], slot_id=root_row[1], active=True,
                 authorization_epoch=root_row[3]),
            User(id=user_row[0], is_active=True, authorization_epoch=user_row[2]),
            root_row[3], user_row[2], namespace, connection.connection,
        )
        try:
            yield context
        finally:
            context._active = False
