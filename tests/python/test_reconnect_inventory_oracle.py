"""Reconnect inventory oracle tests."""

import copy
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def cycle(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "examples"))
    return importlib.import_module("villager_cycle")


def item(protocol, item_id, count, net_id, *, data=b"\0" * 10):
    return {
        "protocol": protocol,
        "kind": f"serialized_{protocol}",
        "id": item_id,
        "stack_size": count,
        "aux_value": 0,
        "block_runtime_id": 0,
        "user_data_buffer": data,
        "net_id": net_id,
        "net_id_kind": None if not item_id else "stack" if protocol == 1001 else "raw",
    }


def records(protocol):
    rows = [
        {
            "index": index,
            "state": "empty",
            "identifier": None,
            "count": 0,
            "stack_id": 0,
            "item": item(protocol, 0, 0, None, data=b""),
        }
        for index in range(36)
    ]
    rows[0] = {
        "index": 0,
        "state": "occupied",
        "identifier": "minecraft:clock",
        "count": 1,
        "stack_id": 36,
        "item": item(protocol, 396, 1, 36, data=b"opaque named clock"),
    }
    for index, stack_id in [(1, 37), (2, 38)]:
        rows[index] = {
            "index": index,
            "state": "occupied",
            "identifier": "minecraft:wheat",
            "count": 4,
            "stack_id": stack_id,
            "item": item(protocol, 337, 4, stack_id),
        }
    return rows


def check(cycle, before, after):
    helper = getattr(cycle, "check_reconnected_inventory", None)
    assert callable(helper), "check_reconnected_inventory is missing; behavioral comparison has not run"
    original_before, original_after = copy.deepcopy(before), copy.deepcopy(after)
    try:
        helper(before, after)
    finally:
        assert before == original_before and after == original_after, "comparison mutated raw records"


@pytest.mark.parametrize("protocol", [1001, 2193])
def test_original_same_session_boundary_rejects_observed_id_only_change(cycle, protocol):
    before = records(protocol)
    after = copy.deepcopy(before)
    after[0]["stack_id"] = after[0]["item"]["net_id"] = 105
    assert cycle.totals(before) == cycle.totals(after)
    cycle.check_cycle_inventory(before, before, [])
    with pytest.raises(RuntimeError, match="cycle changed unrelated inventory"):
        cycle.check_cycle_inventory(before, after, [])


@pytest.mark.parametrize("protocol", [1001, 2193])
def test_confirmed_stack_ids_may_renumber_across_sessions(cycle, protocol):
    before = records(protocol)
    after = copy.deepcopy(before)
    for index, stack_id in [(0, 105), (1, 106), (2, 107)]:
        after[index]["stack_id"] = after[index]["item"]["net_id"] = stack_id
    check(cycle, before, after)


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize("representation", ["confirmed", "absent", "zero", "negative", "empty_absent"])
def test_unchanged_id_and_confirmation_representations_compare_strictly(cycle, protocol, representation):
    before = records(protocol)
    if representation in {"absent", "zero", "negative"}:
        before[0]["stack_id"] = None
        before[0]["item"]["net_id"] = {"absent": None, "zero": 0, "negative": -7}[representation]
        before[0]["item"]["net_id_kind"] = (
            None if representation == "absent" else "request" if protocol == 1001 else "raw"
        )
    elif representation == "empty_absent":
        before[35]["stack_id"] = None
    check(cycle, before, copy.deepcopy(before))


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize(
    "ids",
    [
        (0, 0, 105, 105),
        (None, None, 105, 105),
        (-7, -7, 105, 105),
        (True, True, 105, 105),
        (36.0, 36.0, 105, 105),
        (36, 99, 105, 105),
        (36, 36, 105, 999),
        (36, 36, 0, 0),
        (36, 36, None, None),
        (36, 36, -7, -7),
        (36, 36, True, True),
        (36, 36, 105.0, 105.0),
    ],
)
def test_only_positive_integer_ids_confirmed_on_both_sides_can_be_ignored(cycle, protocol, ids):
    before = records(protocol)
    after = copy.deepcopy(before)
    before[0]["stack_id"], before[0]["item"]["net_id"], after[0]["stack_id"], after[0]["item"]["net_id"] = ids
    with pytest.raises(RuntimeError):
        check(cycle, before, after)


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize(
    "field,value",
    [
        ("protocol", 999),
        ("kind", "network"),
        ("id", 999),
        ("stack_size", 2),
        ("aux_value", 1),
        ("block_runtime_id", 5),
        ("user_data_buffer", b"changed metadata"),
        ("net_id_kind", "legacy_request"),
        ("net_id_kind", None),
    ],
)
def test_item_content_and_metadata_changes_reject_even_with_equal_totals(cycle, protocol, field, value):
    before = records(protocol)
    after = copy.deepcopy(before)
    after[0]["stack_id"] = after[0]["item"]["net_id"] = 105
    after[0]["item"][field] = value
    assert cycle.totals(before) == cycle.totals(after)
    with pytest.raises(RuntimeError):
        check(cycle, before, after)


