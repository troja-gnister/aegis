from __future__ import annotations

import contextlib
import json
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import psycopg
import pytest
from aegis_apps.audit.models import AuditEvent
from aegis_apps.catalog.authorization import CatalogNotReady, CatalogUnavailable
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.catalog.names import source_name
from aegis_apps.identity.models import User
from aegis_apps.identity.session_policy import LAST_SEEN_AT
from aegis_apps.roots.models import Root
from django.contrib.sessions.models import Session
from django.db import DataError, IntegrityError, OperationalError, ProgrammingError, connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def _entry_path(entry_id: uuid.UUID) -> str:
    return f"/api/v1/entries/{entry_id}"


def _root_path(root_id: uuid.UUID) -> str:
    return f"/api/v1/roots/{root_id}/entries"


def _assert_private(response: Any) -> None:
    assert response.headers["Cache-Control"] == "private, no-store"


def _django_operational_error(cause: psycopg.OperationalError) -> OperationalError:
    error = OperationalError("private database detail")
    error.__cause__ = cause
    return error


def test_browse_is_private_and_never_enumerates_source(
    api_catalog: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("web touched original filesystem")

    monkeypatch.setattr("os.scandir", forbidden)
    response = api_catalog.get(_root_path(api_catalog.root.pk))

    assert response.status_code == 200
    _assert_private(response)
    payload = response.json()
    assert len(payload["entries"]) <= 100
    assert "total" not in payload
    assert payload["entries"][0]["size"] == str(2**63 + 17)
    assert payload["entries"][0]["modifiedNs"] == str(2**62 + 9)
    assert "/srv/aegis" not in response.content.decode()


@pytest.mark.parametrize(
    "path",
    (
        lambda: _root_path(uuid.uuid4()),
        lambda: _entry_path(uuid.uuid4()),
        lambda: f"/api/v1/roots/{uuid.uuid4()}/index-status",
    ),
)
def test_catalog_reads_require_authentication_and_never_cache(path: Any) -> None:
    response = Client().get(path())

    assert response.status_code == 401
    assert response.json() == {
        "type": "authentication_required",
        "title": "Authentication required",
    }
    _assert_private(response)


def test_unknown_and_foreign_catalog_objects_are_indistinguishable(api_catalog: Any) -> None:
    foreign_root = Root.objects.create(
        slot_id=f"foreign-{uuid.uuid4().hex}",
        display_name="Private root",
        mode=Root.Mode.READ_ONLY,
        active=True,
    )
    foreign_entry = CatalogEntry.objects.create(
        root=foreign_root,
        raw_name=b"",
        display_name="",
        name_key=b"",
        kind="directory",
    )

    root_responses = [
        api_catalog.get(_root_path(uuid.uuid4())),
        api_catalog.get(_root_path(foreign_root.pk)),
    ]
    entry_responses = [
        api_catalog.get(_entry_path(uuid.uuid4())),
        api_catalog.get(_entry_path(foreign_entry.pk)),
    ]

    for responses in (root_responses, entry_responses):
        assert [response.status_code for response in responses] == [404, 404]
        assert responses[0].content == responses[1].content
        assert responses[0].json() == {
            "type": "catalog_not_found",
            "title": "Catalog item not found",
        }
        for response in responses:
            _assert_private(response)


@pytest.mark.parametrize(
    "query",
    (
        "limit=0",
        "limit=251",
        "limit=01",
        "limit=ten",
        "sort=path",
        "order=sideways",
        "parent=not-a-uuid",
        "filters=%7Bbad",
        "filters=%7B%22v%22%3A2%7D",
        "filters=%7B%22v%22%3A1%7D&filters=%7B%22v%22%3A1%7D",
        "limit=10&limit=20",
        "path=%2Fsrv%2Faegis%2Froots%2Fsecret",
        "content=true",
        "download=true",
        "delete=true",
        "move=true",
        "rename=true",
    ),
)
def test_directory_query_rejects_bad_bounded_or_content_inputs(
    api_catalog: Any, query: str,
) -> None:
    response = api_catalog.get(f"{_root_path(api_catalog.root.pk)}?{query}")

    assert response.status_code == 400
    assert response.json() == {
        "type": "invalid_catalog_query",
        "title": "Invalid catalog query",
    }
    _assert_private(response)


@pytest.mark.parametrize("cursor", ("not-signed", "x" * 8193))
def test_cursor_failures_require_an_authorized_root_and_restart(
    api_catalog: Any, cursor: str,
) -> None:
    response = api_catalog.get(
        _root_path(api_catalog.root.pk),
        data={"cursor": cursor},
    )
    hidden = api_catalog.get(
        _root_path(uuid.uuid4()),
        data={"cursor": cursor},
    )

    assert response.status_code == 409
    assert response.json() == {
        "type": "cursor_restart_required",
        "title": "Catalog view changed",
    }
    assert hidden.status_code == 404
    assert hidden.json()["type"] == "catalog_not_found"
    _assert_private(response)
    _assert_private(hidden)


def test_not_ready_and_database_outage_are_distinct_private_problems(
    api_catalog: Any,
) -> None:
    with patch("aegis_apps.catalog.queries.directory_page", side_effect=CatalogNotReady()):
        not_ready = api_catalog.get(_root_path(api_catalog.root.pk))
    with patch("aegis_apps.catalog.queries.directory_page", side_effect=CatalogUnavailable()):
        unavailable = api_catalog.get(_root_path(api_catalog.root.pk))

    assert not_ready.status_code == 503
    assert not_ready.headers["Retry-After"] == "3"
    assert not_ready.json()["type"] == "catalog_not_ready"
    assert unavailable.status_code == 503
    assert "Retry-After" not in unavailable.headers
    assert unavailable.json()["type"] == "catalog_unavailable"
    _assert_private(not_ready)
    _assert_private(unavailable)


@pytest.mark.parametrize(
    "driver_error",
    (
        psycopg.OperationalError("connection failed"),
        psycopg.errors.ConnectionFailure("connection lost"),
        psycopg.errors.AdminShutdown("administrator shutdown"),
    ),
)
def test_driver_availability_errors_after_authentication_are_fixed_503(
    api_catalog: Any,
    driver_error: psycopg.OperationalError,
) -> None:
    api_catalog.client.raise_request_exception = False
    with patch(
        "aegis_apps.catalog.queries.directory_page",
        side_effect=_django_operational_error(driver_error),
    ):
        response = api_catalog.get(_root_path(api_catalog.root.pk))

    assert response.status_code == 503
    assert response.json() == {
        "type": "catalog_unavailable",
        "title": "Catalog unavailable",
    }
    assert b"private database detail" not in response.content
    assert bytes(str(driver_error), "utf-8") not in response.content
    _assert_private(response)


@pytest.mark.parametrize(
    "database_error",
    (
        ProgrammingError("private programming detail"),
        IntegrityError("private integrity detail"),
        DataError("private data detail"),
        _django_operational_error(psycopg.errors.DeadlockDetected("private deadlock")),
    ),
)
def test_unrelated_database_errors_remain_generic_500(
    api_catalog: Any,
    database_error: Exception,
) -> None:
    api_catalog.client.raise_request_exception = False
    with patch(
        "aegis_apps.catalog.queries.directory_page",
        side_effect=database_error,
    ):
        response = api_catalog.get(_root_path(api_catalog.root.pk))

    assert response.status_code == 500
    assert b"private" not in response.content
    _assert_private(response)


def test_framework_method_resolver_and_unhandled_errors_are_never_cached(
    api_catalog: Any,
) -> None:
    wrong_method = api_catalog.post(
        _root_path(api_catalog.root.pk),
        headers={"X-CSRFToken": api_catalog.client.cookies["csrftoken"].value},
    )
    resolver_404 = api_catalog.get("/api/v1/roots/not-a-uuid/entries")
    api_catalog.client.raise_request_exception = False
    with patch(
        "aegis_apps.catalog.queries.directory_page",
        side_effect=RuntimeError("/srv/aegis/private-secret"),
    ):
        unhandled = api_catalog.get(_root_path(api_catalog.root.pk))

    assert wrong_method.status_code == 405
    assert resolver_404.status_code == 404
    assert unhandled.status_code == 500
    assert b"/srv/aegis/private-secret" not in unhandled.content
    for response in (wrong_method, resolver_404, unhandled):
        _assert_private(response)


@pytest.mark.parametrize("method", ("post", "put", "patch", "delete"))
def test_catalog_has_no_content_or_mutation_methods(api_catalog: Any, method: str) -> None:
    with api_catalog.database.as_django_role("aegis_web"):
        response = getattr(api_catalog.client, method)(
            _entry_path(api_catalog.file.pk),
            headers={"X-CSRFToken": api_catalog.client.cookies["csrftoken"].value},
        )

    assert response.status_code == 405
    _assert_private(response)


def test_details_are_allowlisted_and_query_parameters_are_rejected(api_catalog: Any) -> None:
    response = api_catalog.get(_entry_path(api_catalog.file.pk))
    invalid = api_catalog.get(f"{_entry_path(api_catalog.file.pk)}?content=true")

    assert response.status_code == 200
    assert response.json() == {
        "id": str(api_catalog.file.pk),
        "rootId": str(api_catalog.root.pk),
        "displayName": "safe-file.unknown",
        "kind": "file",
        "typeHint": "unknown",
        "size": str(2**63 + 17),
        "modifiedNs": str(2**62 + 9),
        "sourceState": "present",
        "version": "0",
        "parentId": str(api_catalog.anchor.pk),
        "ancestors": [{"id": str(api_catalog.anchor.pk), "displayName": ""}],
        "ancestorsTruncated": False,
    }
    assert invalid.status_code == 400
    assert invalid.json()["type"] == "invalid_catalog_query"
    _assert_private(response)
    _assert_private(invalid)


def test_mid_request_epoch_change_returns_401_flushes_session_and_audits_once(
    api_catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis_apps.catalog import authorization
    from aegis_apps.catalog.authorization import browse_context as real_browse_context

    old_session_key = api_catalog.client.cookies["sessionid"].value

    @contextlib.contextmanager
    def mutate_before_lock(user: User, root_id: uuid.UUID, namespace: str) -> Any:
        User.objects.filter(pk=user.pk).update(
            authorization_epoch=user.authorization_epoch + 1,
        )
        with real_browse_context(user, root_id, namespace) as context:
            yield context

    monkeypatch.setattr(authorization, "browse_context", mutate_before_lock)
    response = api_catalog.get(_root_path(api_catalog.root.pk))

    assert response.status_code == 401
    assert response.json() == {
        "type": "authentication_required",
        "title": "Authentication required",
    }
    _assert_private(response)
    assert not Session.objects.filter(session_key=old_session_key).exists()
    assert api_catalog.client.get("/api/v1/auth/session").status_code == 401
    revoked = AuditEvent.objects.get(event_type="auth.session.revoked")
    assert revoked.actor is None
    assert revoked.outcome == "denied"
    assert revoked.request_id == response.headers["X-Request-ID"]
    assert revoked.metadata == {"request_id": response.headers["X-Request-ID"]}


@pytest.mark.parametrize("lookup", ("unknown_root", "unknown_entry"))
def test_mid_request_invalid_principal_precedes_early_catalog_rejection(
    api_catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
    lookup: str,
) -> None:
    from aegis_apps.catalog import authorization
    from aegis_apps.catalog import queries as catalog_queries

    old_session_key = api_catalog.client.cookies["sessionid"].value
    if lookup == "unknown_root":
        real_browse_context = authorization.browse_context

        @contextlib.contextmanager
        def mutate_before_root_lock(user: User, root_id: uuid.UUID, namespace: str) -> Any:
            User.objects.filter(pk=user.pk).update(
                authorization_epoch=user.authorization_epoch + 1,
            )
            with real_browse_context(user, root_id, namespace) as context:
                yield context

        monkeypatch.setattr(authorization, "browse_context", mutate_before_root_lock)
        response = api_catalog.get(_root_path(uuid.uuid4()))
    else:
        real_entry_details = catalog_queries.entry_details

        def mutate_before_candidate(
            user: User, entry_id: uuid.UUID, namespace: str,
        ) -> dict[str, object]:
            User.objects.filter(pk=user.pk).update(
                authorization_epoch=user.authorization_epoch + 1,
            )
            return real_entry_details(user, entry_id, namespace)

        monkeypatch.setattr(catalog_queries, "entry_details", mutate_before_candidate)
        response = api_catalog.get(_entry_path(uuid.uuid4()))

    assert response.status_code == 401
    assert response.json()["type"] == "authentication_required"
    assert not Session.objects.filter(session_key=old_session_key).exists()
    assert AuditEvent.objects.filter(event_type="auth.session.revoked").count() == 1


def test_mid_request_epoch_change_after_detail_candidate_flushes_session(
    api_catalog: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis_apps.catalog import queries as catalog_queries
    from aegis_apps.catalog.authorization import browse_context as real_browse_context

    old_session_key = api_catalog.client.cookies["sessionid"].value

    @contextlib.contextmanager
    def mutate_after_candidate(user: User, root_id: uuid.UUID, namespace: str) -> Any:
        with api_catalog.database.connect("aegis_migrator") as writer:
            writer.execute(
                "UPDATE identity_user SET authorization_epoch=authorization_epoch+1 "
                "WHERE id=%s",
                [user.pk],
            )
        with real_browse_context(user, root_id, namespace) as context:
            yield context

    monkeypatch.setattr(catalog_queries, "browse_context", mutate_after_candidate)
    response = api_catalog.get(_entry_path(api_catalog.file.pk))

    assert response.status_code == 401
    assert response.json()["type"] == "authentication_required"
    assert not Session.objects.filter(session_key=old_session_key).exists()
    assert AuditEvent.objects.filter(event_type="auth.session.revoked").count() == 1


def test_http_filter_bounds_and_unknown_type_token_remain_distinct(api_catalog: Any) -> None:
    name = source_name(b"no-extension")
    no_extension = CatalogEntry.objects.create(
        root=api_catalog.root,
        source_parent=api_catalog.anchor,
        logical_parent=api_catalog.anchor,
        source_parent_revision=api_catalog.anchor.source_revision,
        raw_name=name.raw,
        display_name=name.display,
        name_key=name.order_key,
        type_hint=name.type_hint,
        kind="file",
    )
    real_unknown = api_catalog.get(
        _root_path(api_catalog.root.pk),
        data={"filters": json.dumps({"v": 1, "type": ["unknown"]})},
    )
    unknown_token = api_catalog.get(
        _root_path(api_catalog.root.pk),
        data={"filters": json.dumps({"v": 1, "type": ["__unknown__"]})},
    )
    too_many = api_catalog.get(
        _root_path(api_catalog.root.pk),
        data={"filters": json.dumps({"v": 1, "kind": ["file"] * 33})},
    )
    too_large = api_catalog.get(
        _root_path(api_catalog.root.pk),
        data={"filters": json.dumps({"v": 1, "prefix": "x" * 8193})},
    )

    assert [entry["id"] for entry in real_unknown.json()["entries"]] == [
        str(api_catalog.file.pk),
    ]
    assert [entry["id"] for entry in unknown_token.json()["entries"]] == [
        str(no_extension.pk),
    ]
    assert too_many.status_code == too_large.status_code == 400
    assert too_many.json()["type"] == too_large.json()["type"] == "invalid_catalog_query"


def test_authenticated_http_browse_stays_within_middleware_inclusive_budget(
    api_catalog: Any,
) -> None:
    with (
        api_catalog.database.as_django_role("aegis_web"),
        CaptureQueriesContext(connection) as captured,
    ):
        response = api_catalog.client.get(_root_path(api_catalog.root.pk))

    assert response.status_code == 200
    statements = [query["sql"] for query in captured]
    assert len(statements) <= 16
    data = [
        sql for sql in statements
        if not sql.startswith(("BEGIN", "COMMIT", "SET LOCAL"))
        and "django_session" not in sql
    ]
    assert len(data) <= 8
    assert any("django_session" in sql for sql in statements)
    assert any("identity_user" in sql for sql in statements)


def test_activity_refresh_http_browse_stays_within_middleware_inclusive_budget(
    api_catalog: Any,
) -> None:
    session = api_catalog.client.session
    session[LAST_SEEN_AT] = (timezone.now() - timedelta(minutes=2)).isoformat()
    session.save()

    with (
        api_catalog.database.as_django_role("aegis_web"),
        CaptureQueriesContext(connection) as captured,
    ):
        response = api_catalog.client.get(_root_path(api_catalog.root.pk))

    assert response.status_code == 200
    statements = [query["sql"] for query in captured]
    assert len(statements) <= 16
    data = [
        sql for sql in statements
        if not sql.startswith(("BEGIN", "COMMIT", "SET LOCAL"))
        and "django_session" not in sql
    ]
    session_updates = [
        sql for sql in statements
        if sql.lstrip().startswith("UPDATE") and "django_session" in sql
    ]
    assert len(data) <= 8
    assert len(session_updates) == 1
    assert any("identity_user" in sql for sql in statements)


def test_maximum_page_response_is_bounded_to_one_mibibyte(api_catalog: Any) -> None:
    name_prefix = b"\x80" * 252
    entries = []
    for index in range(250):
        raw = name_prefix + f"{index:03d}".encode()
        name = source_name(raw)
        entry = api_catalog.file
        entries.append(CatalogEntry(
            root=api_catalog.root,
            source_parent=api_catalog.anchor,
            logical_parent=api_catalog.anchor,
            source_parent_revision=api_catalog.anchor.source_revision,
            raw_name=name.raw,
            display_name=name.display,
            name_key=name.order_key,
            type_hint=name.type_hint,
            kind=entry.kind,
            size=2**64 - 1,
            mtime_ns=2**63 - 1,
        ))
    CatalogEntry.objects.bulk_create(entries)

    response = api_catalog.get(_root_path(api_catalog.root.pk), data={"limit": "250"})

    assert response.status_code == 200
    assert len(response.content) <= 1024 * 1024
    assert len(response.json()["entries"]) == 250
    assert len(json.dumps(response.json())) <= 1024 * 1024
