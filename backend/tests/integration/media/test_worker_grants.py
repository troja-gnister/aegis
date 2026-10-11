"""The whole media pipeline runs with an actual ``aegis_media`` login and its grants only.

The media role reads only the granted catalog and root columns and never locks a catalog or
root row, so any query outside those grants fails here with ``permission denied``.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aegis_apps.catalog.models import CatalogEntry
from aegis_apps.media.discovery import discover_batch, rediscover_entry
from aegis_apps.media.enums import DerivativeState, StageState
from aegis_apps.media.eviction import evict
from aegis_apps.media.leasing import PARKED_UNTIL
from aegis_apps.media.models import MediaAsset, MediaDerivative
from aegis_apps.media.recovery import RecoverySweep
from aegis_apps.operations.enums import JobState
from aegis_apps.operations.models import Job, WorkerHeartbeat
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from tests.support.database_roles import RoleDatabase

from .worker_support import WORKER_ID, drive, make_library, make_worker

pytestmark = [pytest.mark.integration, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def _clear_shutdown() -> Iterator[None]:
    from aegis_apps.operations.management.commands import run_role

    run_role._reset_shutdown()
    yield
    run_role._reset_shutdown()


@override_settings(AEGIS_PROCESS_ROLE="media")
def test_the_pipeline_runs_under_the_media_role_grants(
    browse_fixture: Any, role_database: RoleDatabase, tmp_path: Path,
) -> None:
    library = make_library(browse_fixture)
    photo = library.photo()
    video = library.video()
    requested = library.photo()
    gone = library.photo()
    discover_batch(library.root.pk, None, limit=500, now=timezone.now())
    MediaAsset.objects.filter(pk=requested.pk).update(
        probe_state=StageState.READY, thumbnail_state=StageState.READY,
    )
    operation = library.request(library.requester(), requested)
    CatalogEntry.objects.filter(pk=gone.pk).update(source_state="missing")
    worker, processor, clock = make_worker(tmp_path)

    def finished() -> bool:
        return (
            MediaAsset.objects.filter(pk__in=(photo.pk, video.pk),
                                      thumbnail_state=StageState.READY).count() == 2
            and Job.objects.get(operation_id=operation.pk).state == JobState.SUCCEEDED
            and MediaAsset.objects.get(pk=gone.pk).next_attempt_at == PARKED_UNTIL
        )

    with role_database.as_django_role("aegis_media"):
        with connection.cursor() as cursor:
            cursor.execute("SELECT current_user")
            assert cursor.fetchone() == ("aegis_media",)
        drive(worker, processor, clock, finished, limit=40)
        report = evict(worker.store, budget_bytes=1)
        sweep = RecoverySweep(worker.store, grace_seconds=0, wall_clock=lambda: time.time() + 1)
        sweep.start()
        while sweep.active:
            sweep.step(now=timezone.now())
        rediscovered = rediscover_entry(photo.pk, now=timezone.now())
        worker.shutdown(wait_seconds=0)

    assert rediscovered == "unchanged"
    assert report.deleted == 1
    assert not MediaDerivative.objects.filter(kind="preview").exists()
    assert set(MediaDerivative.objects.values_list("kind", "state")) == {
        ("thumbnail", DerivativeState.READY), ("poster", DerivativeState.READY),
    }
    heartbeat = WorkerHeartbeat.objects.get(role="media", worker_id=WORKER_ID)
    assert heartbeat.metrics["mediaAssets"] >= 3


def test_the_media_role_cannot_read_ungranted_catalog_columns(
    browse_fixture: Any, role_database: RoleDatabase,
) -> None:
    """A guard for the queries above: the boundary they rely on is real."""
    import psycopg

    library = make_library(browse_fixture)
    library.photo()
    with role_database.connect("aegis_media") as caller:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            caller.execute("SELECT display_name FROM public.catalog_catalogentry LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            caller.execute(
                "SELECT id FROM public.catalog_catalogentry LIMIT 1 FOR SHARE",
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            caller.execute("SELECT display_name FROM public.roots_root LIMIT 1")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            caller.execute("DELETE FROM public.media_mediaasset")
