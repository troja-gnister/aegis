from __future__ import annotations

import json
from typing import cast

import pytest
from aegis_apps.catalog.domain import EntryKind, SourceState
from aegis_apps.catalog.filters import _ascii_json_size, canonical_filter_bytes, parse_filters
from hypothesis import given
from hypothesis import strategies as st


def canonical(value: object) -> dict[str, object]:
    return cast(dict[str, object], json.loads(canonical_filter_bytes(parse_filters(value))))


def raw_filter_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def oversized_raw_filter(prefix_character: str) -> dict[str, object]:
    return {
        "v": 1,
        "kind": ["directory"] * 32,
        "type": ["abcdefghijklmnop"] * 32,
        "availability": ["inaccessible"] * 32,
        "prefix": prefix_character * 1200,
        "size": {"min": "0", "max": "18446744073709551615"},
        "modified": {
            "from": "1970-01-01T00:00:00Z",
            "before": "1970-01-01T00:00:01Z",
        },
    }


def test_equivalent_multiselects_have_one_cursor_context() -> None:
    first = parse_filters({"v": 1, "kind": ["file", "directory", "file"]})
    second = parse_filters({"v": 1, "kind": ["directory", "file"]})
    assert canonical_filter_bytes(first) == canonical_filter_bytes(second)


@given(st.lists(st.sampled_from([kind.value for kind in EntryKind]), max_size=32))
def test_kind_multiselect_canonicalization_is_order_and_duplicate_independent(
    values: list[str],
) -> None:
    filters = parse_filters({"v": 1, "kind": values})
    assert filters.kind == tuple(EntryKind(value) for value in sorted(set(values)))


@pytest.mark.parametrize(
    "value",
    [
        {"v": 1, "sql": "SELECT 1"},
        {"v": 1, "size": {"min": "20", "max": "10"}},
        {"v": 1, "modified": {"from": "2026-01-01"}},
        {"v": 1, "kind": ["file"] * 33},
    ],
)
def test_invalid_filters_fail_clearly(value: object) -> None:
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters(value)


@pytest.mark.parametrize("field", ["kind", "type", "availability"])
def test_empty_and_omitted_multiselects_are_the_same_filter(field: str) -> None:
    assert canonical_filter_bytes(parse_filters({"v": 1, field: []})) == b'{"v":1}'
    assert canonical_filter_bytes(parse_filters({"v": 1})) == b'{"v":1}'


def test_enum_multiselects_are_typed_sorted_and_deduplicated() -> None:
    filters = parse_filters(
        {
            "v": 1,
            "kind": [member.value for member in reversed(EntryKind)],
            "availability": [member.value for member in reversed(SourceState)],
        }
    )
    assert filters.kind == tuple(sorted(EntryKind, key=str))
    assert filters.availability == tuple(sorted(SourceState, key=str))


def test_unknown_type_token_does_not_collide_with_unknown_extension() -> None:
    filters = parse_filters({"v": 1, "type": ["unknown", "__unknown__"]})
    assert filters.type == ("__unknown__", "unknown")
    assert canonical_filter_bytes(filters) == (
        b'{"type":["__unknown__","unknown"],"v":1}'
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"v": True},
        {"v": 2},
        {"v": 1, "kind": None},
        {"v": 1, "kind": "file"},
        {"v": 1, "kind": ["ordinary"]},
        {"v": 1, "availability": ["unknown"]},
        {"v": 1, "type": ["JPG"]},
        {"v": 1, "type": ["a-b"]},
        {"v": 1, "type": ["a" * 17]},
        {"v": 1, "type": [1]},
    ],
)
def test_filter_shape_and_selection_scalars_are_strict(value: object) -> None:
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters(value)


def test_selection_count_is_checked_before_deduplication() -> None:
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters({"v": 1, "type": ["txt"] * 33})


def test_size_bounds_retain_unsigned_64_bit_integer_precision() -> None:
    filters = parse_filters({"v": 1, "size": {"min": "0", "max": "18446744073709551615"}})
    assert filters.size is not None
    assert filters.size.minimum == 0
    assert filters.size.maximum == 2**64 - 1
    assert json.loads(canonical_filter_bytes(filters)) == {
        "size": {"max": "18446744073709551615", "min": "0"},
        "v": 1,
    }


@pytest.mark.parametrize(
    "value",
    [
        {"min": -1},
        {"min": True},
        {"min": 1},
        {"min": 1.0},
        {"min": float("nan")},
        {"min": "-1"},
        {"min": "01"},
        {"max": str(2**64)},
        {"minimum": "1"},
        {"unknown": 1},
        {"unknown": None},
        {},
    ],
)
def test_size_range_rejects_imprecise_or_noncanonical_values(value: object) -> None:
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters({"v": 1, "size": value})


@pytest.mark.parametrize(
    ("field", "bound"),
    [
        ("size", {"min": "0"}),
        ("modified", {"from": "2026-01-01T00:00:00Z"}),
    ],
)
def test_explicit_unknown_metadata_is_distinct_and_excludes_bounds(
    field: str, bound: dict[str, str]
) -> None:
    unknown = parse_filters({"v": 1, field: {"unknown": True}})
    assert canonical_filter_bytes(unknown) != canonical_filter_bytes(parse_filters({"v": 1}))
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters({"v": 1, field: {"unknown": True, **bound}})


