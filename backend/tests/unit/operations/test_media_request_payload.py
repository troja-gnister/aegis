from __future__ import annotations

from typing import Any

import pytest
from aegis_apps.operations.serializers import (
    MEDIA_REQUEST_PERMISSIONS,
    normalize_media_request_payload,
)
from aegis_apps.roots.permissions import Permission

ROOT = "0b8e5e4c-7f3a-4c1e-9a55-3c7d2f1e0a11"
OTHER_ROOT = "1c9f6f5d-8a4b-4d2f-8b66-4d8e3a2f1b22"
ENTRY = "2da07a6e-9b5c-4e30-9c77-5e9f4b3a2c33"
FINGERPRINT = "0123456789abcdef" * 4
INTENT = {"roots": [{"id": ROOT, "permissions": MEDIA_REQUEST_PERMISSIONS}]}


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "v": 1, "rootId": ROOT, "entryId": ENTRY, "fingerprint": FINGERPRINT,
    }
    payload.update(overrides)
    return payload


def test_media_request_payload_is_normalized_and_bound_to_its_intent() -> None:
    assert int(Permission.BROWSE | Permission.PREVIEW) == MEDIA_REQUEST_PERMISSIONS
    assert normalize_media_request_payload(_payload(), intent=INTENT) == _payload()
    wider = {"roots": [{"id": ROOT, "permissions": 255}]}
    assert normalize_media_request_payload(_payload(), intent=wider) == _payload()


@pytest.mark.parametrize(
    "payload",
    [
        _payload(v=2),
        _payload(v=True),
        _payload(v="1"),
        _payload(rootId=ROOT.upper()),
        _payload(entryId=ENTRY.upper()),
        _payload(entryId="not-a-uuid"),
        _payload(entryId="{" + ENTRY + "}"),
        _payload(fingerprint=FINGERPRINT.upper()),
        _payload(fingerprint=FINGERPRINT[:-1]),
        _payload(fingerprint=None),
        _payload(extra=1),
        {"v": 1, "rootId": ROOT, "entryId": ENTRY},
        [ROOT, ENTRY, FINGERPRINT],
        None,
    ],
)
def test_malformed_media_request_payloads_are_rejected(payload: Any) -> None:
    with pytest.raises(ValueError, match="media request payload"):
        normalize_media_request_payload(payload, intent=INTENT)


@pytest.mark.parametrize(
    "intent",
    [
        {"roots": []},
        {"roots": [{"id": OTHER_ROOT, "permissions": MEDIA_REQUEST_PERMISSIONS}]},
        {"roots": [{"id": ROOT, "permissions": int(Permission.BROWSE)}]},
        {"roots": [{"id": ROOT, "permissions": int(Permission.PREVIEW)}]},
        {
            "roots": [
                {"id": ROOT, "permissions": MEDIA_REQUEST_PERMISSIONS},
                {"id": OTHER_ROOT, "permissions": MEDIA_REQUEST_PERMISSIONS},
            ]
        },
    ],
)
def test_payload_must_match_a_single_root_intent_with_browse_and_preview(
    intent: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="media request payload"):
        normalize_media_request_payload(_payload(), intent=intent)
