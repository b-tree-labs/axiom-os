# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""TableSpec 2.0 (ADR-217): additive over 1.0, and one text form per cell type on every surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.scidisplay import table_spec as ts

PARITY = json.loads((Path(__file__).parent / "data" / "table_cell_parity.json").read_text())

#: 1.0 documents as callers have written them: minimal, sorted and filtered, with phase 1's layout.
DOCS_1_0 = [
    {"schema_version": "1.0", "kind": "rows", "title": "t",
     "columns": [{"id": "a", "label": "A", "sortable": False, "align": "left"}],
     "page": {"number": 1, "size": 25}},
    {"schema_version": "1.0", "kind": "rows", "title": "Readings",
     "columns": [{"id": "ts", "label": "Time", "sortable": True, "align": "left"},
                 {"id": "value", "label": "Value", "sortable": True, "align": "right",
                  "uncertainty_from": "sigma"}],
     "page": {"number": 3, "size": 50},
     "sort": {"column": "value", "direction": "desc"},
     "filter": {"search": "fuel", "equals": [{"column": "ts", "value": "x"}]}},
    {"schema_version": "1.0", "kind": "rows", "title": "Files",
     "columns": [{"id": "name", "label": "Name", "sortable": True, "align": "left", "width": 240,
                  "min_width": 120, "max_width": 600, "resizable": False, "pinned": "left", "type": "text"}],
     "page": {"number": 1, "size": 25}, "table_id": "library.files"},
]


def _door(**over):
    doc = {
        "schema_version": "2.0", "kind": "rows", "title": "Readings", "id": "tbl_readings",
        "source": {"type": "verb", "verb": "data.table", "args": {"dataset": "readings"}},
        "columns": [
            {"id": "ts", "label": "Time", "sortable": True, "align": "left", "type": "time"},
            {"id": "value", "label": "Value", "sortable": True, "align": "right", "type": "quantity",
             "unit": "kW", "uncertainty_from": "sigma", "profile": True, "filterable": True,
             "format": {"digits": "auto"}, "colorscale": {"palette": "cividis", "domain": "column"}},
            {"id": "quality", "label": "Quality", "sortable": False, "align": "left", "type": "quality"},
            {"id": "site", "label": "Site", "sortable": True, "align": "left"},
        ],
        "page": {"number": 1, "size": 25},
        "sort": [{"column": "site", "direction": "asc"}, {"column": "ts", "direction": "desc"}],
        "filter": {"where": [{"column": "value", "op": "between", "value": [0, 100]},
                             {"column": "quality", "op": "in", "value": ["good", "suspect"]},
                             {"column": "site", "op": "not_null"}]},
        "window": {"offset": 0, "limit": 200, "cursor": None},
        "links": {"charts": ["fig_1"]},
    }
    doc.update(over)
    return doc


class TestEveryOneZeroDocumentParsesUnchanged:
    @pytest.mark.parametrize("doc", DOCS_1_0)
    def test_it_parses_and_writes_back_byte_for_byte(self, doc):
        spec = ts.parse_document(doc)
        assert spec.schema_version == "1.0"
        assert json.dumps(spec.to_document(), sort_keys=True) == json.dumps(doc, sort_keys=True)

    def test_a_one_zero_document_may_not_carry_a_two_zero_key(self):
        doc = dict(DOCS_1_0[0], window={"offset": 0, "limit": 10, "cursor": None})
        with pytest.raises(ts.UnknownFieldError, match="need schema_version '2.0'"):
            ts.parse_document(doc)

    def test_nor_a_two_zero_column_field(self):
        doc = json.loads(json.dumps(DOCS_1_0[0]))
        doc["columns"][0]["unit"] = "kW"
        with pytest.raises(ts.UnknownFieldError):
            ts.parse_document(doc)

    def test_built_in_code_a_spec_using_two_zero_fields_must_say_two_zero(self):
        with pytest.raises(ts.SpecFormatError, match="need schema_version '2.0'"):
            ts.TableSpec(kind="rows", title="t", schema_version="1.0",
                         columns=(ts.Column(id="a", label="A", type="quantity"),))

    def test_tabulate_still_says_one_zero_when_it_uses_nothing_new(self):
        out = ts.tabulate([{"a": 1}], columns=[("a", "A")], title="t")
        assert out["spec"]["schema_version"] == "1.0"


