from __future__ import annotations

import re
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

ROOT_LIST_PATH = "/api/v1/roots"
OPERATIONS_STATUS_PATH = "/api/v1/admin/operations/status"
NO_STORE_PATHS = frozenset((ROOT_LIST_PATH, OPERATIONS_STATUS_PATH))
NO_STORE_PATH_PATTERNS = (
    re.compile(r"^/api/v1/roots/[^/]+/(?:entries|index-status|scans|timeline)$"),
    re.compile(r"^/api/v1/entries/[^/]+(?:/media(?:/requests)?)?$"),
)
# Media delivery sets its own caching per response (derivatives may be revalidated);
# anything else under it, such as a resolver 404, must still never be stored.
MEDIA_DELIVERY_PREFIX = "/media/"


class RootCatalogCacheControlMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        path = request.path_info
        forced = path in NO_STORE_PATHS or any(
            pattern.fullmatch(path) for pattern in NO_STORE_PATH_PATTERNS
        )
        unset_media = (
            path.startswith(MEDIA_DELIVERY_PREFIX) and "Cache-Control" not in response.headers
        )
        if forced or unset_media:
            response.headers["Cache-Control"] = "private, no-store"
        return response
