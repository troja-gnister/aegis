"""Private HTTP transport for bounded catalog metadata and scan requests."""

from __future__ import annotations

import json
import re
from typing import Any, TypedDict
from uuid import UUID

from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.authentication import SessionAuthentication
from rest_framework.renderers import JSONRenderer
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from aegis_apps.common.middleware import REQUEST_ID
from aegis_apps.identity.api import AUTHENTICATION_REQUIRED
from aegis_apps.identity.models import User
from aegis_apps.identity.session_policy import cache_namespace, revoke_session
from aegis_apps.indexing import selectors as indexing_selectors
from aegis_apps.indexing import services as indexing_services
from aegis_apps.indexing.database import ScanRequestThrottled
from aegis_apps.operations.selectors import SchemaCompatibilityError

from . import authorization, queries
from .authorization import (
    CatalogAuthenticationRequired,
    CatalogNotFound,
    CatalogNotReady,
    CatalogUnavailable,
)
from .cursors import MAX_CURSOR_BYTES, CursorRestartRequired
from .filters import MAX_FILTER_BYTES, FileFilter, parse_filters

CATALOG_NOT_FOUND = {"type": "catalog_not_found", "title": "Catalog item not found"}
CATALOG_NOT_READY = {"type": "catalog_not_ready", "title": "Catalog not ready"}
CURSOR_RESTART_REQUIRED = {
    "type": "cursor_restart_required",
    "title": "Catalog view changed",
}
INVALID_CATALOG_QUERY = {
    "type": "invalid_catalog_query",
    "title": "Invalid catalog query",
}
CATALOG_UNAVAILABLE = {"type": "catalog_unavailable", "title": "Catalog unavailable"}
SCAN_REQUEST_DENIED = {"type": "scan_request_denied", "title": "Scan request denied"}

_ALLOWED_DIRECTORY_PARAMETERS = frozenset(
    {"filters", "sort", "order", "limit", "parent", "cursor"}
)
_INTEGER = re.compile(r"[1-9][0-9]{0,2}\Z", re.ASCII)
_MAX_QUERY_STRING_BYTES = 32_768


class DirectoryInput(TypedDict):
    parent_id: UUID | None
    filters: FileFilter
    sort: str
    order: str
    limit: int
    cursor: str | None


