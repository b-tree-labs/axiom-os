# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-265 D7: ``data series`` and ``data aggregate`` print readings the way every reading prints.

They printed the raw envelope as JSON in text mode, so a person read ``"low": 0.5`` off a
nested dict, or nothing at all when no bound was served. Text now uses the reading family's
one renderer; ``--json`` is unchanged.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import friendly_text
from axiom.uncertainty.serving import Served

EXACT = Served(fn="max", low=0.5, high=0.5, quantified=1).payload()
NONE = Served(fn="mean", unquantified=4).payload()


def test_an_aggregate_is_one_reading_line_with_its_bound():
    value = {
        "data": {"table": "signals", "column": "value", "fn": "max", "value": 31.2},
        "provenance": {"source": "gold.signals", "method": "max(value)", "rows": 48},
        "unit": {"value": "degC"},
        "uncertainty": EXACT,
    }
    text = friendly_text.render("aggregate", value)
    assert "max(value) over gold.signals: 31.2 degC ± 0.5 degC" in text
    assert "48 rows" in text


def test_an_aggregate_with_no_served_bound_says_so():
    value = {
        "data": {"table": "signals", "column": "value", "fn": "mean", "value": 20.0},
        "provenance": {"source": "gold.signals", "method": "avg(value)", "rows": 4},
    }
    assert "20 (uncertainty not reported)" in friendly_text.render("aggregate", value)


def test_a_grouped_aggregate_is_a_table_with_an_uncertainty_column():
    value = {
        "data": {"table": "signals", "column": "value", "fn": "max", "group_by": ["channel"],
                 "groups": [{"channel": "T1", "value": 31.2, "rows": 3, "uncertainty": EXACT},
                            {"channel": "P1", "value": 101.0, "rows": 3, "uncertainty": NONE}]},
        "provenance": {"source": "gold.signals", "method": "max(value) by channel", "rows": 6},
    }
    lines = friendly_text.render("aggregate", value).splitlines()
    assert any(line.split()[:1] == ["T1"] and "± 0.5" in line for line in lines)
    assert any(line.split()[:1] == ["P1"] and "not reported" in line for line in lines)


def test_an_empty_answer_says_why():
    value = {"data": None, "provenance": {"note": "no rows matched; no value to report"}}
    assert friendly_text.render("aggregate", value) == "no rows matched; no value to report"
    assert friendly_text.render("series", value) == "no rows matched; no value to report"


def test_a_series_is_a_table_of_points_each_with_its_bound():
    value = {
        "data": {"table": "signals", "column": "value", "bucket": "1 hour", "fn": "mean",
                 "series": [{"t": "2026-03-11T00:00:00Z", "value": 20.5, "uncertainty": EXACT},
                            {"t": "2026-03-11T01:00:00Z", "value": 21.0, "uncertainty": NONE}]},
        "provenance": {"source": "gold.signals", "method": "avg(value) by 1 hour", "rows": 2},
        "unit": {"value": "degC"},
    }
    lines = friendly_text.render("series", value).splitlines()
    assert any("2026-03-11 00:00" in line and "± 0.5 degC" in line for line in lines)
    assert any("2026-03-11 01:00" in line and "not reported" in line for line in lines)


def test_a_grouped_series_names_each_group():
    value = {
        "data": {"bucket": "1 hour", "fn": "mean",
                 "series": [{"key": {"channel": "T1"}, "points": [{"t": "2026-03-11T00:00:00Z", "value": 20.5}]}]},
        "provenance": {"source": "gold.signals", "method": "avg(value) by 1 hour", "rows": 1},
    }
    text = friendly_text.render("series", value)
    assert "channel=T1" in text and "not reported" in text


def test_series_and_aggregate_are_text_verbs():
    assert {"series", "aggregate"} <= friendly_text.TEXT_VERBS
