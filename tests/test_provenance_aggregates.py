# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-113 / spec-output-provenance-gate: the gate admits a stated value that is a
descriptive-statistic aggregate (sum/mean/min/max/std) of a tool-returned series,
in ANY format (JSON list, JSON list-of-dicts, CSV/table, predominantly-numeric
inline list) — while still BLOCKING fabrication, `count`-laundering, prose-number
laundering, and Tier>=2 values the model computed without a tool.

Series used everywhere: [2.0, 4.0, 6.0] -> sum 12, mean 4, min 2, max 6.
Values are dollars so the default currency quantity pattern gates them.
"""
from __future__ import annotations

import json

from axiom.rag.provenance import ProvenanceGateConfig, evaluate_answer_provenance

CFG = ProvenanceGateConfig()  # default: currency + date patterns, domain-agnostic


def _decide(answer: str, texts: list[str]):
    return evaluate_answer_provenance(
        answer, grounded_texts=texts, tools_called=("analytics",), config=CFG
    )


# --- aggregate of a real series is admitted, format-agnostic ----------------

def test_sum_and_mean_over_json_list_of_dicts():
    texts = [json.dumps([{"peak": 2.0}, {"peak": 4.0}, {"peak": 6.0}])]
    d = _decide("The total is $12.00 and the average is $4.00.", texts)
    assert d.grounded, d.reason


def test_sum_and_mean_over_bare_json_list():
    texts = [json.dumps([2.0, 4.0, 6.0])]
    d = _decide("Sum $12.00, mean $4.00.", texts)
    assert d.grounded, d.reason


def test_sum_and_mean_over_csv_column():
    texts = ["peak\n2.0\n4.0\n6.0\n"]
    d = _decide("Total $12.00, average $4.00.", texts)
    assert d.grounded, d.reason


def test_sum_and_mean_over_csv_with_index_column():
    texts = ["date,peak\n2026-09-01,2.0\n2026-09-02,4.0\n2026-09-03,6.0\n"]
    d = _decide("Average $4.00 over the window; total $12.00.", texts)
    assert d.grounded, d.reason


def test_sum_and_mean_over_inline_numeric_list():
    texts = ["daily peaks: 2.0, 4.0, 6.0"]
    d = _decide("The sum is $12.00.", texts)
    assert d.grounded, d.reason


# --- the fabrication guarantee is preserved ---------------------------------

def test_fabricated_value_still_blocked():
    texts = [json.dumps([2.0, 4.0, 6.0])]
    d = _decide("The total is $5.00.", texts)  # 5 is no aggregate/element of [2,4,6]
    assert not d.grounded


def test_count_is_not_admitted():
    # series of 3 values; 3 == count. count must NOT be added to supported numbers.
    texts = [json.dumps([10.0, 20.0, 30.0])]
    d = _decide("We spent $3.00.", texts)  # 3 only matches count
    assert not d.grounded, "count must not launder a fabricated small integer"


def test_prose_incidental_numbers_do_not_launder():
    # a prose line's incidental numbers must NOT form a spurious series aggregate:
    # 14 and 1.02 would give sum 15.02 / mean 7.51 if prose were treated as a series.
    texts = ["The reactor ran 14 days at 1.02 MW peak this window."]
    d = _decide("The combined figure is $15.02.", texts)
    assert not d.grounded, "prose numbers must not be aggregated into support"


def test_tier2_value_without_tool_blocked():
    # a regression slope the model computed inline (no analytics result in corpus)
    # is not an element or descriptive aggregate of the series -> blocked.
    texts = [json.dumps([2.0, 4.0, 6.0])]
    d = _decide("The fitted slope is $1.97 per day.", texts)
    assert not d.grounded