def _response(data: Any = None, *, status: int = 200) -> Response:
    response = Response(data, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


def _user(request: Request) -> User | None:
    user = request.user
    return user if isinstance(user, User) and user.is_authenticated else None


def _revoke_stale_request(request: Request) -> Response:
    django_request: HttpRequest = request._request
    revoke_session(django_request)
    request.user = AnonymousUser()
    return _response(AUTHENTICATION_REQUIRED, status=401)


def _query_values(request: Request, *, allowed: frozenset[str]) -> dict[str, str]:
    raw = request.META.get("QUERY_STRING", "")
    if not isinstance(raw, str) or len(raw.encode("latin-1")) > _MAX_QUERY_STRING_BYTES:
        raise ValueError("invalid_catalog_query")
    if len(request.query_params) > len(allowed):
        raise ValueError("invalid_catalog_query")
    values: dict[str, str] = {}
    for key, selected in request.query_params.lists():
        if key not in allowed or len(selected) != 1:
            raise ValueError("invalid_catalog_query")
        value = selected[0]
        if not isinstance(value, str):
            raise ValueError("invalid_catalog_query")
        values[key] = value
    return values


def _directory_input(request: Request) -> DirectoryInput:
    values = _query_values(request, allowed=_ALLOWED_DIRECTORY_PARAMETERS)
    encoded_filters = values.get("filters")
    if encoded_filters is None:
        filters = FileFilter()
    else:
        if len(encoded_filters.encode("utf-8")) > MAX_FILTER_BYTES:
            raise ValueError("invalid_catalog_query")
        try:
            filters = parse_filters(json.loads(encoded_filters))
        except (json.JSONDecodeError, UnicodeError, RecursionError, ValueError):
            raise ValueError("invalid_catalog_query") from None
    sort = values.get("sort", "name")
    order = values.get("order", "asc")
    if sort not in ("name", "modified", "size") or order not in ("asc", "desc"):
        raise ValueError("invalid_catalog_query")
    encoded_limit = values.get("limit", "100")
    if _INTEGER.fullmatch(encoded_limit) is None:
        raise ValueError("invalid_catalog_query")
    limit = int(encoded_limit)
    if limit > 250:
        raise ValueError("invalid_catalog_query")
    parent: UUID | None = None
    if "parent" in values:
        try:
            parent = UUID(values["parent"])
        except (ValueError, AttributeError):
            raise ValueError("invalid_catalog_query") from None
        if str(parent) != values["parent"]:
            raise ValueError("invalid_catalog_query")
    cursor = values.get("cursor")
    if cursor is not None and (
        len(cursor) > MAX_CURSOR_BYTES or not cursor.isascii()
    ):
        raise CursorRestartRequired()
    return {
        "parent_id": parent,
        "filters": filters,
        "sort": sort,
        "order": order,
        "limit": limit,
        "cursor": cursor,
    }


def _scan_request_is_empty(request: Request) -> bool:
    if request.query_params:
        return False
    length = request.META.get("CONTENT_LENGTH", "")
    if length not in ("", "0", None):
        return False
    if request.META.get("HTTP_TRANSFER_ENCODING"):
        return False
    return not request._request.body


class CatalogAPIView(APIView):
    authentication_classes = (SessionAuthentication,)
    permission_classes = ()
    renderer_classes = (JSONRenderer,)

    def finalize_response(
        self,
        request: Request,
        response: Response,
        *args: Any,
        **kwargs: Any,
    ) -> Response:
        finalized = super().finalize_response(request, response, *args, **kwargs)
        finalized["Cache-Control"] = "private, no-store"
        return finalized

    def _catalog_error(self, request: Request, error: Exception) -> Response:
        if isinstance(error, CatalogAuthenticationRequired):
            return _revoke_stale_request(request)
        if isinstance(error, CatalogNotFound):
            return _response(CATALOG_NOT_FOUND, status=404)
        if isinstance(error, CatalogNotReady):
            response = _response(CATALOG_NOT_READY, status=503)
            response["Retry-After"] = "3"
            return response
        if isinstance(error, CursorRestartRequired):
            return _response(CURSOR_RESTART_REQUIRED, status=409)
        if isinstance(error, (CatalogUnavailable, SchemaCompatibilityError)):
            return _response(CATALOG_UNAVAILABLE, status=503)
        if isinstance(error, ValueError):
            return _response(INVALID_CATALOG_QUERY, status=400)
        raise error


class DirectoryListView(CatalogAPIView):
    def get(self, request: Request, root_id: UUID) -> Response:
        user = _user(request)
        if user is None:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        namespace = cache_namespace(session=request.session, user=user)
        try:
            with authorization.browse_context(user, root_id, namespace) as context:
                inputs = _directory_input(request)
                payload = queries.directory_page(context, **inputs)
        except Exception as error:
            return self._catalog_error(request, error)
        return _response(payload)


class EntryDetailView(CatalogAPIView):
    def get(self, request: Request, entry_id: UUID) -> Response:
        user = _user(request)
        if user is None:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        namespace = cache_namespace(session=request.session, user=user)
        try:
            payload = queries.entry_details(user, entry_id, namespace)
            _query_values(request, allowed=frozenset())
        except Exception as error:
            return self._catalog_error(request, error)
        return _response(payload)


class IndexStatusView(CatalogAPIView):
    def get(self, request: Request, root_id: UUID) -> Response:
        user = _user(request)
        if user is None:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        try:
            payload = indexing_selectors.index_status(user, root_id)
            _query_values(request, allowed=frozenset())
        except Exception as error:
            return self._catalog_error(request, error)
        return _response(payload)


@method_decorator(csrf_protect, name="dispatch")
class RootScanView(CatalogAPIView):
    def post(self, request: Request, root_id: UUID) -> Response:
        user = _user(request)
        if user is None:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        request_id = request.headers.get("X-Request-ID", "")
        if REQUEST_ID.fullmatch(request_id) is None or not _scan_request_is_empty(request):
            return _response(SCAN_REQUEST_DENIED, status=400)
        try:
            scan_id = indexing_services.request_root_scan(user, root_id, request_id)
        except ScanRequestThrottled as error:
            response = _response(SCAN_REQUEST_DENIED, status=429)
            response["Retry-After"] = str(min(60, max(1, error.retry_after)))
            return response
        except PermissionError:
            return _response(SCAN_REQUEST_DENIED, status=403)
        except ValueError:
            return _response(SCAN_REQUEST_DENIED, status=400)
        except RuntimeError:
            return _response(CATALOG_UNAVAILABLE, status=503)
        return _response({"scanId": str(scan_id)}, status=202)
