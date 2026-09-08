from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

ROOT_LIST_PATH = "/api/v1/roots"


class RootCatalogCacheControlMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if request.path_info == ROOT_LIST_PATH:
            response.headers["Cache-Control"] = "private, no-store"
        return response