class TestTwoZeroRoundTrips:
    def test_every_new_field_survives_parse_and_emit(self):
        doc = _door()
        spec = ts.parse_document(doc)
        assert json.dumps(spec.to_document(), sort_keys=True) == json.dumps(doc, sort_keys=True)

    def test_multi_key_sort(self):
        spec = ts.parse_document(_door())
        assert [s.column for s in spec.sorts] == ["site", "ts"]
        assert spec.sort == spec.sorts[0]  # 1.0 callers read spec.sort

    def test_a_single_sort_object_still_parses(self):
        spec = ts.parse_document(_door(sort={"column": "ts", "direction": "desc"}))
        assert spec.sorts == (ts.Sort("ts", "desc"),)
        assert spec.to_document()["sort"] == {"column": "ts", "direction": "desc"}

    def test_a_width_object(self):
        doc = _door()
        doc["columns"][0]["width"] = {"initial": 120, "min": 60, "max": 480}
        col = ts.parse_document(doc).columns[0]
        assert (col.width, col.min_width, col.max_width) == (120, 60, 480)

    def test_table_id_and_id_are_one_name(self):
        assert ts.parse_document(_door(table_id="tbl_readings")).table_id == "tbl_readings"
        with pytest.raises(ts.SpecFormatError, match="disagree"):
            ts.parse_document(_door(table_id="other"))

    def test_equals_takes_the_compact_pair(self):
        spec = ts.parse_document(_door(filter={"equals": [["site", "a"]]}))
        assert spec.filter.equals == (("site", "a"),)


class TestWhereIsAClosedSet:
    @pytest.mark.parametrize("op", ts.WHERE_OPS)
    def test_each_operator_parses(self, op):
        value = {"between": [1, 2], "in": [1], "not_in": [1], "is_null": None, "not_null": None,
                 "contains": "x", "starts_with": "x"}.get(op, 1)
        column = "site" if op in ("contains", "starts_with") else "value"
        pred = {"column": column, "op": op} | ({} if value is None else {"value": value})
        ts.parse_document(_door(filter={"where": [pred]}))

    def test_an_unknown_operator_is_refused(self):
        with pytest.raises(ts.SpecFormatError, match="not one of"):
            ts.parse_document(_door(filter={"where": [{"column": "value", "op": "like", "value": "%"}]}))

    def test_an_unknown_column_is_refused(self):
        with pytest.raises(ts.SpecFormatError, match="does not have"):
            ts.parse_document(_door(filter={"where": [{"column": "nope", "op": "eq", "value": 1}]}))

    @pytest.mark.parametrize("pred", [
        {"column": "value", "op": "between", "value": [1]},
        {"column": "value", "op": "in", "value": []},
        {"column": "value", "op": "is_null", "value": 1},
        {"column": "value", "op": "eq", "value": "ten"},     # a number column holds numbers
        {"column": "value", "op": "contains", "value": "1"},  # a text operator on a number
        {"column": "value", "op": "eq", "value": {"$gt": 1}},
    ])
    def test_values_are_typed_by_the_column_and_the_operator(self, pred):
        with pytest.raises(ts.SpecFormatError):
            ts.parse_document(_door(filter={"where": [pred]}))

    def test_in_memory_evaluation_follows_sql_null_logic(self):
        f = ts.Filter(where=({"column": "v", "op": "ne", "value": 1},))
        assert f.matches({"v": 2}) and not f.matches({"v": None})
        assert ts.Filter(where=({"column": "v", "op": "is_null"},)).matches({"v": None})

    def test_tabulate_applies_where_to_inline_rows(self):
        rows = [{"v": i} for i in range(10)]
        out = ts.tabulate(rows, columns=[("v", "V", True)], title="t",
                          where=[{"column": "v", "op": "between", "value": [3, 5]}])
        assert [r["v"] for r in out["rows"]] == [3, 4, 5]


