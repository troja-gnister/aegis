from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from scripts.podman_dev import inventory
from scripts.podman_dev.records import (
    RECORD_NAME,
    ApiRecord,
    PathIdentity,
    SessionError,
    SessionRecord,
    WebkitRecord,
    load_record,
    save_record,
)
from tests.support.podman_dev_doubles import ScriptedPodman, listing_replies

CONTAINER = "c" * 64
IMAGE = "e" * 64
NETWORK = "f" * 64


def full_record() -> SessionRecord:
    return SessionRecord(
        state_identity=PathIdentity(1, 2),
        api=ApiRecord(
            pid=42, start_time=99, command=["podman", "system", "service"],
            socket="/run/user/1000/aegis-podman-dev/api-x/podman.sock",
            directory_identity=PathIdentity(3, 4), socket_identity=PathIdentity(5, 6),
        ),
        webkit=WebkitRecord(container_id=CONTAINER, image_id=IMAGE, port=41234, token="a" * 32),
    )


def test_record_round_trips_privately_and_atomically(tmp_path: Path) -> None:
    save_record(tmp_path, full_record())
    assert load_record(tmp_path) == full_record()
    assert stat.S_IMODE((tmp_path / RECORD_NAME).stat().st_mode) == 0o600
    assert sorted(path.name for path in tmp_path.iterdir()) == [RECORD_NAME]


def test_partial_record_round_trips(tmp_path: Path) -> None:
    partial = SessionRecord(state_identity=PathIdentity(7, 8))
    save_record(tmp_path, partial)
    assert load_record(tmp_path) == partial


def test_webkit_endpoint_has_the_validated_shape() -> None:
    assert full_record().webkit is not None
    assert full_record().webkit.endpoint == f"ws://127.0.0.1:41234/{'a' * 32}"


def test_snapshot_records_identity_fields_and_tolerates_null() -> None:
    run = ScriptedPodman(listing_replies(
        containers=json.dumps([{
            "Id": CONTAINER, "Names": ["media-app"], "ImageID": IMAGE, "Created": 1,
            "State": "running",
        }]),
        volumes="null",
        networks=json.dumps([{"id": NETWORK, "name": "demo_default", "created": "t0"}]),
        images=json.dumps([{"Id": IMAGE, "Created": 5}]),
    ))
    assert inventory.snapshot(run) == {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1]},
        "volumes": {},
        "networks": {NETWORK: ["demo_default"]},
        "images": {IMAGE: [5]},
    }
    run.assert_done()


def test_snapshot_survives_a_json_round_trip() -> None:
    run = ScriptedPodman(listing_replies(volumes=json.dumps([
        {"Name": "demo_caddy_data", "CreatedAt": "t1"},
    ])))
    snapshot = inventory.snapshot(run)
    assert json.loads(json.dumps(snapshot)) == snapshot


def test_snapshot_failure_names_the_command() -> None:
    run = ScriptedPodman([(("ps",), 125, "")])
    with pytest.raises(SessionError, match="podman ps --all --format json failed"):
        inventory.snapshot(run)


def test_snapshot_rejects_an_unexpected_json_shape() -> None:
    run = ScriptedPodman([(("ps",), 0, json.dumps({"error": "x"}))])
    with pytest.raises(
        SessionError, match="podman ps --all --format json returned unexpected JSON",
    ):
        inventory.snapshot(run)


def test_compare_refuses_a_baseline_missing_a_kind() -> None:
    before: inventory.Inventory = {"containers": {}, "volumes": {}, "networks": {}}
    after: inventory.Inventory = {
        "containers": {}, "volumes": {}, "networks": {}, "images": {},
    }
    with pytest.raises(SessionError, match="baseline is missing images"):
        inventory.compare(before, after)


def test_compare_ignores_new_objects_and_runtime_state() -> None:
    before: inventory.Inventory = {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1]},
        "volumes": {}, "networks": {}, "images": {},
    }
    after: inventory.Inventory = {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1], "d" * 64: [["new"], IMAGE, 2]},
        "volumes": {"aegis-new": ["t"]}, "networks": {}, "images": {"9" * 64: [3]},
    }
    assert inventory.compare(before, after) == []


def test_compare_reports_disappeared_and_recreated_objects() -> None:
    before: inventory.Inventory = {
        "containers": {CONTAINER: [["media-app"], IMAGE, 1]},
        "volumes": {"demo_caddy_data": ["t1"]}, "networks": {}, "images": {},
    }
    after: inventory.Inventory = {
        "containers": {},
        "volumes": {"demo_caddy_data": ["t2"]}, "networks": {}, "images": {},
    }
    assert inventory.compare(before, after) == [
        f"pre-existing containers {CONTAINER} disappeared (was [['media-app'], '{IMAGE}', 1])",
        "pre-existing volumes demo_caddy_data changed from ['t1'] to ['t2']",
    ]


def network_snapshot(name: str, created: str) -> inventory.Inventory:
    run = ScriptedPodman(listing_replies(
        networks=json.dumps([{"id": NETWORK, "name": name, "created": created}]),
    ))
    snapshot = inventory.snapshot(run)
    run.assert_done()
    return snapshot


def test_network_identity_is_its_id_and_name_only() -> None:
    # Podman synthesizes the default network without a config file and reports
    # the time of each call as its creation time.
    before = network_snapshot("podman", "2026-10-04T09:38:47.311693885-05:00")
    regenerated = network_snapshot("podman", "2026-10-04T09:41:27.69728746-05:00")
    assert inventory.compare(before, regenerated) == []
    renamed = network_snapshot("renamed", "2026-10-04T09:41:27.69728746-05:00")
    assert inventory.compare(before, renamed) == [
        f"pre-existing networks {NETWORK} changed from ['podman'] to ['renamed']",
    ]
