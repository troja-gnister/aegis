"""Delivery URLs and the compact entry ``media`` object (API contract, spec M6).

Every URL carries the session's cache namespace and the root's authorization epoch, so a
browser cache entry can never be reused by another session or after a grant change.
Derivative URLs also carry a fingerprint token: a keyed digest of the source fingerprint
the derivative was built from. The fingerprint itself hashes the file's device and inode,
which a browsing user must not be able to recover offline, so it never leaves the server;
the token is all clients see (also as the details ``fingerprint`` field).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Final, Literal
from uuid import UUID

from django.db.models import F
from django.utils.crypto import salted_hmac

from aegis_apps.catalog.domain import EntryKind

from .enums import DerivativeKind, MediaKind, SortSource, StageState
from .keys import is_hex_digest

DELIVERY_PREFIX: Final = "/media/v1"
MAX_NAMESPACE_LENGTH: Final = 128
MAX_EPOCH: Final = 2**63 - 1
FINGERPRINT_TOKEN_SALT: Final = "aegis.media.fingerprint-token.v1"
FINGERPRINT_TOKEN_LENGTH: Final = 32

type Disposition = Literal["inline", "attachment"]

# Annotation aliases for the LEFT JOIN of an entry query on its media asset (the asset's
# primary key is the entry, so the join never multiplies rows). Values are selected by
# alias only, so no source identity column of the asset is ever projected.
MEDIA_ANNOTATIONS: Final[Mapping[str, F]] = MappingProxyType({
    "_media_kind": F("media_asset__kind"),
    "_media_thumbnail": F("media_asset__thumbnail_state"),
    "_media_sort_at": F("media_asset__sort_at"),
    "_media_sort_source": F("media_asset__sort_source"),
    "_media_width": F("media_asset__width"),
    "_media_height": F("media_asset__height"),
    "_media_duration_ms": F("media_asset__duration_ms"),
    "_media_fingerprint": F("media_asset__fingerprint"),
})
MEDIA_FIELDS: Final = tuple(MEDIA_ANNOTATIONS)


@dataclass(frozen=True, slots=True)
class MediaLinks:
    """What the current caller may receive as URLs for one root."""

    namespace: str
    root_epoch: int
    preview: bool
    export: bool


def _require_url_inputs(entry_id: UUID, namespace: str, root_epoch: int) -> None:
    if type(entry_id) is not UUID:
        raise ValueError("invalid media URL input")
    if (
        not isinstance(namespace, str)
        or not 1 <= len(namespace) <= MAX_NAMESPACE_LENGTH
        or not namespace.isascii()
        or not namespace.isalnum()
    ):
        raise ValueError("invalid media URL input")
    if type(root_epoch) is not int or not 0 <= root_epoch <= MAX_EPOCH:
        raise ValueError("invalid media URL input")


def fingerprint_token(fingerprint: str) -> str:
    """The client-visible token of a source fingerprint (32 hex, keyed by SECRET_KEY)."""
    if not is_hex_digest(fingerprint):
        raise ValueError("invalid source fingerprint")
    digest = salted_hmac(FINGERPRINT_TOKEN_SALT, fingerprint, algorithm="sha256").hexdigest()
    return digest[:FINGERPRINT_TOKEN_LENGTH]


def derivative_url(
    entry_id: UUID, kind: str, fingerprint: str, *, namespace: str, root_epoch: int,
) -> str:
    """The delivery URL of a derivative built from the source *fingerprint*."""
    _require_url_inputs(entry_id, namespace, root_epoch)
    if kind not in DerivativeKind.values or not is_hex_digest(fingerprint):
        raise ValueError("invalid media URL input")
    return (
        f"{DELIVERY_PREFIX}/derivatives/{entry_id}/{kind}"
        f"?f={fingerprint_token(fingerprint)}&n={namespace}&e={root_epoch}"
    )


def original_url(
    entry_id: UUID, disposition: Disposition, *, namespace: str, root_epoch: int,
) -> str:
    _require_url_inputs(entry_id, namespace, root_epoch)
    if disposition not in ("inline", "attachment"):
        raise ValueError("invalid media URL input")
    return (
        f"{DELIVERY_PREFIX}/originals/{entry_id}"
        f"?d={disposition}&n={namespace}&e={root_epoch}"
    )


def download_url(
    entry_id: UUID, *, kind: object, available: bool, links: MediaLinks,
) -> str | None:
    """The attachment URL of an original, or ``None``.

    Every regular file may be downloaded with EXPORT while it is available (present, with
    current source and navigation chains); directories, links and special entries never.
    Entry details and media details issue this same URL. It names no file version: each
    request is authorized again against the current catalog, and an interrupted download
    resumes through the gateway's validators (spec M6).
    """
    if kind != EntryKind.FILE or available is not True or not links.export:
        return None
    return original_url(
        entry_id, "attachment", namespace=links.namespace, root_epoch=links.root_epoch,
    )


def rfc3339_utc(value: datetime) -> str:
    """An aware timestamp as RFC 3339 in UTC with a ``Z`` designator."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive timestamp")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def entry_media(
    row: Mapping[str, object], *, available: bool, links: MediaLinks,
) -> dict[str, object] | None:
    """The contract's ``media`` object for one joined entry row, or ``None``.

    ``thumbnailUrl`` is set only when the thumbnail is ready, the entry is available
    (present with a current source chain) and the caller has PREVIEW.
    """
    kind = row.get("_media_kind")
    if kind is None:
        return None
    if kind not in MediaKind.values:
        raise ValueError("invalid media kind")
    thumbnail = row["_media_thumbnail"]
    sort_at = row["_media_sort_at"]
    sort_source = row["_media_sort_source"]
    fingerprint = row["_media_fingerprint"]
    entry_id = row["id"]
    if (
        thumbnail not in StageState.values
        or sort_source not in SortSource.values
        or not isinstance(sort_at, datetime)
        or not isinstance(fingerprint, str)
        or type(entry_id) is not UUID
    ):
        raise ValueError("invalid media row")
    url = None
    if thumbnail == StageState.READY and available and links.preview:
        url = derivative_url(
            entry_id, DerivativeKind.THUMBNAIL, fingerprint,
            namespace=links.namespace, root_epoch=links.root_epoch,
        )
    return {
        "kind": kind,
        "thumbnail": thumbnail,
        "sortDate": rfc3339_utc(sort_at),
        "sortDateSource": sort_source,
        "width": row["_media_width"],
        "height": row["_media_height"],
        "durationMs": row["_media_duration_ms"],
        "thumbnailUrl": url,
    }