class TestSourcesAndWindows:
    def test_a_window_is_bounded_like_a_page(self):
        with pytest.raises(ts.SpecFormatError, match="capped"):
            ts.parse_document(_door(window={"offset": 0, "limit": 5000, "cursor": None}))

    def test_a_verb_source_names_a_verb(self):
        with pytest.raises(ts.SpecFormatError):
            ts.parse_document(_door(source={"type": "verb", "args": {}}))

    def test_a_door_source_never_uses_the_in_memory_sort_window(self):
        spec = ts.parse_document(_door())
        with pytest.raises(ts.DoorSortWindowError):
            ts.rows_needed(spec.page, sorted_=True, source=spec.source)
        assert ts.rows_needed(spec.page, sorted_=True) == 10_000  # inline sources keep 1.0's rule

    def test_grouping_is_carried_and_checked(self):
        spec = ts.parse_document(_door(group={"by": ["site"], "aggregates": [{"column": "*", "fn": "count"}]}))
        assert spec.to_document()["group"]["by"] == ["site"]
        with pytest.raises(ts.SpecFormatError):
            ts.parse_document(_door(group={"by": ["nope"]}))


class TestTheWebComponentGetsTheSameDocument:
    def test_props(self):
        spec = ts.parse_document(_door())
        props = spec.to_datatable_props()
        assert props["sortRules"] == [{"column": "site", "direction": "asc"}, {"column": "ts", "direction": "desc"}]
        assert props["tableId"] == "tbl_readings"
        value = next(c for c in props["columns"] if c["id"] == "value")
        assert value["unit"] == "kW" and value["profile"] is True and value["type"] == "quantity"
        assert props["filter"]["where"][0] == {"column": "value", "op": "between", "value": [0, 100]}
        # A door spec carries itself, which is what doorRowSource(spec) posts with each window.
        assert props["spec"] == spec.to_document()


class TestTerminalParity:
    @pytest.mark.parametrize("case", PARITY["cells"], ids=[c["name"] for c in PARITY["cells"]])
    def test_cell_text_matches_the_shared_vectors(self, case):
        props = case["column"]
        col = ts.Column(id=props["id"], label=props["label"], type=props.get("type", ""),
                        unit=props.get("unit", ""), uncertainty_from=props.get("uncertainty_from", ""),
                        format=props.get("format") or {})
        assert ts.cell_text(col, case["row"]) == case["text"]

    @pytest.mark.parametrize("case", PARITY["counts"], ids=[str(c["count"]) for c in PARITY["counts"]])
    def test_count_text_matches_the_shared_vectors(self, case):
        assert ts.count_text(case["count"]) == case["text"]

    def test_a_bad_reading_is_never_drawn_as_its_sentinel(self):
        spec = ts.parse_document(_door(filter=None))
        text = ts.render_text(spec, [{"ts": "t", "value": 0, "sigma": None, "quality": "bad",
                                      "quality_reason": "sentinel:0", "site": "a"}])
        row = [line for line in text.splitlines() if "sentinel" in line][0]
        assert "bad (sentinel:0)" in row and " 0 kW" not in row

    def test_multi_key_sort_markers_carry_their_place(self):
        text = ts.render_text(ts.parse_document(_door(filter=None)), [])
        assert "SITE ↑1" in text and "TIME ↓2" in text

    def test_a_door_footer_states_the_count_and_never_claims_a_window_sort(self):
        spec = ts.parse_document(_door(filter=None))
        text = ts.render_text(spec, [{"ts": "t", "value": 1, "sigma": 0.1, "quality": "good", "site": "a"}],
                              count={"kind": "estimate", "n": 101_606_332}, complete=False,
                              notes=["window limited to 200 rows (ADR-157)"])
        assert "rows 1–1 of ~101.6 million rows (estimated)" in text
        assert "window limited to 200 rows (ADR-157)" in text
        assert "sorted in the database over every matching row" in text
        assert "sorted over the rows fetched" not in text
