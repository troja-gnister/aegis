from __future__ import annotations

import re
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

ROOT_LIST_PATH = "/api/v1/roots"
OPERATIONS_STATUS_PATH = "/api/v1/admin/operations/status"
NO_STORE_PATHS = frozenset((ROOT_LIST_PATH, OPERATIONS_STATUS_PATH))
NO_STORE_PATH_PATTERNS = (
    re.compile(r"^/api/v1/roots/[^/]+/(?:entries|index-status|scans)$"),
    re.compile(r"^/api/v1/entries/[^/]+$"),
)


class RootCatalogCacheControlMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.path_info in NO_STORE_PATHS or any(
            pattern.fullmatch(request.path_info) for pattern in NO_STORE_PATH_PATTERNS
        ):
            response.headers["Cache-Control"] = "private, no-store"
        return response
