"""Authorized media delivery: Django decides, the gateway sends the bytes (spec M6).

Every response is decided per request, after the same locked authorization as browsing:
the session's cache namespace (``n``) and the root's current epoch (``e``) must equal the
URL's, and derivatives also require the token of their source fingerprint (``f``), whose
fingerprint must also equal the catalog's current observation of the file. Any mismatch,
any missing permission and any unknown entry is the same ``404``. A successful answer is an
empty response with ``X-Accel-Redirect`` to an ``internal`` gateway alias; conditional
requests for derivatives are answered here with ``304`` and never redirected.
"""

from __future__ import annotations

import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Final, Literal
from uuid import UUID

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.http import HttpRequest, HttpResponse, HttpResponseNotModified
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django.views import View

from aegis_apps.catalog.api import _database_is_unavailable
from aegis_apps.catalog.authorization import (
    CatalogAuthenticationRequired,
    CatalogNotFound,
    CatalogUnavailable,
    entry_context,
)
from aegis_apps.common.database_privileges import (
    MEDIA_TOUCH_INTERVAL_SECONDS,
    current_database_login,
)
from aegis_apps.identity.models import User
from aegis_apps.identity.session_policy import cache_namespace, revoke_session
from aegis_apps.operations.selectors import SchemaCompatibilityError
from aegis_apps.roots.permissions import Permission

from . import selectors
from .disposition import content_disposition
from .enums import DerivativeKind, DerivativeState
from .formats import derivative_content_type, original_content_type
from .links import FINGERPRINT_TOKEN_LENGTH, fingerprint_token
from .models import MediaDerivative
from .paths import UnsafeRedirect, derivative_redirect, original_redirect

logger = logging.getLogger(__name__)

DERIVATIVE_CACHE_CONTROL: Final = "private, no-cache"
ORIGINAL_CACHE_CONTROL: Final = "private, no-store"
ERROR_CACHE_CONTROL: Final = "private, no-store"
MAX_QUERY_STRING_BYTES: Final = 512
MAX_IF_NONE_MATCH_BYTES: Final = 8192
MAX_EPOCH: Final = 2**63 - 1

_TOKEN_RE: Final = re.compile(rf"[0-9a-f]{{{FINGERPRINT_TOKEN_LENGTH}}}\Z", re.ASCII)
_EPOCH_START: Final = datetime(1970, 1, 1, tzinfo=UTC)
_NAMESPACE_RE: Final = re.compile(r"[0-9A-Za-z]{1,128}\Z", re.ASCII)
_EPOCH_RE: Final = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z", re.ASCII)
_DERIVATIVE_PARAMETERS: Final = frozenset({"f", "n", "e"})
_ORIGINAL_PARAMETERS: Final = frozenset({"d", "n", "e"})


class DeliveryDenied(Exception):
    """Any refusal; always answered as the same 404."""


def _private(response: HttpResponse, cache_control: str) -> HttpResponse:
    response["Cache-Control"] = cache_control
    return response


def _not_found() -> HttpResponse:
    response = HttpResponse(b"Not found\n", status=404, content_type="text/plain; charset=utf-8")
    return _private(response, ERROR_CACHE_CONTROL)


def _unavailable() -> HttpResponse:
    response = HttpResponse(
        b"Temporarily unavailable\n", status=503, content_type="text/plain; charset=utf-8",
    )
    return _private(response, ERROR_CACHE_CONTROL)


def _query(request: HttpRequest, allowed: frozenset[str]) -> dict[str, str]:
    raw = request.META.get("QUERY_STRING", "")
    if not isinstance(raw, str) or len(raw) > MAX_QUERY_STRING_BYTES:
        raise DeliveryDenied()
    if set(request.GET) != allowed:
        raise DeliveryDenied()
    values: dict[str, str] = {}
    for key in allowed:
        selected = request.GET.getlist(key)
        if len(selected) != 1 or not isinstance(selected[0], str):
            raise DeliveryDenied()
        values[key] = selected[0]
    return values


def _authenticated(request: HttpRequest) -> User:
    user = request.user
    if not isinstance(user, User) or not user.is_authenticated:
        raise DeliveryDenied()
    return user


def _require_namespace(request: HttpRequest, user: User, supplied: str) -> str:
    if _NAMESPACE_RE.fullmatch(supplied) is None:
        raise DeliveryDenied()
    try:
        expected = cache_namespace(session=request.session, user=user)
    except ValueError:
        raise DeliveryDenied() from None
    if not secrets.compare_digest(supplied.encode("ascii"), expected.encode("ascii")):
        raise DeliveryDenied()
    return expected


def _epoch(value: str) -> int:
    if _EPOCH_RE.fullmatch(value) is None:
        raise DeliveryDenied()
    epoch = int(value)
    if epoch > MAX_EPOCH:
        raise DeliveryDenied()
    return epoch


def strong_etag(derivative: selectors.ReadyDerivative) -> str:
    """The derivative key plus the published row's size and creation time.

    A derivative regenerated under the same key (after eviction) is a new publication, so
    it never shares a validator with the bytes it replaces.
    """
    created = derivative.created_at.astimezone(UTC) - _EPOCH_START
    microseconds = (created.days * 86_400 + created.seconds) * 1_000_000 + created.microseconds
    return f'"{derivative.key}.{derivative.size}.{microseconds}"'


