from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.identity.models import User
from aegis_apps.media.details import ACTIVE_JOB_STATES
from aegis_apps.media.models import MediaAsset
from aegis_apps.media.request_policy import MediaRequestPolicy
from aegis_apps.media.requests import (
    MediaRequestBackoff,
    MediaRequestOutcome,
    active_request_count,
    entry_request_jobs,
    media_jobs,
    parse_media_request_job,
    request_derivative,
)
from aegis_apps.operations.enums import JobKind
from aegis_apps.operations.leases import claim_next_job, fail_job
from aegis_apps.operations.models import Job, Operation
from aegis_apps.operations.services import validate_authorization_snapshot
from aegis_apps.roots.models import Root, RootGrant
from aegis_apps.roots.permissions import Permission
from django.db import close_old_connections, connection, transaction
from django.db.models import F
from django.test import Client
from django.utils import timezone

from .conftest import ALL_MEDIA, MediaCatalog

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]

UNSUPPORTED = {"type": "media_unsupported", "title": "Media unsupported"}
NOT_FOUND = {"type": "catalog_not_found", "title": "Catalog item not found"}
INVALID = {"type": "invalid_request", "title": "Invalid request"}


def _path(entry: CatalogEntry) -> str:
    return f"/api/v1/entries/{entry.pk}/media/requests"


def _request(
    media: MediaCatalog, entry: CatalogEntry, kind: object = "preview", **kwargs: Any,
) -> Any:
    return media.post(_path(entry), {"kind": kind}, **kwargs)


