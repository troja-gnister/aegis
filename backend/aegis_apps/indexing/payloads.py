"""Django-free checkpoint serialization and conservative PostgreSQL JSONB budgets."""

import base64
import json

from aegis_apps.catalog.domain import Observation

from .protocol import MAX_PAYLOAD_BYTES, ReaderBatch, _observation_payload, encode_message


def checkpoint_observation(item: Observation) -> dict[str, object]:
    # This protocol helper rederives source_name and rejects mismatched DTOs.
    payload = _observation_payload(item)
    payload.update(display=item.name.display,
                   name_key=base64.b64encode(item.name.order_key).decode("ascii"),
                   type_hint=item.name.type_hint)
    return payload


def _jsonb_size(value: object) -> int:
    # JSONB text uses comma/colon spaces. ASCII escaping conservatively bounds
    # UTF-8 JSONB output, including non-ASCII and astral Unicode display names.
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False).encode("ascii"))


def checkpoint_observation_size(item: Observation) -> int:
    return _jsonb_size(checkpoint_observation(item))


def checkpoint_batch_overhead(sequence: int) -> int:
    return _jsonb_size({"sequence": sequence, "observations": []})


def checkpoint_payload(batch: ReaderBatch) -> dict[str, object]:
    encode_message(batch)
    payload: dict[str, object] = {
        "sequence": batch.sequence,
        "observations": [checkpoint_observation(item) for item in batch.observations],
    }
    if _jsonb_size(payload) > MAX_PAYLOAD_BYTES:
        raise ValueError("invalid scan batch")
    return payload
