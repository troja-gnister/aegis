"""Fenced asset leases: lane order, queue order, fencing, renewal and release."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from aegis_apps.media.discovery import discover_batch, reset_asset
from aegis_apps.media.enums import Lane, StageState
from aegis_apps.media.leasing import (
    MAX_CLAIM_BATCH,
    PARKED_UNTIL,
    AssetLease,
    bump_to_interactive,
    claim_assets,
    fenced,
    lease_delta,
    release_asset_lease,
    renew_asset_lease,
)
from aegis_apps.media.models import MediaAsset
from django.utils import timezone

from .worker_support import Library, make_library

pytestmark = [pytest.mark.integration, pytest.mark.django_db]


@pytest.fixture
def library(browse_fixture: Any) -> Library:
    return make_library(browse_fixture)


def _assets(library: Library, count: int) -> list[uuid.UUID]:
    entries = [library.photo() for _ in range(count)]
    discover_batch(library.root.pk, None, limit=500, now=timezone.now())
    return [entry.pk for entry in entries]


def test_a_claim_fences_the_asset_and_never_counts_an_attempt(library: Library) -> None:
    (entry_id,) = _assets(library, 1)
    owner = uuid.uuid4()
    now = timezone.now()
    (claimed,) = claim_assets(owner, limit=4, root_ids=[library.root.pk], now=now)
    asset = MediaAsset.objects.get(pk=entry_id)
    assert claimed.lease == AssetLease(entry_id, 1, owner, now + lease_delta())
    assert (asset.attempt_token, asset.attempts) == (1, 0)
    assert asset.lease_owner == owner
    assert asset.lease_expires_at == asset.next_attempt_at == now + lease_delta()
    # Leased rows leave the claim range until the lease expires.
    assert claim_assets(uuid.uuid4(), limit=4, root_ids=[library.root.pk], now=now) == []
    later = now + lease_delta() + timedelta(seconds=1)
    (again,) = claim_assets(uuid.uuid4(), limit=4, root_ids=[library.root.pk], now=later)
    assert again.lease.attempt_token == 2
    assert MediaAsset.objects.get(pk=entry_id).attempts == 0


def test_the_interactive_lane_is_claimed_first(library: Library) -> None:
    background = _assets(library, 3)
    MediaAsset.objects.filter(pk=background[2]).update(lane=Lane.INTERACTIVE)
    claimed = claim_assets(uuid.uuid4(), limit=1, root_ids=[library.root.pk],
                           now=timezone.now())
    assert [item.lease.entry_id for item in claimed] == [background[2]]


def test_within_a_lane_the_oldest_queue_time_comes_first(library: Library) -> None:
    ids = _assets(library, 3)
    now = timezone.now()
    for offset, entry_id in zip((30, 10, 20), ids, strict=True):
        MediaAsset.objects.filter(pk=entry_id).update(
            next_attempt_at=now - timedelta(seconds=offset),
        )
    claimed = claim_assets(uuid.uuid4(), limit=3, root_ids=[library.root.pk], now=now)
    assert [item.lease.entry_id for item in claimed] == [ids[0], ids[2], ids[1]]


def test_finished_future_parked_and_foreign_assets_are_not_claimed(library: Library) -> None:
    ready, future, parked, failed = _assets(library, 4)
    now = timezone.now()
    MediaAsset.objects.filter(pk=ready).update(
        probe_state=StageState.READY, thumbnail_state=StageState.READY,
    )
    MediaAsset.objects.filter(pk=future).update(next_attempt_at=now + timedelta(minutes=5))
    MediaAsset.objects.filter(pk=parked).update(next_attempt_at=PARKED_UNTIL)
    MediaAsset.objects.filter(pk=failed).update(
        probe_state=StageState.FAILED, safe_error_code="decode_failed", attempts=1,
        attempt_token=1,
    )
    claimed = claim_assets(uuid.uuid4(), limit=8, root_ids=[library.root.pk], now=now)
    assert [item.lease.entry_id for item in claimed] == [failed]
    assert claim_assets(uuid.uuid4(), limit=8, root_ids=[uuid.uuid4()], now=now) == []
    assert claim_assets(uuid.uuid4(), limit=8, root_ids=[], now=now) == []


def test_claim_batches_are_bounded(library: Library) -> None:
    _assets(library, 3)
    with pytest.raises(ValueError):
        claim_assets(uuid.uuid4(), limit=MAX_CLAIM_BATCH + 1, root_ids=[library.root.pk],
                     now=timezone.now())
    assert len(claim_assets(uuid.uuid4(), limit=2, root_ids=[library.root.pk],
                            now=timezone.now())) == 2
    with pytest.raises(ValueError):
        lease_delta(0)
    with pytest.raises(ValueError):
        lease_delta(3_601)


def test_renewal_and_release_require_the_live_lease(library: Library) -> None:
    (entry_id,) = _assets(library, 1)
    owner = uuid.uuid4()
    now = timezone.now()
    (claimed,) = claim_assets(owner, limit=1, root_ids=[library.root.pk], now=now)
    renewed = renew_asset_lease(claimed.lease, now=now + timedelta(seconds=60))
    assert renewed is not None
    assert renewed.expires_at == now + timedelta(seconds=60) + lease_delta()
    stranger = AssetLease(entry_id, claimed.lease.attempt_token, uuid.uuid4(), now)
    assert renew_asset_lease(stranger, now=now) is None
    assert not release_asset_lease(stranger, now=now)
    stale_token = AssetLease(entry_id, 0, owner, now)
    assert renew_asset_lease(stale_token, now=now) is None
    expired = now + timedelta(hours=2)
    assert renew_asset_lease(renewed, now=expired) is None
    assert release_asset_lease(renewed, now=now + timedelta(seconds=61))
    asset = MediaAsset.objects.get(pk=entry_id)
    assert (asset.lease_owner, asset.lease_expires_at, asset.attempts) == (None, None, 0)
    assert asset.next_attempt_at == now + timedelta(seconds=61)


def test_a_fingerprint_reset_fences_out_the_lease_holder(library: Library) -> None:
    (entry_id,) = _assets(library, 1)
    now = timezone.now()
    (claimed,) = claim_assets(uuid.uuid4(), limit=1, root_ids=[library.root.pk], now=now)
    reset_asset(entry_id, now=now, source=None)
    assert not MediaAsset.objects.filter(fenced(claimed.lease, now=now)).exists()
    assert renew_asset_lease(claimed.lease, now=now) is None


def test_an_interactive_request_bumps_unfinished_stages_only(library: Library) -> None:
    pending, done, parked = _assets(library, 3)
    MediaAsset.objects.filter(pk=done).update(
        probe_state=StageState.READY, thumbnail_state=StageState.READY,
    )
    MediaAsset.objects.filter(pk=parked).update(next_attempt_at=PARKED_UNTIL)
    now = timezone.now()
    assert bump_to_interactive(pending, now=now) == 1
    assert bump_to_interactive(done, now=now) == 0
    assert bump_to_interactive(parked, now=now) == 0
    assert MediaAsset.objects.get(pk=pending).lane == Lane.INTERACTIVE
