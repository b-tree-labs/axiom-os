# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for tabular-upload handling (whole-file load + deterministic outliers)."""

from __future__ import annotations

from axiom.rag.tabular import (
    iqr_outliers,
    parse_table,
    render_table_block,
    sniff_tabular,
    zscore_outliers,
)

# A tiny reactivity-vs-date table shaped like the file that tripped the chat:
# a clear high outlier (143.42) that must be found regardless of row order.
CSV = (
    "date,reactivity_final,mean_rod_height,cumulative_burnup_mwh\n"
    "2025-02-10,130.21,500.0,60.72\n"
    "2025-03-10,143.42,550.75,93.64\n"
    "2025-05-06,128.13,492.0,137.33\n"
    "2025-06-09,130.01,499.25,178.01\n"
    "2025-11-24,133.33,505.0,562.20\n"
    "2026-03-23,133.07,511.0,767.33\n"
)


def test_sniff_by_extension_and_content():
    assert sniff_tabular("temporary_final_reactivity.csv")
    assert sniff_tabular("data.tsv")
    assert sniff_tabular(None, sample="a,b,c\n1,2,3\n4,5,6")
    assert not sniff_tabular("notes.txt", sample="just some prose without delimiters")
    assert not sniff_tabular(None, sample=None)


def test_parse_captures_every_row_and_column():
    t = parse_table(CSV)
    assert t.columns[0] == "date"
    assert t.n_rows == 6  # all rows, not a chunk
    assert t.column("reactivity_final") == [
        "130.21", "143.42", "128.13", "130.01", "133.33", "133.07",
    ]


def test_iqr_finds_the_high_outlier_over_full_column():
    t = parse_table(CSV)
    idxs = iqr_outliers(t.column("reactivity_final"))
    # 143.42 is row index 1; it is the outlier and must be found deterministically
    assert 1 in idxs
    vals = t.column("reactivity_final")
    assert all(vals[i] == "143.42" for i in idxs)


def test_outlier_result_is_stable_across_calls():
    t = parse_table(CSV)
    col = t.column("reactivity_final")
    assert iqr_outliers(col) == iqr_outliers(col)  # deterministic, unlike LLM eyeballing


def test_zscore_outliers_ignores_zero_variance():
    assert zscore_outliers(["5", "5", "5", "5"]) == []


def test_iqr_ignores_non_numeric_cells():
    idxs = iqr_outliers(["10", "11", "n/a", "12", "500", "10"])
    assert 4 in idxs  # 500 is the outlier; "n/a" is skipped, not crashed on


def test_render_shows_all_rows_when_within_budget():
    t = parse_table(CSV)
    block = render_table_block(t, max_tokens=4000)
    assert "143.42" in block and "130.21" in block and "133.07" in block  # every row present
    assert "6 rows total" in block
    assert "rows omitted" not in block


def test_render_declares_row_omission_when_over_budget():
    t = parse_table(CSV)
    block = render_table_block(t, max_tokens=8)  # tiny budget forces truncation
    assert "rows omitted to fit" in block  # honest, not a silent slice


def test_render_focus_columns_declares_omitted_columns():
    t = parse_table(CSV)
    block = render_table_block(t, max_tokens=4000, focus_columns=["reactivity_final"])
    assert "columns omitted to fit" in block
    assert "mean_rod_height" in block  # named as omitted


def test_empty_input_is_safe():
    assert parse_table("").n_rows == 0
    assert render_table_block(parse_table("")) == "(empty table)"
    assert iqr_outliers([]) == []
