from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

import pytest
from aegis_apps.media.enums import DerivativeKind
from aegis_apps.media.keys import (
    DERIVATIVE_KEY_DOMAIN,
    FINGERPRINT_DOMAIN,
    PARAMS_DOMAIN,
    canonical_params,
    derivative_key,
    fingerprint,
    params_digest,
    relative_path,
)

ROOT = uuid.UUID("00112233-4455-6677-8899-aabbccddeeff")
ENTRY = uuid.UUID("ffeeddcc-bbaa-9988-7766-554433221100")
UINT64_MAX = (1 << 64) - 1

# Vectors computed independently from the documented M1 byte layout.
FINGERPRINT_VECTOR = "98dbfddde68dc63ed960fb3940a5c3dc652c469970be34c57c9d1085311572ad"
FINGERPRINT_VECTOR_INPUT = (
    "61656769732e6d656469612e66696e6765727072696e742e763100"
    "00112233445566778899aabbccddeeff"
    "ffeeddccbbaa99887766554433221100"
    "000000000000fd01"
    "000000000012d687"
    "00000000003d8600"
    "000000000000000017979cfe3d85cd15"
)
THUMBNAIL_PARAMS = {"format": "webp", "maxEdge": 400, "quality": 75}
THUMBNAIL_PARAMS_DIGEST = "b148d8d0d871a297e81d12ba7116f33e530c2d206feca897d6fcb4a48327adbf"
TRANSCODE_PARAMS = {
    "audioBitrate": 160000,
    "faststart": True,
    "format": "mp4",
    "maxHeight": 1080,
    "videoCodec": "h264",
}
TRANSCODE_PARAMS_DIGEST = "1e21918ac46466508afbb117358462c656a401696e88ea4bfee683aac3117a2e"


def test_fingerprint_matches_the_documented_vector_and_layout() -> None:
    value = fingerprint(ROOT, ENTRY, 64769, 1234567, 4032000, 1_700_000_000_123_456_789)

    assert value == FINGERPRINT_VECTOR
    layout = bytes.fromhex(FINGERPRINT_VECTOR_INPUT)
    assert len(layout) == 99
    assert layout.startswith(FINGERPRINT_DOMAIN)
    assert hashlib.sha256(layout).hexdigest() == FINGERPRINT_VECTOR
    manual = (
        FINGERPRINT_DOMAIN
        + ROOT.bytes
        + ENTRY.bytes
        + (64769).to_bytes(8, "big")
        + (1234567).to_bytes(8, "big")
        + (4032000).to_bytes(8, "big")
        + (1_700_000_000_123_456_789).to_bytes(16, "big", signed=True)
    )
    assert manual == layout


@pytest.mark.parametrize(
    ("device", "inode", "size", "mtime_ns", "expected"),
    [
        (0, 0, 0, -1, "fa26155ba35615716b7e020bf1e46d859f80334a08027cccadc605cca86dda88"),
        (
            UINT64_MAX,
            UINT64_MAX,
            UINT64_MAX,
            (1 << 127) - 1,
            "2d5a837ea024a6cc6cec3bbecbb33ad6d9c578cf90a5f3f2a164b40024235f07",
        ),
        (0, 0, 0, -(1 << 127), "d0a17bf07163bc8cdcdbac74837b135483524329ac2ffe2024e103c37807dbc2"),
    ],
)
def test_fingerprint_boundary_vectors(
    device: int, inode: int, size: int, mtime_ns: int, expected: str
) -> None:
    assert fingerprint(ROOT, ENTRY, device, inode, size, mtime_ns) == expected


def test_fingerprint_accepts_integral_catalog_decimals() -> None:
    assert (
        fingerprint(
            ROOT,
            ENTRY,
            Decimal("64769"),
            Decimal("1234567"),
            Decimal("4.032E+6"),
            Decimal("1700000000123456789"),
        )
        == FINGERPRINT_VECTOR
    )


def test_fingerprint_changes_with_every_input() -> None:
    base = (ROOT, ENTRY, 1, 2, 3, 4)
    variants = {
        fingerprint(*base),
        fingerprint(ENTRY, ROOT, 1, 2, 3, 4),
        fingerprint(ROOT, uuid.UUID(int=ENTRY.int ^ 1), 1, 2, 3, 4),
        fingerprint(ROOT, ENTRY, 2, 2, 3, 4),
        fingerprint(ROOT, ENTRY, 1, 3, 3, 4),
        fingerprint(ROOT, ENTRY, 1, 2, 4, 4),
        fingerprint(ROOT, ENTRY, 1, 2, 3, 5),
    }
    assert len(variants) == 7


@pytest.mark.parametrize(
    "arguments",
    [
        (str(ROOT), ENTRY, 1, 2, 3, 4),
        (ROOT, ENTRY.bytes, 1, 2, 3, 4),
        (ROOT, ENTRY, -1, 2, 3, 4),
        (ROOT, ENTRY, 1, UINT64_MAX + 1, 3, 4),
        (ROOT, ENTRY, 1, 2, True, 4),
        (ROOT, ENTRY, 1, 2, 3.0, 4),
        (ROOT, ENTRY, 1, 2, "3", 4),
        (ROOT, ENTRY, 1, 2, Decimal("3.5"), 4),
        (ROOT, ENTRY, 1, 2, Decimal("NaN"), 4),
        (ROOT, ENTRY, 1, 2, 3, None),
        (ROOT, ENTRY, 1, 2, 3, 1 << 127),
        (ROOT, ENTRY, 1, 2, 3, -(1 << 127) - 1),
    ],
)
def test_fingerprint_rejects_invalid_or_unencodable_inputs(arguments: tuple[object, ...]) -> None:
    with pytest.raises(ValueError, match="invalid"):
        fingerprint(*arguments)  # type: ignore[arg-type]


