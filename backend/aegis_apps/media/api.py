"""Private HTTP transport for media details, interactive requests and the timeline."""

from __future__ import annotations

import re
from typing import Final, TypedDict
from uuid import UUID

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework.exceptions import ParseError, UnsupportedMediaType
from rest_framework.request import Request
from rest_framework.response import Response

from aegis_apps.catalog import authorization
from aegis_apps.catalog.api import CatalogAPIView, _query_values, _response, _user
from aegis_apps.catalog.cursors import CursorRestartRequired
from aegis_apps.identity.api import AUTHENTICATION_REQUIRED, INVALID_REQUEST_PROBLEM
from aegis_apps.identity.serializers import BoundedJSONParser
from aegis_apps.identity.session_policy import cache_namespace

from . import details, requests, timeline
from .requests import MediaRequestBackoff, MediaRequestLimited, MediaRequestUnsupported

MEDIA_UNSUPPORTED: Final = {"type": "media_unsupported", "title": "Media unsupported"}
MEDIA_REQUEST_LIMIT: Final = {"type": "media_request_limit", "title": "Too many media requests"}
MEDIA_REQUEST_BACKOFF: Final = {
    "type": "media_request_backoff", "title": "Media request failed recently",
}
_TIMELINE_PARAMETERS: Final = frozenset({"kind", "year", "cursor", "limit"})
_LIMIT_RE: Final = re.compile(r"[1-9][0-9]{0,2}\Z", re.ASCII)
_YEAR_RE: Final = re.compile(r"[0-9]{4}\Z", re.ASCII)


class TimelineInput(TypedDict):
    kind: timeline.TimelineKind
    year: int | None
    limit: int
    cursor: str | None


def _timeline_input(request: Request) -> TimelineInput:
    values = _query_values(request, allowed=_TIMELINE_PARAMETERS)
    encoded_kind = values.get("kind", "all")
    if encoded_kind not in ("all", "photo", "video"):
        raise ValueError("invalid_catalog_query")
    kind: timeline.TimelineKind = (
        "photo" if encoded_kind == "photo" else "video" if encoded_kind == "video" else "all"
    )
    encoded_limit = values.get("limit", str(timeline.DEFAULT_TIMELINE_LIMIT))
    if _LIMIT_RE.fullmatch(encoded_limit) is None:
        raise ValueError("invalid_catalog_query")
    limit = int(encoded_limit)
    if limit > timeline.MAX_TIMELINE_LIMIT:
        raise ValueError("invalid_catalog_query")
    year: int | None = None
    if "year" in values:
        if _YEAR_RE.fullmatch(values["year"]) is None or int(values["year"]) < 1:
            raise ValueError("invalid_catalog_query")
        year = int(values["year"])
    cursor = values.get("cursor")
    if cursor is not None and (
        len(cursor) > timeline.MAX_TIMELINE_CURSOR_BYTES or not cursor.isascii()
    ):
        raise CursorRestartRequired()
    return {"kind": kind, "year": year, "limit": limit, "cursor": cursor}


class EntryMediaView(CatalogAPIView):
    def get(self, request: Request, entry_id: UUID) -> Response:
        user = _user(request)
        if user is None:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        namespace = cache_namespace(session=request.session, user=user)
        try:
            payload = details.media_details(user, entry_id, namespace)
            _query_values(request, allowed=frozenset())
        except Exception as error:
            return self._catalog_error(request, error)
        return _response(payload)


@method_decorator(csrf_protect, name="dispatch")
class EntryMediaRequestView(CatalogAPIView):
    parser_classes = (BoundedJSONParser,)

    def post(self, request: Request, entry_id: UUID) -> Response:
        user = _user(request)
        if user is None:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        try:
            body = request.data
        except (ParseError, UnsupportedMediaType):
            return _response(INVALID_REQUEST_PROBLEM, status=400)
        if (
            request.query_params
            or not isinstance(body, dict)
            or set(body) != {"kind"}
            or not isinstance(body["kind"], str)
            or body["kind"] not in requests.REQUESTABLE
        ):
            return _response(INVALID_REQUEST_PROBLEM, status=400)
        try:
            outcome = requests.request_derivative(user, entry_id, body["kind"])
        except MediaRequestUnsupported:
            return _response(MEDIA_UNSUPPORTED, status=409)
        except MediaRequestLimited as error:
            response = _response(MEDIA_REQUEST_LIMIT, status=429)
            response["Retry-After"] = str(error.retry_after)
            return response
        except MediaRequestBackoff as error:
            response = _response(MEDIA_REQUEST_BACKOFF, status=409)
            response["Retry-After"] = str(error.retry_after)
            return response
        except Exception as error:
            return self._catalog_error(request, error)
        return _response({"state": outcome.state, "kind": outcome.kind}, status=202)


class TimelineView(CatalogAPIView):
    def get(self, request: Request, root_id: UUID) -> Response:
        user = _user(request)
        if user is None:
            return _response(AUTHENTICATION_REQUIRED, status=401)
        namespace = cache_namespace(session=request.session, user=user)
        try:
            with authorization.root_context(user, root_id, namespace) as context:
                payload = timeline.timeline_page(context, **_timeline_input(request))
        except Exception as error:
            return self._catalog_error(request, error)
        return _response(payload)
