# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the bronze→gold promotion engine (ADR-001 P3): declared map,
EAV pivot, field-level source precedence, and SCD-2 supersession. Pure + no DB.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.promote import (
    InMemoryGoldTable,
    Pivot,
    PromotionMap,
    parse_promotion_map,
    promote,
    promote_rows,
)

# ---- map: parse + validate ------------------------------------------------


def test_parse_map_from_toml_shape():
    pmap = parse_promotion_map({
        "promotion": {
            "target": "gold.series",
            "natural_key": ["obs_date"],
            "source_precedence": ["db.v1", "csv.v1"],
            "scd2": True,
            "columns": {"obs_date": "slot", "predicted": "m.pred", "measured": "m.meas"},
            "pivot": {"key": "slot", "name": "name", "value": "value"},
        }
    })
    assert pmap.target == "gold.series"
    assert pmap.natural_key == ("obs_date",)
    assert pmap.pivot == Pivot("slot", "name", "value")
    assert pmap.validate() == []


def test_validate_flags_natural_key_not_in_columns():
    pmap = PromotionMap(target="t", natural_key=("id",), columns={"other": "x"})
    assert any("natural_key column 'id'" in e for e in pmap.validate())


def test_validate_flags_incomplete_pivot():
    pmap = PromotionMap(target="t", natural_key=("k",), columns={"k": "k"},
                        pivot=Pivot("k", "", "value"))
    assert any("[promotion.pivot] requires 'name'" in e for e in pmap.validate())


# ---- EAV pivot + field-level precedence (the anonymized stress test) -------


def test_eav_pivot_merges_two_sources_by_precedence():
    # predicted comes from the higher-precedence 'db' source; measured from 'csv'.
    pmap = PromotionMap(
        target="gold.series",
        natural_key=("obs_date",),
        columns={"obs_date": "slot", "predicted": "m.pred", "measured": "m.meas"},
        pivot=Pivot("slot", "name", "value"),
        source_precedence=("db.v1", "csv.v1"),
    )
    rows = [
        {"schema_ref": "db.v1", "slot": "2026-04-01", "name": "m.pred", "value": 654},
        {"schema_ref": "csv.v1", "slot": "2026-04-01", "name": "m.meas", "value": 487},
    ]
    gold = InMemoryGoldTable(pmap)
    res = promote(rows, pmap, gold, run_id="r1", now="2026-04-01T08:00:00Z")
    assert len(res.inserted) == 1
    row = gold.current_rows()[0]
    assert row.values == {"obs_date": "2026-04-01", "predicted": 654, "measured": 487}
    assert row.run_id == "r1" and row.is_current and row.valid_to is None


def test_precedence_overrides_conflicting_field():
    pmap = PromotionMap(
        target="t", natural_key=("k",),
        columns={"k": "k", "v": "v"},
        source_precedence=("hi", "lo"),
    )
    rows = [
        {"schema_ref": "lo", "k": "x", "v": 1},
        {"schema_ref": "hi", "k": "x", "v": 2},   # higher precedence wins
    ]
    gold = InMemoryGoldTable(pmap)
    promote(rows, pmap, gold, run_id="r1", now="t0")
    assert gold.current_rows()[0].values["v"] == 2


def test_unknown_schema_ref_ranks_lowest():
    pmap = PromotionMap(target="t", natural_key=("k",), columns={"k": "k", "v": "v"},
                        source_precedence=("known",))
    rows = [
        {"schema_ref": "mystery", "k": "x", "v": 9},
        {"schema_ref": "known", "k": "x", "v": 5},
    ]
    gold = InMemoryGoldTable(pmap)
    promote(rows, pmap, gold, run_id="r1", now="t0")
    assert gold.current_rows()[0].values["v"] == 5


# ---- SCD-2 ----------------------------------------------------------------


def _simple_map(scd2=True):
    return PromotionMap(target="t", natural_key=("k",), columns={"k": "k", "v": "v"}, scd2=scd2)


def test_scd2_insert_then_unchanged_then_supersede():
    pmap = _simple_map()
    gold = InMemoryGoldTable(pmap)

    r1 = promote([{"schema_ref": "s", "k": "x", "v": 1}], pmap, gold, run_id="r1", now="t1")
    assert len(r1.inserted) == 1 and r1.unchanged == 0

    r2 = promote([{"schema_ref": "s", "k": "x", "v": 1}], pmap, gold, run_id="r2", now="t2")
    assert r2.inserted == [] and r2.unchanged == 1        # same value → no-op

    r3 = promote([{"schema_ref": "s", "k": "x", "v": 2}], pmap, gold, run_id="r3", now="t3")
    assert r3.superseded == [("x",)] and len(r3.inserted) == 1

    # exactly one current row (v=2); the old v=1 is closed with valid_to.
    current = gold.current_rows()
    assert len(current) == 1 and current[0].values["v"] == 2
    closed = [r for r in gold.rows if not r.is_current]
    assert len(closed) == 1 and closed[0].values["v"] == 1 and closed[0].valid_to == "t3"


def test_non_scd2_overwrites_without_history():
    pmap = _simple_map(scd2=False)
    gold = InMemoryGoldTable(pmap)
    promote([{"schema_ref": "s", "k": "x", "v": 1}], pmap, gold, run_id="r1", now="t1")
    promote([{"schema_ref": "s", "k": "x", "v": 2}], pmap, gold, run_id="r2", now="t2")
    assert len(gold.rows) == 1                            # no history kept
    assert gold.rows[0].values["v"] == 2


def test_promote_rows_is_pure_given_current_state():
    pmap = _simple_map()
    current = {("x",): "nope-different-hash"}
    res = promote_rows([{"schema_ref": "s", "k": "x", "v": 1}], pmap, current,
                       run_id="r1", now="t1")
    # a changed hash → supersede + insert, computed without any gold table object
    assert res.superseded == [("x",)] and len(res.inserted) == 1