def test_preview_request_queues_one_actor_bound_media_job(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0001.jpg")
    asset = media_catalog.asset(entry)
    root = media_catalog.root
    user = User.objects.get(pk=media_catalog.api.user.pk)

    response = _request(media_catalog, entry)

    assert response.status_code == 202
    assert response.json() == {"state": "pending", "kind": "preview"}
    assert response.headers["Cache-Control"] == "private, no-store"
    operation = Operation.objects.get()
    job = Job.objects.get()
    assert operation.actor_id == user.pk
    assert operation.kind == "media.preview"
    assert operation.request_id.startswith("media_")
    assert operation.intent == {"roots": [{"id": str(root.pk), "permissions": 3}]}
    assert operation.authorization_snapshot == {
        "userEpoch": user.authorization_epoch,
        "rootEpochs": {str(root.pk): root.authorization_epoch},
    }
    assert job.operation_id == operation.pk
    assert job.target_role == "media"
    assert job.kind == "media.preview"
    assert job.state == "queued"
    assert job.payload == {
        "v": 1, "rootId": str(root.pk), "entryId": str(entry.pk),
        "fingerprint": asset.fingerprint,
    }


def test_transcode_request_targets_videos(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"clip.mov")
    media_catalog.asset(entry, kind="video", container="mov", video_codec="hevc")

    response = _request(media_catalog, entry, "transcode")

    assert response.status_code == 202
    assert response.json() == {"state": "pending", "kind": "transcode"}
    assert Job.objects.get().kind == "media.transcode"


def test_requests_are_idempotent_while_pending(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0002.jpg")
    media_catalog.asset(entry)

    responses = [_request(media_catalog, entry) for _ in range(3)]

    assert [response.status_code for response in responses] == [202, 202, 202]
    assert {response.json()["state"] for response in responses} == {"pending"}
    assert Operation.objects.count() == 1
    assert Job.objects.count() == 1


def test_ready_derivative_is_reported_without_a_job(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0003.jpg")
    asset = media_catalog.asset(entry)
    media_catalog.derivative(asset, "preview")

    response = _request(media_catalog, entry)

    assert response.status_code == 202
    assert response.json() == {"state": "ready", "kind": "preview"}
    assert not Operation.objects.exists()


def test_a_ready_derivative_of_an_older_fingerprint_does_not_count(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"IMG_0004.jpg")
    asset = media_catalog.asset(entry)
    media_catalog.derivative(asset, "preview", fingerprint="c" * 64)

    response = _request(media_catalog, entry)

    assert response.json()["state"] == "pending"
    assert Job.objects.count() == 1


def test_idempotency_is_per_fingerprint_and_per_actor(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0005.jpg")
    asset = media_catalog.asset(entry)
    first = _request(media_catalog, entry)
    MediaAsset.objects.filter(pk=asset.pk).update(fingerprint="b" * 64)
    changed = _request(media_catalog, entry)
    RootGrant.objects.create(
        root_id=media_catalog.api.root.pk, user=media_catalog.api.group_user,
        permissions=int(ALL_MEDIA),
    )
    other = _request(media_catalog, entry, client=media_catalog.api.group_client)

    assert [first.status_code, changed.status_code, other.status_code] == [202, 202, 202]
    jobs = list(Job.objects.order_by("created_at").values("payload", "operation__actor_id"))
    assert len(jobs) == 3
    assert jobs[1]["payload"]["fingerprint"] == "b" * 64
    assert jobs[2]["operation__actor_id"] == media_catalog.api.group_user.pk


@pytest.mark.parametrize(
    ("asset_overrides", "kind"),
    [
        ({"probe_state": "unsupported", "thumbnail_state": "unsupported",
          "width": None, "height": None}, "preview"),
        ({"probe_state": "quarantined", "thumbnail_state": "quarantined",
          "safe_error_code": "timeout", "width": None, "height": None}, "preview"),
        ({"thumbnail_state": "quarantined", "safe_error_code": "decode_failed"}, "preview"),
        ({}, "transcode"),
    ],
)
def test_unsupported_or_quarantined_assets_are_409(
    media_catalog: MediaCatalog, asset_overrides: dict[str, object], kind: str,
) -> None:
    entry = media_catalog.entry(b"IMG_0006.jpg")
    media_catalog.asset(entry, **asset_overrides)

    response = _request(media_catalog, entry, kind)

    assert response.status_code == 409
    assert response.json() == UNSUPPORTED
    assert not Operation.objects.exists()


def test_preview_does_not_apply_to_videos(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"clip.mp4")
    media_catalog.asset(entry, kind="video")

    response = _request(media_catalog, entry, "preview")

    assert response.status_code == 409
    assert response.json() == UNSUPPORTED


@pytest.mark.parametrize(
    "permissions",
    [Permission.BROWSE, Permission.BROWSE | Permission.EXPORT, Permission.PREVIEW, Permission(0)],
)
def test_requests_need_browse_and_preview(
    media_catalog: MediaCatalog, permissions: Permission,
) -> None:
    entry = media_catalog.entry(b"IMG_0007.jpg")
    media_catalog.asset(entry)
    media_catalog.grant(permissions)

    response = _request(media_catalog, entry)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert not Operation.objects.exists()


def test_non_media_unknown_and_unavailable_entries_are_404(
    media_catalog: MediaCatalog,
) -> None:
    plain = media_catalog.entry(b"notes.txt")
    folder = media_catalog.entry(b"albums", kind="directory")
    gone = media_catalog.entry(b"IMG_0008.jpg")
    media_catalog.asset(gone)
    CatalogEntry.objects.filter(pk=gone.pk).update(source_state="missing")
    unknown = media_catalog.post(
        f"/api/v1/entries/{uuid.uuid4()}/media/requests", {"kind": "preview"},
    )

    for response in (*(_request(media_catalog, e) for e in (plain, folder, gone)), unknown):
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert not Operation.objects.exists()


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "thumbnail"},
        {"kind": "poster"},
        {"kind": "Preview"},
        {"kind": ["preview"]},
        {"kind": None},
        {"kind": "preview", "extra": 1},
        {},
        ["preview"],
        "preview",
    ],
)
def test_malformed_bodies_are_400(media_catalog: MediaCatalog, body: object) -> None:
    entry = media_catalog.entry(b"IMG_0009.jpg")
    media_catalog.asset(entry)

    response = media_catalog.post(_path(entry), body)

    assert response.status_code == 400
    assert response.json() == INVALID
    assert not Operation.objects.exists()


def test_non_json_and_query_parameters_are_400(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0010.jpg")
    media_catalog.asset(entry)
    token = media_catalog.client.cookies["csrftoken"].value

    with media_catalog.api.database.as_django_role("aegis_web"):
        form = media_catalog.client.post(
            _path(entry), data="kind=preview",
            content_type="application/x-www-form-urlencoded",
            headers={"X-CSRFToken": token},
        )
        broken = media_catalog.client.post(
            _path(entry), data="{", content_type="application/json",
            headers={"X-CSRFToken": token},
        )
    query = media_catalog.post(f"{_path(entry)}?kind=preview", {"kind": "preview"})

    for response in (form, broken, query):
        assert response.status_code == 400
        assert response.json() == INVALID
    assert not Operation.objects.exists()


def test_requests_require_csrf_and_authentication(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0011.jpg")
    media_catalog.asset(entry)

    without_token = _request(media_catalog, entry, csrf=False)
    anonymous = Client().post(
        _path(entry), {"kind": "preview"}, content_type="application/json",
    )

    assert without_token.status_code == 403
    assert without_token.json()["type"] == "csrf_failed"
    assert anonymous.status_code in {401, 403}
    assert not Operation.objects.exists()


def test_reads_are_not_allowed_on_the_request_endpoint(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0012.jpg")
    media_catalog.asset(entry)

    assert media_catalog.get(_path(entry)).status_code == 405


def test_concurrent_requests_of_one_actor_queue_one_job(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0013.jpg")
    media_catalog.asset(entry)
    actor_id = media_catalog.api.user.pk
    barrier = Barrier(2)

    def request(_index: int) -> MediaRequestOutcome:
        close_old_connections()
        try:
            actor = User.objects.get(pk=actor_id)
            barrier.wait(timeout=10)
            return request_derivative(actor, entry.pk, "preview")
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(request, range(2)))

    assert {outcome.state for outcome in outcomes} == {"pending"}
    assert sum(outcome.operation_id is not None for outcome in outcomes) == 1
    assert Job.objects.count() == 1


def test_worker_parses_the_job_and_rechecks_its_authorization(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"IMG_0014.jpg")
    asset = media_catalog.asset(entry)
    assert _request(media_catalog, entry).status_code == 202
    job = Job.objects.select_related("operation").get()

    parsed = parse_media_request_job(job)

    assert parsed.kind == "preview"
    assert parsed.root_id == media_catalog.api.root.pk
    assert parsed.entry_id == entry.pk
    assert parsed.fingerprint == asset.fingerprint
    assert parsed.operation_id == job.operation_id
    assert validate_authorization_snapshot(job.operation) is True

    media_catalog.grant(Permission.BROWSE)
    assert validate_authorization_snapshot(job.operation) is False
    media_catalog.grant(ALL_MEDIA)
    Root.objects.filter(pk=media_catalog.api.root.pk).update(
        authorization_epoch=F("authorization_epoch") + 1,
    )
    assert validate_authorization_snapshot(job.operation) is False


def test_request_never_writes_media_tables(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_0015.jpg")
    asset = media_catalog.asset(entry)
    before = MediaAsset.objects.filter(pk=asset.pk).values().get()

    assert _request(media_catalog, entry).status_code == 202

    assert MediaAsset.objects.filter(pk=asset.pk).values().get() == before
    assert not asset.derivatives.exists()


LIMIT = {"type": "media_request_limit", "title": "Too many media requests"}
BACKOFF = {"type": "media_request_backoff", "title": "Media request failed recently"}


def _fail_next_media_job(at: datetime, code: str = "handler_failed") -> None:
    lease = claim_next_job("media", str(uuid.uuid4()), at)
    assert lease is not None
    assert fail_job(lease, error_code=code, now=at) is True


def _user(media: MediaCatalog) -> User:
    return User.objects.get(pk=media.api.user.pk)


def test_open_requests_are_capped_per_actor_and_kind(
    media_catalog: MediaCatalog, settings: Any,
) -> None:
    settings.AEGIS_MEDIA_REQUEST_POLICY = MediaRequestPolicy(2, 1, 60, 86_400)
    photos = [media_catalog.entry(f"IMG_2{index:03d}.jpg".encode()) for index in range(3)]
    videos = [media_catalog.entry(f"clip_{index}.mp4".encode()) for index in range(2)]
    for entry in photos:
        media_catalog.asset(entry)
    for entry in videos:
        media_catalog.asset(entry, kind="video")

    first, second, third = (_request(media_catalog, entry) for entry in photos)
    repeat = _request(media_catalog, photos[0])
    transcode, over = (_request(media_catalog, entry, "transcode") for entry in videos)

    assert [first.status_code, second.status_code] == [202, 202]
    assert third.status_code == 429
    assert third.json() == LIMIT
    assert third.headers["Retry-After"] == "30"
    assert third.headers["Cache-Control"] == "private, no-store"
    # Idempotent repeats never count against the cap.
    assert repeat.status_code == 202
    assert repeat.json()["state"] == "pending"
    assert transcode.status_code == 202
    assert over.status_code == 429
    assert Job.objects.count() == 3

    # The cap is per actor.
    RootGrant.objects.create(
        root_id=media_catalog.api.root.pk, user=media_catalog.api.group_user,
        permissions=int(ALL_MEDIA),
    )
    other = _request(media_catalog, photos[2], client=media_catalog.api.group_client)
    assert other.status_code == 202


def test_a_finished_request_frees_its_slot(media_catalog: MediaCatalog, settings: Any) -> None:
    settings.AEGIS_MEDIA_REQUEST_POLICY = MediaRequestPolicy(1, 1, 60, 86_400)
    first, second = (media_catalog.entry(name) for name in (b"a.jpg", b"b.jpg"))
    media_catalog.asset(first)
    media_catalog.asset(second)
    assert _request(media_catalog, first).status_code == 202
    assert _request(media_catalog, second).status_code == 429

    _fail_next_media_job(timezone.now())

    assert _request(media_catalog, second).status_code == 202


def test_failed_requests_back_off_with_doubling_waits(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"poison.jpg")
    media_catalog.asset(entry)
    user = _user(media_catalog)
    assert request_derivative(user, entry.pk, "preview").state == "pending"
    first_failure = timezone.now()
    _fail_next_media_job(first_failure)

    response = _request(media_catalog, entry)
    assert response.status_code == 409
    assert response.json() == BACKOFF
    assert 1 <= int(response.headers["Retry-After"]) <= 60
    with pytest.raises(MediaRequestBackoff) as early:
        request_derivative(user, entry.pk, "preview", now=first_failure + timedelta(seconds=30))
    assert early.value.retry_after == 30

    retry = request_derivative(user, entry.pk, "preview", now=first_failure + timedelta(seconds=61))
    assert retry.state == "pending" and retry.operation_id is not None
    second_failure = first_failure + timedelta(seconds=61)
    _fail_next_media_job(second_failure)

    with pytest.raises(MediaRequestBackoff) as doubled:
        request_derivative(user, entry.pk, "preview", now=second_failure + timedelta(seconds=90))
    assert doubled.value.retry_after == 30
    accepted = request_derivative(
        user, entry.pk, "preview", now=second_failure + timedelta(seconds=121),
    )
    assert accepted.state == "pending"
    assert Job.objects.filter(state="failed").count() == 2


def test_backoff_applies_to_every_actor_and_only_to_that_fingerprint(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"poison.jpg")
    asset = media_catalog.asset(entry)
    assert _request(media_catalog, entry).status_code == 202
    _fail_next_media_job(timezone.now())
    RootGrant.objects.create(
        root_id=media_catalog.api.root.pk, user=media_catalog.api.group_user,
        permissions=int(ALL_MEDIA),
    )

    other = _request(media_catalog, entry, client=media_catalog.api.group_client)
    MediaAsset.objects.filter(pk=asset.pk).update(fingerprint="a" * 64)
    changed = _request(media_catalog, entry)

    assert other.status_code == 409
    assert other.json() == BACKOFF
    assert changed.status_code == 202


def test_a_revoked_authorization_is_not_a_failure_of_the_file(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"fine.jpg")
    media_catalog.asset(entry)
    assert _request(media_catalog, entry).status_code == 202
    _fail_next_media_job(timezone.now(), code="authorization_stale")

    assert _request(media_catalog, entry).status_code == 202


def test_a_failed_current_derivative_starts_the_backoff(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"broken.jpg")
    asset = media_catalog.asset(entry)
    failed = media_catalog.derivative(asset, "preview", state="failed")
    user = _user(media_catalog)

    response = _request(media_catalog, entry)

    assert response.status_code == 409
    assert response.json() == BACKOFF
    assert not Operation.objects.exists()
    later = request_derivative(
        user, entry.pk, "preview", now=failed.created_at + timedelta(seconds=61),
    )
    assert later.state == "pending"


def test_requests_never_block_browsing_readers_of_the_root(
    media_catalog: MediaCatalog,
) -> None:
    entry = media_catalog.entry(b"IMG_3000.jpg")
    media_catalog.asset(entry)
    database = media_catalog.api.database

    with database.connect("aegis_migrator") as reader, reader.transaction():
        # A browse, details or delivery request holds the root FOR SHARE.
        reader.execute(
            "SELECT id FROM roots_root WHERE id = %s FOR SHARE", [media_catalog.api.root.pk],
        )
        response = _request(media_catalog, entry)

    assert response.status_code == 202
    assert Job.objects.count() == 1


def test_request_lookups_use_the_media_request_index(media_catalog: MediaCatalog) -> None:
    entry = media_catalog.entry(b"IMG_3001.jpg")
    asset = media_catalog.asset(entry)
    assert _request(media_catalog, entry).status_code == 202
    actor_id = media_catalog.api.user.pk
    queries = [
        entry_request_jobs(entry.pk, JobKind.MEDIA_PREVIEW, asset.fingerprint).filter(
            state__in=ACTIVE_JOB_STATES, operation__actor_id=actor_id,
        ),
        entry_request_jobs(entry.pk, JobKind.MEDIA_PREVIEW, asset.fingerprint).filter(
            state="failed",
        ),
        media_jobs(JobKind.MEDIA_PREVIEW).filter(
            state__in=ACTIVE_JOB_STATES, operation__actor_id=actor_id,
        ),
    ]

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("ANALYZE operations_job")
        cursor.execute("SET LOCAL enable_seqscan = off")
        plans = [query.explain() for query in queries]

    # The entry-scoped lookups seek the partial (kind, state, payload entry) index; the
    # cap's count is bounded to open jobs of one kind by an index either way.
    for plan in plans[:2]:
        assert "operations_job_media_req_idx" in plan, plan
    assert "Seq Scan on operations_job" not in plans[2], plans[2]
    assert "Index" in plans[2], plans[2]
    assert active_request_count(actor_id, JobKind.MEDIA_PREVIEW) == 1