def if_none_match_hits(header: object, etag: str) -> bool:
    """RFC 9110 If-None-Match with weak comparison against one strong ETag."""
    if not isinstance(header, str) or not header or len(header) > MAX_IF_NONE_MATCH_BYTES:
        return False
    value = header.strip()
    if value == "*":
        return True
    for candidate in value.split(","):
        tag = candidate.strip()
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def _touch_in_test_database(key: str) -> None:
    """The touch function's exact effect, for owner-login test databases without it."""
    now = timezone.now()
    candidate = (
        MediaDerivative.objects.select_for_update(skip_locked=True)
        .filter(
            key=key,
            evictable=True,
            state=DerivativeState.READY,
            last_access_at__lt=now - timedelta(seconds=MEDIA_TOUCH_INTERVAL_SECONDS),
        )
        .values_list("id", flat=True)
        .first()
    )
    if candidate is not None:
        MediaDerivative.objects.filter(pk=candidate).update(last_access_at=now)


def record_derivative_access(key: str) -> None:
    """Record an LRU access through ``aegis_touch_media_derivative`` (best effort).

    It runs after the authorization transaction, in its own transaction. Outside tests
    the web process always runs as ``aegis_web`` (checked at startup) and the function's
    EXECUTE grant enforces the boundary. Test databases that never synchronized the
    privilege boundary use the equivalent ORM update, like operation authorization checks.
    """
    try:
        with transaction.atomic():
            if settings.AEGIS_ENVIRONMENT != "test" or current_database_login() == "aegis_web":
                with connection.cursor() as cursor:
                    cursor.execute("SELECT public.aegis_touch_media_derivative(%s)", [key])
            else:
                _touch_in_test_database(key)
    except DatabaseError:
        logger.warning("Media derivative access was not recorded")


def _denied_response(request: HttpRequest, error: Exception) -> HttpResponse:
    if isinstance(error, CatalogAuthenticationRequired):
        revoke_session(request)
        return _not_found()
    if isinstance(error, (DeliveryDenied, CatalogNotFound, UnsafeRedirect)):
        return _not_found()
    if isinstance(error, (CatalogUnavailable, SchemaCompatibilityError)) or (
        _database_is_unavailable(error)
    ):
        return _unavailable()
    raise error


class DerivativeDeliveryView(View):
    http_method_names = ("get", "head")

    def get(self, request: HttpRequest, entry_id: UUID, kind: str) -> HttpResponse:
        try:
            if kind not in DerivativeKind.values:
                raise DeliveryDenied()
            user = _authenticated(request)
            values = _query(request, _DERIVATIVE_PARAMETERS)
            token = values["f"]
            if _TOKEN_RE.fullmatch(token) is None:
                raise DeliveryDenied()
            epoch = _epoch(values["e"])
            namespace = _require_namespace(request, user, values["n"])
            with entry_context(
                user, entry_id, namespace, required=Permission.PREVIEW,
            ) as context:
                if context.root_epoch != epoch:
                    raise DeliveryDenied()
                entry = selectors.entry_file(context, entry_id)
                if entry is None or not entry.available:
                    raise DeliveryDenied()
                derivative = selectors.ready_derivative(context, entry_id, kind)
                if derivative is None or not secrets.compare_digest(
                    fingerprint_token(derivative.fingerprint).encode("ascii"),
                    token.encode("ascii"),
                ):
                    raise DeliveryDenied()
                # The file changed since media discovery: its derivatives show the old
                # bytes until rediscovery, so the client falls back to its icon.
                if derivative.fingerprint != entry.source_fingerprint:
                    raise DeliveryDenied()
            redirect = derivative_redirect(derivative.key, derivative.extension)
            content_type = derivative_content_type(derivative.extension)
        except Exception as error:
            return _denied_response(request, error)
        record_derivative_access(derivative.key)
        etag = strong_etag(derivative)
        response: HttpResponse
        if if_none_match_hits(request.headers.get("If-None-Match"), etag):
            response = HttpResponseNotModified()
        else:
            response = HttpResponse(status=200, content_type=content_type)
            response["X-Accel-Redirect"] = redirect
        response["ETag"] = etag
        patch_vary_headers(response, ("Cookie",))
        return _private(response, DERIVATIVE_CACHE_CONTROL)


class OriginalDeliveryView(View):
    http_method_names = ("get", "head")

    def get(self, request: HttpRequest, entry_id: UUID) -> HttpResponse:
        try:
            user = _authenticated(request)
            values = _query(request, _ORIGINAL_PARAMETERS)
            disposition = values["d"]
            if disposition not in ("inline", "attachment"):
                raise DeliveryDenied()
            mode: Literal["inline", "attachment"] = (
                "inline" if disposition == "inline" else "attachment"
            )
            required = Permission.PREVIEW if mode == "inline" else Permission.EXPORT
            epoch = _epoch(values["e"])
            namespace = _require_namespace(request, user, values["n"])
            with entry_context(user, entry_id, namespace, required=required) as context:
                if context.root_epoch != epoch:
                    raise DeliveryDenied()
                entry = selectors.entry_file(context, entry_id)
                if entry is None or not entry.available:
                    raise DeliveryDenied()
                content_type = original_content_type(entry.type_hint, inline=mode == "inline")
                if content_type is None:
                    raise DeliveryDenied()
                names = selectors.source_names(context, entry_id)
                if names is None or names[-1] != entry.raw_name:
                    raise DeliveryDenied()
                redirect = original_redirect(context.root.slot_id, names)
            response = HttpResponse(status=200, content_type=content_type)
            response["X-Accel-Redirect"] = redirect
            response["Content-Disposition"] = content_disposition(mode, entry.raw_name)
            response["X-Content-Type-Options"] = "nosniff"
        except Exception as error:
            return _denied_response(request, error)
        return _private(response, ORIGINAL_CACHE_CONTROL)