def test_one_sided_size_ranges_are_supported() -> None:
    assert canonical({"v": 1, "size": {"min": "7"}}) == {
        "size": {"min": "7"},
        "v": 1,
    }
    assert canonical({"v": 1, "size": {"max": "7"}}) == {
        "size": {"max": "7"},
        "v": 1,
    }


def test_dates_require_offsets_normalize_to_exact_signed_utc_nanoseconds() -> None:
    filters = parse_filters(
        {
            "v": 1,
            "modified": {
                "from": "1969-12-31T19:00:00.000000001-05:00",
                "before": "1970-01-01T01:00:00.000000002+01:00",
            },
        }
    )
    assert filters.modified is not None
    assert filters.modified.from_ns == 1
    assert filters.modified.before_ns == 2
    assert canonical_filter_bytes(filters) == (
        b'{"modified":{"before":"2","from":"1"},"v":1}'
    )


def test_pre_epoch_date_keeps_negative_nanosecond_precision() -> None:
    filters = parse_filters({"v": 1, "modified": {"from": "1969-12-31T23:59:59.999999999Z"}})
    assert filters.modified is not None
    assert filters.modified.from_ns == -1


def test_one_sided_modified_ranges_and_signed_limits_are_supported() -> None:
    lower = "1677-09-21T00:12:43.145224192Z"
    upper = "2262-04-11T23:47:16.854775807Z"
    assert canonical({"v": 1, "modified": {"from": lower}})["modified"] == {
        "from": str(-(2**63))
    }
    assert canonical({"v": 1, "modified": {"before": upper}})["modified"] == {
        "before": str(2**63 - 1)
    }


@pytest.mark.parametrize(
    "value",
    [
        {"from": "2026-01-01"},
        {"from": "2026-01-01T00:00:00"},
        {"from": "2026-01-01T00:00:00+24:00"},
        {"from": "2026-01-01T00:00:00.0000000001Z"},
        {"from": "1677-09-21T00:12:43.145224191Z"},
        {"before": "2262-04-11T23:47:16.854775808Z"},
        {"from": True},
        {"from": 0},
        {"from": float("nan")},
        {"from": "2026-01-02T00:00:00Z", "before": "2026-01-01T00:00:00Z"},
        {"from": "2026-01-01T00:00:00Z", "before": "2026-01-01T00:00:00Z"},
        {"unknown": False},
        {"until": "2026-01-01T00:00:00Z"},
        {},
    ],
)
def test_modified_range_rejects_invalid_or_empty_bounds(value: object) -> None:
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters({"v": 1, "modified": value})


def test_prefix_is_literal_display_text_with_name_order_normalization() -> None:
    filters = parse_filters({"v": 1, "prefix": "%_\\E\u0301Stra\u00dfe"})
    assert filters.prefix == "%_\\\u00e9strasse"
    assert json.loads(canonical_filter_bytes(filters))["prefix"] == "%_\\\u00e9strasse"


def test_empty_prefix_normalizes_to_no_restriction() -> None:
    assert canonical_filter_bytes(parse_filters({"v": 1, "prefix": ""})) == b'{"v":1}'


@pytest.mark.parametrize("value", [None, True, 1, b"name", "a" * 2049, "\udcff"])
def test_prefix_rejects_wrong_types_invalid_unicode_and_more_than_2kib(value: object) -> None:
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters({"v": 1, "prefix": value})


def test_prefix_accepts_exactly_2kib_without_path_component_validation() -> None:
    prefix = "/" + "a" * 2047
    assert parse_filters({"v": 1, "prefix": prefix}).prefix == prefix


def test_normalized_filter_document_is_bounded() -> None:
    filters = parse_filters({"v": 1, "prefix": "\u0130" * 683})
    assert len(filters.prefix.encode()) > 2048 if filters.prefix is not None else False
    assert len(canonical_filter_bytes(filters)) <= 8192

    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters({"v": 1, "prefix": "\x01" * 2048})


def test_raw_filter_budget_precedes_duplicate_and_escape_normalization() -> None:
    value = oversized_raw_filter("\x01")
    assert len(raw_filter_bytes(value)) == 8849
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters(value)


def test_raw_filter_budget_counts_delete_as_an_ascii_json_escape() -> None:
    value = oversized_raw_filter("\x7f")
    assert len(raw_filter_bytes(value)) == 8849
    with pytest.raises(ValueError, match="invalid filters"):
        parse_filters(value)


def test_raw_filter_budget_accepts_exact_8192_byte_document() -> None:
    empty = {"v": 1, "prefix": ""}
    remaining = 8192 - len(raw_filter_bytes(empty))
    prefix = "\x01" * (remaining // 6) + "a" * (remaining % 6)
    value = {"v": 1, "prefix": prefix}
    assert len(prefix.encode()) <= 2048
    assert len(raw_filter_bytes(value)) == 8192
    assert len(canonical_filter_bytes(parse_filters(value))) == 8192


@pytest.mark.parametrize(
    "value",
    [
        '"\\',
        "\b\f\n\r\t",
        "\x00\x01\x1f",
        "\x7f",
        "\u00e9",
        "\U0001f642",
        {"outer": [None, True, False, -17, '"', {"inner": "\x00\u00e9\U0001f642"}]},
    ],
)
def test_raw_size_accounting_matches_ascii_json_for_every_escape_class(
    value: object,
) -> None:
    expected = len(raw_filter_bytes(value))
    assert _ascii_json_size(value, expected) == expected
    with pytest.raises(ValueError, match="invalid filters"):
        _ascii_json_size(value, expected - 1)
