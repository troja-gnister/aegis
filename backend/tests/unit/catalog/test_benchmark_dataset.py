"""Deterministic, streamed synthetic catalog generation for the 2A.1 benchmark."""
from __future__ import annotations

import inspect
import uuid
from collections import Counter
from itertools import islice

import pytest
from aegis_apps.catalog.names import source_name

from scripts.benchmarks.dataset import (
    ROOTS,
    STALE_FIXTURES,
    DatasetShape,
    catalog_records,
    dataset_counts,
    directory_targets,
    expected_children,
)

SMALL = DatasetShape(entries=6000, wide_folder=1500, seed=20260914)


def test_catalog_generation_is_deterministic_and_streamed() -> None:
    shape = DatasetShape(entries=1000000, wide_folder=50000, seed=20260914)
    first = list(islice(catalog_records(shape), 1000))
    second = list(islice(catalog_records(shape), 1000))
    assert first == second
    assert len({row["id"] for row in first}) == 1000
    assert inspect.isgenerator(catalog_records(shape))


def test_different_seeds_produce_different_identities() -> None:
    other = DatasetShape(entries=SMALL.entries, wide_folder=SMALL.wide_folder, seed=7)
    first = {row["id"] for row in islice(catalog_records(SMALL), 200)}
    second = {row["id"] for row in islice(catalog_records(other), 200)}
    assert not first & second


@pytest.mark.parametrize(
    ("entries", "wide", "seed"),
    [
        (0, 10, 1), (100, 0, 1), (100, 200, 1), (1000, 100, -1), (1000, 100, 2**63),
        (20_000_001, 100, 1), (1000, 1_000_001, 1), (True, 100, 1), (1000, False, 1),
        (1000.0, 100, 1), (1000, 100, "1"),
    ],
)
def test_impossible_or_untyped_shapes_are_refused(entries: object, wide: object,
                                                  seed: object) -> None:
    with pytest.raises(ValueError, match="benchmark dataset shape"):
        DatasetShape(entries=entries, wide_folder=wide, seed=seed)  # type: ignore[arg-type]


def test_small_shape_has_exact_reported_counts_and_a_valid_tree() -> None:
    records = list(catalog_records(SMALL))
    assert len(records) == SMALL.entries
    counts = dataset_counts(SMALL)
    assert counts["entries"] == SMALL.entries
    kinds = Counter(row["kind"] for row in records)
    states = Counter(row["source_state"] for row in records)
    assert counts["kinds"] == dict(sorted(kinds.items()))
    assert counts["states"] == dict(sorted(states.items()))
    assert kinds["directory"] > 3 and kinds["file"] > kinds["directory"]
    assert kinds["symlink"] >= 1 and kinds["special"] >= 1
    assert states["missing"] >= 1 and states["inaccessible"] >= 1
    by_id = {row["id"]: row for row in records}
    assert len(by_id) == SMALL.entries
    seen: set[object] = set()
    anchors = []
    locations = set()
    for row in records:
        parent = row["source_parent_id"]
        assert row["logical_parent_id"] == parent
        if parent is None:
            anchors.append(row)
            assert row["raw_name"] == b"" and row["kind"] == "directory"
            assert row["source_parent_revision"] is None
        else:
            assert parent in seen, "parents must stream before their children"
            assert by_id[parent]["kind"] == "directory"
            assert by_id[parent]["root"] == row["root"]
            location = (parent, row["raw_name"])
            assert location not in locations
            locations.add(location)
            name = source_name(row["raw_name"])  # type: ignore[arg-type]
            assert (row["display_name"], row["name_key"], row["type_hint"]) == (
                name.display, name.order_key, name.type_hint,
            )
        seen.add(row["id"])
    assert sorted(str(row["root"]) for row in anchors) == sorted(root.key for root in ROOTS)


