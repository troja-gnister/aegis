from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from aegis_apps.common.middleware import RequestContextMiddleware
from aegis_apps.common.request_context import request_id_var
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory, override_settings
from django.urls import path


def _raise_error(_request: HttpRequest) -> HttpResponse:
    raise RuntimeError("private exception text")


urlpatterns = [path("test/error", _raise_error)]


def test_untrusted_request_id_is_replaced() -> None:
    response = Client().get("/health/live", headers={"X-Request-ID": "../secret\nforged"})

    value = response.headers["X-Request-ID"]
    assert len(value) == 32
    assert value.isascii() and value.isalnum()


def test_valid_request_id_is_preserved() -> None:
    response = Client().get("/health/live", headers={"X-Request-ID": "client_ID-1234"})

    assert response.headers["X-Request-ID"] == "client_ID-1234"


@override_settings(ROOT_URLCONF=__name__)
def test_request_id_is_returned_on_converted_error_response() -> None:
    client = Client(raise_request_exception=False)

    response = client.get("/test/error", headers={"X-Request-ID": "error_req-1234"})

    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "error_req-1234"


def test_request_context_resets_after_sequential_request() -> None:
    seen: list[str] = []

    def respond(request: HttpRequest) -> HttpResponse:
        seen.append(request_id_var.get())
        assert request.request_id == seen[-1]  # type: ignore[attr-defined]
        return HttpResponse()

    middleware = RequestContextMiddleware(respond)
    request = RequestFactory().get("/", headers={"X-Request-ID": "sequential-1234"})

    response = middleware(request)

    assert response.headers["X-Request-ID"] == "sequential-1234"
    assert seen == ["sequential-1234"]
    assert request_id_var.get(None) is None


def test_concurrent_request_contexts_do_not_bleed() -> None:
    barrier = Barrier(2)

    def invoke(request_id: str) -> tuple[str, str, None]:
        observed = ""

        def respond(_request: HttpRequest) -> HttpResponse:
            nonlocal observed
            barrier.wait(timeout=5)
            observed = request_id_var.get()
            barrier.wait(timeout=5)
            return HttpResponse()

        request = RequestFactory().get("/", headers={"X-Request-ID": request_id})
        response = RequestContextMiddleware(respond)(request)
        return observed, response.headers["X-Request-ID"], request_id_var.get(None)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, ("concurrent-A", "concurrent-B")))

    assert results == [
        ("concurrent-A", "concurrent-A", None),
        ("concurrent-B", "concurrent-B", None),
    ]
