from __future__ import annotations

import re
import secrets
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .request_context import request_id_var

REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class RequestContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        supplied = request.headers.get("X-Request-ID", "")
        request_id = supplied if REQUEST_ID.fullmatch(supplied) else secrets.token_hex(16)
        token = request_id_var.set(request_id)
        request.request_id = request_id  # type: ignore[attr-defined]
        try:
            response = self.get_response(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)