def test_ancestry_markers_follow_the_task6_contract_except_labeled_stale_fixtures() -> None:
    records = list(catalog_records(SMALL))
    by_id = {row["id"]: row for row in records}
    stale = {row["id"]: row for row in records if row["display_name"] in STALE_FIXTURES}
    assert {row["display_name"] for row in stale.values()} == set(STALE_FIXTURES)
    unknown = stale_by_name(stale, "stale-unknown-marker")
    mismatched = stale_by_name(stale, "stale-mismatched-marker")
    inaccessible = stale_by_name(stale, "inaccessible-directory")
    assert unknown["source_parent_revision"] is None
    parent = by_id[mismatched["source_parent_id"]]
    assert mismatched["source_parent_revision"] != parent["source_revision"]
    assert inaccessible["source_state"] == "inaccessible"
    for row in records:
        if row["source_parent_id"] is None or row["id"] in stale:
            continue
        assert row["source_parent_revision"] == by_id[row["source_parent_id"]]["source_revision"]
    for fixture in stale.values():
        identity = fixture["id"]
        assert isinstance(identity, uuid.UUID)
        assert expected_children(SMALL, identity), "stale fixtures must have children"


def stale_by_name(stale: dict[object, dict[str, object]], name: str) -> dict[str, object]:
    return next(row for row in stale.values() if row["display_name"] == name)


def test_wide_folder_and_name_variety_cover_the_adversarial_cases() -> None:
    targets = directory_targets(SMALL)
    wide = [target for target in targets if target.label == "wide"]
    assert len(wide) == 1 and wide[0].children == SMALL.wide_folder
    children = [row for row in catalog_records(SMALL) if row["source_parent_id"] == wide[0].id]
    assert len(children) == SMALL.wide_folder
    raws = [row["raw_name"] for row in children]
    assert any(len(raw) == 255 for raw in raws)  # type: ignore[arg-type]
    assert any(b"\xff" in raw for raw in raws)  # type: ignore[operator]
    assert any(not raw.isascii() for raw in raws)  # type: ignore[attr-defined]
    assert any(b"%" in raw for raw in raws)  # type: ignore[operator]
    keys = Counter(row["name_key"] for row in children)
    assert any(count > 1 for count in keys.values()), "case-adjacent names tie on the key"
    types = Counter(row["type_hint"] for row in children)
    assert types["jpg"] > SMALL.wide_folder // 4
    assert types[None] > 0 and types["unknown"] > 0
    assert 0 < types["xyz"] < SMALL.wide_folder // 100
    assert any(row["size"] is None for row in children)
    assert any(row["mtime_ns"] is None for row in children)
    large = [row for row in children if row["size"] is not None
             and row["size"] >= 2**40]  # type: ignore[operator]
    assert 0 < len(large) < SMALL.wide_folder // 100, "rare large sizes"
    sizes = Counter(row["size"] for row in children if row["size"] is not None)
    assert max(sizes.values()) > 10, "size ties"


def test_directory_targets_match_generated_parents_and_grants_are_explicit() -> None:
    records = list(catalog_records(SMALL))
    child_counts = Counter(row["source_parent_id"] for row in records)
    targets = directory_targets(SMALL)
    assert {target.label for target in targets} >= {"anchor", "wide", "tree", "stale"}
    for target in targets:
        assert child_counts[target.id] == target.children
        assert target.root in {root.key for root in ROOTS}
    assert sum(1 for root in ROOTS if root.direct_users and root.group_users) >= 1
    assert all(root.direct_users or root.group_users for root in ROOTS)
    users = {user for root in ROOTS for user in (*root.direct_users, *root.group_users)}
    assert users == set(range(1, 11))


def test_expected_children_sort_like_the_browse_query() -> None:
    wide = next(target for target in directory_targets(SMALL) if target.label == "wide")
    rows = expected_children(SMALL, wide.id)
    assert all(row["source_state"] != "missing" for row in rows)
    keys = [(row["kind"] != "directory", row["name_key"], row["id"]) for row in rows]
    assert keys == sorted(keys)
    by_size = expected_children(SMALL, wide.id, sort="size", order="desc")
    assert {row["id"] for row in by_size} == {row["id"] for row in rows}
    assert by_size[0]["kind"] == "directory"
    files = [row for row in by_size if row["kind"] != "directory"]
    known = [row["size"] for row in files if row["size"] is not None]
    assert known == sorted(known, reverse=True)  # type: ignore[type-var]
    assert files[-1]["size"] is None, "null sizes sort last"
