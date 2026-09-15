import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.indexing.models import IndexDeployment
from aegis_apps.roots.models import Root, RootGrant
from django.contrib.auth.models import Group
from django.db import close_old_connections, connection, transaction

from tests.support.database_roles import RoleDatabase

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


def test_unknown_and_foreign_ids_are_indistinguishable(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import CatalogNotFound, browse_context

    foreign = Root.objects.create(
        slot_id="foreign", display_name="Foreign", active=True, mode="read_only",
    )
    child = browse_fixture.entry()
    for root_id in (uuid.uuid4(), foreign.pk):
        with (
            pytest.raises(CatalogNotFound, match=r"^catalog_not_found$"),
            browse_context(browse_fixture.user, root_id, browse_fixture.namespace),
        ):
            pytest.fail("foreign root authorized")
    with pytest.raises(CatalogNotFound):
        browse_fixture.page(parent_id=uuid.uuid4())
    with pytest.raises(CatalogNotFound):
        browse_fixture.details(uuid.uuid4())
    RootGrant.objects.filter(user=browse_fixture.user).update(permissions=0)
    with pytest.raises(CatalogNotFound):
        browse_fixture.details(child.pk)
    with pytest.raises(CatalogNotFound):
        browse_fixture.page(cursor="invalid cursor")


def test_additive_group_grants_and_no_superuser_shortcut(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import CatalogNotFound

    group = Group.objects.create(name="Browsers")
    browse_fixture.user.groups.add(group)
    browse_fixture.user.refresh_from_db()
    RootGrant.objects.filter(user=browse_fixture.user).update(permissions=2)
    RootGrant.objects.create(root=browse_fixture.root, group=group, permissions=1)
    assert browse_fixture.page()["entries"] == []
    RootGrant.objects.filter(group=group).update(permissions=2)
    User.objects.filter(pk=browse_fixture.user.pk).update(is_superuser=True)
    with pytest.raises(CatalogNotFound):
        browse_fixture.page()


def test_manifest_binding_drift_is_closed(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import CatalogNotFound

    IndexDeployment.objects.update(manifest_identity="b" * 64)
    with pytest.raises(CatalogNotFound):
        browse_fixture.page()


def test_uncreated_anchor_is_not_ready_and_get_does_not_create(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import CatalogNotReady

    CatalogEntry.objects.filter(pk=browse_fixture.anchor.pk)._raw_delete("default")
    with pytest.raises(CatalogNotReady):
        browse_fixture.page()
    assert not CatalogEntry.objects.filter(root=browse_fixture.root).exists()


@pytest.mark.parametrize("mutation", ["missing", "inaccessible", "kind", "revision", "unknown"])
def test_source_ancestor_staleness_survives_return_to_present(
    browse_fixture: Any, mutation: str,
) -> None:
    parent = browse_fixture.entry(b"parent", kind="directory")
    child = browse_fixture.entry(b"child", parent=parent, kind="directory")
    leaf = browse_fixture.entry(b"leaf", parent=child)
    changes: dict[str, dict[str, Any]] = {
        "missing": {"source_state": "missing"},
        "inaccessible": {"source_state": "inaccessible"},
        "kind": {"kind": "file"},
        "revision": {"source_revision": 1},
        "unknown": {"source_parent_revision": None},
    }
    CatalogEntry.objects.filter(pk=parent.pk).update(**changes[mutation])
    page = browse_fixture.page(parent_id=child.pk)
    assert page["indexStatus"]["state"] == "unavailable"
    assert page["entries"][0]["sourceState"] != "present"
    assert browse_fixture.details(leaf.pk)["sourceState"] != "present"


@pytest.mark.parametrize("state", ["missing", "inaccessible"])
def test_logical_ancestor_staleness_is_visible_in_pages_and_details(
    browse_fixture: Any, state: str,
) -> None:
    logical = browse_fixture.entry(b"logical", kind="directory", state=state)
    moved = browse_fixture.entry(b"moved", logical_parent=logical, kind="directory")
    leaf = browse_fixture.entry(b"leaf", parent=moved)
    assert browse_fixture.details(leaf.pk)["sourceState"] == "inaccessible"
    assert browse_fixture.page(parent_id=moved.pk)["indexStatus"]["state"] == "unavailable"
    assert browse_fixture.page(parent_id=logical.pk)["entries"][0]["sourceState"] == "inaccessible"


def test_foreign_parent_and_entry_do_not_escape_authorized_root(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import CatalogNotFound

    foreign = Root.objects.create(
        slot_id="foreign", display_name="Foreign", active=True, mode="read_only",
    )
    anchor = CatalogEntry.objects.create(
        root=foreign, raw_name=b"", name_key=b"", display_name="", kind="directory",
    )
    with pytest.raises(CatalogNotFound):
        browse_fixture.page(parent_id=anchor.pk)
    with pytest.raises(CatalogNotFound):
        browse_fixture.details(anchor.pk)


def test_revision_change_then_source_recovery_does_not_revive_descendants(
    browse_fixture: Any,
) -> None:
    ancestor = browse_fixture.entry(b"ancestor", kind="directory")
    child = browse_fixture.entry(b"child", parent=ancestor, kind="directory")
    CatalogEntry.objects.filter(pk=ancestor.pk).update(source_state="missing", source_revision=1)
    CatalogEntry.objects.filter(pk=ancestor.pk).update(source_state="present")
    assert browse_fixture.page(parent_id=child.pk)["indexStatus"]["state"] == "unavailable"
    assert browse_fixture.details(child.pk)["sourceState"] == "inaccessible"


@pytest.mark.parametrize("logical", [False, True])
def test_cyclic_ancestry_is_unavailable_without_unbounded_walk(
    browse_fixture: Any, logical: bool,
) -> None:
    first = browse_fixture.entry(b"first", kind="directory")
    second = browse_fixture.entry(b"second", parent=first, kind="directory")
    field = "logical_parent" if logical else "source_parent"
    CatalogEntry.objects.filter(pk=first.pk).update(**{field: second})
    assert browse_fixture.page(parent_id=second.pk)["indexStatus"]["state"] == "unavailable"
    assert browse_fixture.details(second.pk)["sourceState"] == "inaccessible"


def test_shared_browse_serializes_foundation_revocation(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import (
        CatalogAuthenticationRequired,
        CatalogNotFound,
        browse_context,
    )
    from aegis_apps.catalog.filters import FileFilter
    from aegis_apps.catalog.queries import directory_page
    from aegis_apps.roots.services import remove_grant

    child = browse_fixture.entry()
    admin = User.objects.create_superuser(username="revoke-admin")
    grant = RootGrant.objects.get(user=browse_fixture.user)
    attempted = Event()

    def revoke() -> None:
        close_old_connections()
        try:
            def observe(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
                if "roots_root" in sql and "FOR UPDATE" in sql:
                    attempted.set()
                return execute(sql, params, many, context)

            with connection.execute_wrapper(observe):
                remove_grant(actor=admin, grant_id=grant.pk, request_id="browse-revoke-race")
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with browse_context(
            browse_fixture.user, browse_fixture.root.pk, browse_fixture.namespace,
        ) as c:
            future = executor.submit(revoke)
            assert attempted.wait(5)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            page = directory_page(c, None, FileFilter(), "name", "asc", 100, None)
            assert isinstance(page["entries"], list)
            assert page["entries"][0]["id"] == str(child.pk)
            assert page["directoryId"] == str(browse_fixture.anchor.pk)
        future.result(timeout=5)
    with pytest.raises(CatalogAuthenticationRequired):
        browse_fixture.page()
    browse_fixture.user.refresh_from_db()
    with pytest.raises(CatalogNotFound):
        browse_fixture.page()


@pytest.mark.parametrize("mutation", ["user_epoch", "binding"])
def test_waiting_reader_rechecks_captured_epoch_and_manifest(
    browse_fixture: Any, mutation: str, role_database: RoleDatabase,
) -> None:
    from aegis_apps.catalog.authorization import CatalogAuthenticationRequired, CatalogNotFound
    from aegis_apps.indexing.binding import install_index_binding
    from aegis_apps.indexing.config import ScanPolicy
    from aegis_apps.roots.permissions import Permission
    from aegis_apps.roots.services import set_user_grant

    admin = User.objects.create_superuser(username="epoch-race-admin")
    mutated, reader_attempted = Event(), Event()

    def mutate() -> None:
        close_old_connections()
        original_settings = connection.settings_dict
        connection.settings_dict = {
            **original_settings, "USER": "aegis_migrator",
            "PASSWORD": role_database.passwords["aegis_migrator"],
        }
        try:
            with transaction.atomic():
                if mutation == "user_epoch":
                    set_user_grant(actor=admin, root_id=browse_fixture.root.pk,
                                   user_id=browse_fixture.user.pk, permissions=Permission(3),
                                   request_id="browse-epoch-race")
                else:
                    install_index_binding(None, ScanPolicy(3600, 120, 500, 1))
                mutated.set()
                assert reader_attempted.wait(5)
        finally:
            connection.close()
            connection.settings_dict = original_settings

    def observe(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        if "roots_root" in sql and "FOR SHARE" in sql:
            reader_attempted.set()
        return execute(sql, params, many, context)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(mutate)
        try:
            assert mutated.wait(5)
            with connection.execute_wrapper(observe), pytest.raises(CatalogAuthenticationRequired):
                browse_fixture.page()
        finally:
            reader_attempted.set()
            future.result(timeout=5)
    if mutation == "binding":
        browse_fixture.user.refresh_from_db()
        with pytest.raises(CatalogNotFound):
            browse_fixture.page()


def test_actual_web_lock_timeout_has_fixed_safe_error(
    browse_fixture: Any, role_database: RoleDatabase,
) -> None:
    from aegis_apps.catalog.authorization import CatalogUnavailable

    with role_database.connect("aegis_migrator") as blocker, blocker.transaction():
        blocker.execute(
            "SELECT id FROM roots_root WHERE id=%s FOR UPDATE", [browse_fixture.root.pk],
        )
        with role_database.as_django_role("aegis_web"), pytest.raises(
            CatalogUnavailable, match=r"^catalog_unavailable$",
        ):
            browse_fixture.page()


def test_expired_context_and_statement_timeout_are_closed(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import CatalogNotFound, CatalogUnavailable, browse_context
    from aegis_apps.catalog.filters import FileFilter
    from aegis_apps.catalog.queries import directory_page

    with browse_context(browse_fixture.user, browse_fixture.root.pk, browse_fixture.namespace) as c:
        pass
    with transaction.atomic(), pytest.raises(CatalogNotFound):
        directory_page(c, None, FileFilter(), "name", "asc", 100, None)
    with pytest.raises(CatalogUnavailable, match=r"^catalog_unavailable$"), browse_context(
        browse_fixture.user, browse_fixture.root.pk, browse_fixture.namespace,
    ), connection.cursor() as cursor:
        cursor.execute("SELECT pg_sleep(6)")


def test_unrelated_missing_entry_does_not_poison_current_parent(browse_fixture: Any) -> None:
    browse_fixture.entry(b"unrelated", state="missing")
    folder = browse_fixture.entry(b"current", kind="directory")
    child = browse_fixture.entry(b"child", parent=folder)
    assert browse_fixture.page(parent_id=folder.pk)["indexStatus"]["state"] == "not_indexed"
    assert browse_fixture.details(child.pk)["sourceState"] == "present"


def test_two_actual_web_readers_share_authorization_locks(
    browse_fixture: Any, role_database: RoleDatabase,
) -> None:
    from aegis_apps.catalog.authorization import browse_context

    def second_reader() -> str:
        close_old_connections()
        try:
            page = browse_fixture.page()
            with connection.cursor() as cursor:
                cursor.execute("SELECT session_user")
                assert cursor.fetchone() == ("aegis_web",)
            return str(page["directoryId"])
        finally:
            connection.close()

    with (
        role_database.as_django_role("aegis_web"),
        ThreadPoolExecutor(max_workers=1) as executor,
        browse_context(browse_fixture.user, browse_fixture.root.pk, browse_fixture.namespace),
    ):
        future = executor.submit(second_reader)
        assert future.result(timeout=3) == str(browse_fixture.anchor.pk)


def test_details_recheck_revocation_after_permission_bound_candidate_lookup(
    browse_fixture: Any,
) -> None:
    from aegis_apps.catalog.authorization import CatalogAuthenticationRequired
    from aegis_apps.roots.services import remove_grant

    entry = browse_fixture.entry()
    admin = User.objects.create_superuser(username="detail-race-admin")
    grant = RootGrant.objects.get(user=browse_fixture.user)
    candidate_read, revoked = Event(), Event()

    def revoke() -> None:
        close_old_connections()
        try:
            assert candidate_read.wait(5)
            remove_grant(actor=admin, grant_id=grant.pk, request_id="details-revoke-race")
            revoked.set()
        finally:
            connection.close()

    def observe(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        result = execute(sql, params, many, context)
        if "catalog_catalogentry" in sql and "roots_rootgrant" in sql:
            candidate_read.set()
            assert revoked.wait(5)
        return result

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(revoke)
        try:
            with connection.execute_wrapper(observe), pytest.raises(CatalogAuthenticationRequired):
                browse_fixture.details(entry.pk)
        finally:
            candidate_read.set()
            future.result(timeout=5)


def test_stale_physical_ancestor_of_moved_logical_parent_is_unavailable(
    browse_fixture: Any,
) -> None:
    physical = browse_fixture.entry(b"physical", kind="directory")
    logical = browse_fixture.entry(b"logical", parent=physical, kind="directory")
    moved = browse_fixture.entry(b"moved", logical_parent=logical, kind="directory")
    CatalogEntry.objects.filter(pk=physical.pk).update(source_revision=1)
    assert browse_fixture.page(parent_id=moved.pk)["indexStatus"]["state"] == "unavailable"
    assert browse_fixture.details(moved.pk)["sourceState"] == "inaccessible"


def test_inactive_principals_and_roots_are_rechecked(browse_fixture: Any) -> None:
    from aegis_apps.catalog.authorization import CatalogAuthenticationRequired, CatalogNotFound

    User.objects.filter(pk=browse_fixture.user.pk).update(is_active=False)
    with pytest.raises(CatalogAuthenticationRequired):
        browse_fixture.page()
    User.objects.filter(pk=browse_fixture.user.pk).update(is_active=True)
    Root.objects.filter(pk=browse_fixture.root.pk).update(active=False)
    with pytest.raises(CatalogNotFound):
        browse_fixture.page()


def test_bad_manifest_configuration_fails_closed(
    browse_fixture: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aegis_apps.catalog.authorization import CatalogNotFound

    monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", "invalid")
    with pytest.raises(CatalogNotFound):
        browse_fixture.page()


def test_disconnected_logical_directory_is_not_a_current_anchor(browse_fixture: Any) -> None:
    directory = browse_fixture.entry(b"disconnected", kind="directory")
    CatalogEntry.objects.filter(pk=directory.pk).update(logical_parent=None)
    assert browse_fixture.page(parent_id=directory.pk)["indexStatus"]["state"] == "unavailable"
    assert browse_fixture.details(directory.pk)["sourceState"] == "inaccessible"


@pytest.mark.parametrize("changed", ["namespace", "root_epoch", "filters", "limit"])
def test_current_context_is_bound_to_cursor(browse_fixture: Any, changed: str) -> None:
    from aegis_apps.catalog.cursors import CursorRestartRequired
    from aegis_apps.catalog.filters import parse_filters

    browse_fixture.seed_ties(4)
    page = browse_fixture.page(limit=1)
    options: dict[str, Any] = {"limit": 1, "cursor": page["nextCursor"]}
    if changed == "namespace":
        browse_fixture.namespace = "b" * 40
    elif changed == "root_epoch":
        Root.objects.filter(pk=browse_fixture.root.pk).update(authorization_epoch=1)
    elif changed == "filters":
        options["filters"] = parse_filters({"v": 1, "kind": ["file"]})
    else:
        options["limit"] = 2
    with pytest.raises(CursorRestartRequired):
        browse_fixture.page(**options)


@pytest.mark.parametrize("mutation", ["revoked", "inactive"])
@pytest.mark.parametrize("target", [
    "details", "unknown_entry", "unknown_root", "inactive_root", "bad_manifest",
])
def test_actual_web_invalid_principal_precedes_early_not_found(
    browse_fixture: Any, role_database: RoleDatabase, monkeypatch: pytest.MonkeyPatch,
    mutation: str, target: str,
) -> None:
    from aegis_apps.catalog.authorization import CatalogAuthenticationRequired, browse_context
    from aegis_apps.identity.admin_services import set_user_active
    from aegis_apps.roots.services import remove_grant

    entry = browse_fixture.entry()
    admin = User.objects.create_superuser(username="early-rejection-admin")
    # The fixture principal represents middleware's already-validated epoch.
    # Commit the real foundation mutation before candidate/root resolution starts.
    captured_epoch = browse_fixture.user.authorization_epoch
    if mutation == "revoked":
        grant = RootGrant.objects.get(user=browse_fixture.user)
        remove_grant(actor=admin, grant_id=grant.pk, request_id="early-rejection-revoke")
    else:
        set_user_active(actor=admin, user_id=browse_fixture.user.pk, active=False,
                        request_id="early-rejection-inactive")
    assert browse_fixture.user.authorization_epoch == captured_epoch
    if target == "inactive_root":
        Root.objects.filter(pk=browse_fixture.root.pk).update(active=False)
    elif target == "bad_manifest":
        monkeypatch.setenv("AEGIS_MOUNT_MANIFEST_SHA256", "invalid")
    with role_database.as_django_role("aegis_web"):
        with connection.cursor() as cursor:
            cursor.execute("SELECT session_user")
            assert cursor.fetchone() == ("aegis_web",)
        with pytest.raises(CatalogAuthenticationRequired, match=r"^authentication_required$"):
            if target in ("details", "unknown_entry", "bad_manifest"):
                browse_fixture.details(uuid.uuid4() if target == "unknown_entry" else entry.pk)
            else:
                root_id = uuid.uuid4() if target == "unknown_root" else browse_fixture.root.pk
                with browse_context(browse_fixture.user, root_id, browse_fixture.namespace):
                    pytest.fail("invalid principal authorized")


def test_actual_web_current_principal_retains_identical_failed_lookup_errors(
    browse_fixture: Any, role_database: RoleDatabase,
) -> None:
    from aegis_apps.catalog.authorization import CatalogNotFound, browse_context
    from aegis_apps.roots.services import remove_grant

    entry = browse_fixture.entry()
    admin = User.objects.create_superuser(username="current-rejection-admin")
    grant = RootGrant.objects.get(user=browse_fixture.user)
    remove_grant(actor=admin, grant_id=grant.pk, request_id="current-rejection-revoke")
    browse_fixture.user.refresh_from_db()
    with role_database.as_django_role("aegis_web"):
        for entry_id in (entry.pk, uuid.uuid4()):
            with pytest.raises(CatalogNotFound, match=r"^catalog_not_found$"):
                browse_fixture.details(entry_id)
        for root_id in (browse_fixture.root.pk, uuid.uuid4()):
            with (
                pytest.raises(CatalogNotFound, match=r"^catalog_not_found$"),
                browse_context(browse_fixture.user, root_id, browse_fixture.namespace),
            ):
                pytest.fail("unauthorized root authorized")