def test_params_digest_vectors_are_canonical_and_order_independent() -> None:
    assert canonical_params(THUMBNAIL_PARAMS) == b'{"format":"webp","maxEdge":400,"quality":75}'
    assert params_digest(THUMBNAIL_PARAMS) == THUMBNAIL_PARAMS_DIGEST
    assert params_digest(dict(reversed(THUMBNAIL_PARAMS.items()))) == THUMBNAIL_PARAMS_DIGEST
    assert hashlib.sha256(
        PARAMS_DOMAIN + canonical_params(TRANSCODE_PARAMS)
    ).hexdigest() == TRANSCODE_PARAMS_DIGEST
    assert params_digest(TRANSCODE_PARAMS) == TRANSCODE_PARAMS_DIGEST
    assert params_digest({}) == hashlib.sha256(PARAMS_DOMAIN + b"{}").hexdigest()


@pytest.mark.parametrize(
    "params",
    [
        {"quality": 0.75},
        {"quality": None},
        {"nested": {"a": 1}},
        {"list": [1]},
        {"Format": "webp"},
        {"max-edge": 1},
        {"": 1},
        {"a" * 33: 1},
        {"format": "wébp"},
        {"format": "line\nbreak"},
        {"format": "x" * 65},
        {"big": 1 << 63},
        {f"k{index}": index for index in range(33)},
        {f"k{index}": "x" * 64 for index in range(20)},
    ],
)
def test_params_digest_rejects_non_canonical_or_unbounded_parameters(
    params: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="invalid derivative parameters"):
        params_digest(params)


@pytest.mark.parametrize(
    ("processor_version", "kind", "parameters", "expected"),
    [
        (
            "1",
            DerivativeKind.THUMBNAIL,
            THUMBNAIL_PARAMS_DIGEST,
            "9c909ac25fa3144d0379d3184cfbe8fee4f514c6789f6d8a7d7cdc666c129a34",
        ),
        (
            "1",
            "preview",
            THUMBNAIL_PARAMS_DIGEST,
            "972b5a8ed9bb5f912d0d62bd2df89f77dad0c6011fab482e47eab1bc7f6c2e71",
        ),
        (
            "2026.10.1",
            DerivativeKind.TRANSCODE,
            TRANSCODE_PARAMS_DIGEST,
            "b791442d9c523f12999a0e4e4ffc928f91bddb1045ec9618a280ef359e0329f5",
        ),
        (
            "aegis-media-processor/1",
            DerivativeKind.THUMBNAIL,
            THUMBNAIL_PARAMS_DIGEST,
            "f5969432d3c11942649c9d3d383d136b37ba7ba4f26024ee03788f6d00219caa",
        ),
    ],
)
def test_derivative_key_vectors(
    processor_version: str, kind: str, parameters: str, expected: str
) -> None:
    assert derivative_key(FINGERPRINT_VECTOR, processor_version, kind, parameters) == expected


def test_derivative_key_layout_is_length_prefixed() -> None:
    version = b"1"
    kind = b"thumbnail"
    manual = (
        DERIVATIVE_KEY_DOMAIN
        + bytes.fromhex(FINGERPRINT_VECTOR)
        + bytes([len(version)])
        + version
        + bytes([len(kind)])
        + kind
        + bytes.fromhex(THUMBNAIL_PARAMS_DIGEST)
    )
    assert len(manual) == 102
    assert hashlib.sha256(manual).hexdigest() == derivative_key(
        FINGERPRINT_VECTOR, "1", "thumbnail", THUMBNAIL_PARAMS_DIGEST
    )


@pytest.mark.parametrize(
    ("source", "version", "kind", "parameters"),
    [
        (FINGERPRINT_VECTOR.upper(), "1", "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR[:63], "1", "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "", "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "1 ", "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "v" * 65, "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "-1", "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "/1", "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "processor\\1", "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "v\u00e9", "thumbnail", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "1", "original", THUMBNAIL_PARAMS_DIGEST),
        (FINGERPRINT_VECTOR, "1", "thumbnail", "z" * 64),
        (FINGERPRINT_VECTOR, "1", "thumbnail", None),
    ],
)
def test_derivative_key_rejects_invalid_inputs(
    source: str, version: str, kind: str, parameters: object
) -> None:
    with pytest.raises(ValueError, match="invalid"):
        derivative_key(source, version, kind, parameters)  # type: ignore[arg-type]


def test_relative_path_shards_by_the_first_two_key_bytes() -> None:
    key = "9c909ac25fa3144d0379d3184cfbe8fee4f514c6789f6d8a7d7cdc666c129a34"

    assert relative_path(key, "webp") == f"9c/90/{key}.webp"
    assert relative_path(key, "mp4") == f"9c/90/{key}.mp4"


@pytest.mark.parametrize(
    ("key", "extension"),
    [
        ("../" + "a" * 61, "webp"),
        ("A" * 64, "webp"),
        ("a" * 65, "webp"),
        ("a" * 64, "jpg"),
        ("a" * 64, "webp/../x"),
        ("a" * 64, ""),
    ],
)
def test_relative_path_rejects_unsafe_keys_and_extensions(key: str, extension: str) -> None:
    with pytest.raises(ValueError, match="invalid derivative"):
        relative_path(key, extension)