@pytest.mark.parametrize("protocol", [1001, 2193])
@pytest.mark.parametrize(
    "case",
    [
        "identifier",
        "count",
        "balanced_counts",
        "index",
        "row_order",
        "slot_movement",
        "duplicate_index",
        "short_inventory",
        "long_inventory",
        "unknown_before",
        "unknown_after",
        "unknown_both",
        "empty_to_occupied",
        "occupied_to_empty",
        "missing_row_field",
        "missing_item_field",
        "extra_row_field",
        "extra_item_field",
        "empty_id_representation",
        "empty_kind_representation",
        "item_absence",
    ],
)
def test_all_slots_order_presence_state_and_content_remain_exact(cycle, protocol, case):
    before = records(protocol)
    after = copy.deepcopy(before)
    if case == "identifier":
        after[0]["identifier"] = "minecraft:compass"
    elif case == "count":
        after[0]["count"] = 2
    elif case == "balanced_counts":
        after[1]["count"] = after[1]["item"]["stack_size"] = 3
        after[2]["count"] = after[2]["item"]["stack_size"] = 5
        assert cycle.totals(before) == cycle.totals(after)
    elif case == "index":
        after[0]["index"] = 35
    elif case == "row_order":
        after[1], after[2] = after[2], after[1]
    elif case == "slot_movement":
        after[0], after[4] = after[4], after[0]
        after[0]["index"], after[4]["index"] = 0, 4
        assert cycle.totals(before) == cycle.totals(after)
    elif case == "duplicate_index":
        after[35]["index"] = 34
    elif case == "short_inventory":
        after.pop()
    elif case == "long_inventory":
        after.append(copy.deepcopy(after[-1]))
    elif case.startswith("unknown"):
        for target in [before] if case == "unknown_before" else [after] if case == "unknown_after" else [before, after]:
            target[35].update(state="unknown", count=None, stack_id=None, item=None)
    elif case == "empty_to_occupied":
        after[35] = {**copy.deepcopy(after[0]), "index": 35}
    elif case == "occupied_to_empty":
        after[0] = {**copy.deepcopy(after[35]), "index": 0}
    elif case == "missing_row_field":
        del after[0]["stack_id"]
    elif case == "missing_item_field":
        del after[0]["item"]["user_data_buffer"]
    elif case == "extra_row_field":
        after[0]["extra"] = None
    elif case == "extra_item_field":
        after[0]["item"]["extra"] = None
    elif case == "empty_id_representation":
        after[35]["stack_id"] = None
    elif case == "empty_kind_representation":
        after[35]["item"]["net_id_kind"] = "raw" if protocol == 2193 else "stack"
    elif case == "item_absence":
        after[35]["item"] = None
    with pytest.raises(RuntimeError):
        check(cycle, before, after)
