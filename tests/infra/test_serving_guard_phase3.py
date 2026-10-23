# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Phase 3: the cheap answer is the enforced answer (ADR-157).

Measured 2026-09-24: the same aggregate costs 0.33 ms with the full index
prefix and 977 ms with one key column missing — about 3,000 times more — and
nothing required the cheap shape. And a wide window re-read raw rows that a
declared rollup already summarises. Both are policy: a deployment declares
its tables' required filter columns and its rollups; the guard refuses the
expensive shape by naming the cheap one, and routes coarse asks to the rollup.
"""

from __future__ import annotations

from axiom.infra import serving_guard as sg


def _policy(tmp_path, text):
    f = tmp_path / "serving_policy.toml"
    f.write_text(text, encoding="utf-8")
    return sg.load_policy(f)


# ----------------------------------------------------------------- loading


def test_table_and_rollup_policy_load_from_the_file(tmp_path):
    p = _policy(
        tmp_path,
        '[tables."gold.signals"]\nrequired_filters = ["site"]\n\n'
        '[rollups."gold.signals"]\ntable = "gold.signals_1h"\n'
        'native_bucket = "1 hour"\n',
    )
    assert p.table_policy("gold.signals").required_filters == ("site",)
    r = p.rollup("gold.signals")
    assert r is not None
    assert r.table == "gold.signals_1h" and r.native_bucket_seconds == 3600.0


def test_an_undeclared_table_has_no_requirements(tmp_path):
    p = _policy(tmp_path, "")
    assert p.table_policy("gold.other").required_filters == ()
    assert p.rollup("gold.other") is None


# ---------------------------------------------------------- required filters


def test_a_missing_required_filter_is_named():
    missing = sg.missing_required_filters(
        required=("site",), filter_text=None, group_by=(), extra_columns=()
    )
    assert missing == ("site",)


def test_a_filter_naming_the_column_satisfies_it():
    assert (
        sg.missing_required_filters(
            required=("site",),
            filter_text="site = 'alpha' and value > 0",
            group_by=(),
            extra_columns=(),
        )
        == ()
    )


def test_grouping_by_the_column_satisfies_it():
    # A grouped answer touches every group once — the index prefix is used
    # per group, which is the cheap plan the requirement exists to force.
    assert (
        sg.missing_required_filters(
            required=("site",), filter_text=None, group_by=("site",), extra_columns=()
        )
        == ()
    )


def test_a_column_supplied_by_the_verb_itself_satisfies_it():
    # compare() always filters by role and may filter by sites; the verb
    # passes those as extra_columns.
    assert (
        sg.missing_required_filters(
            required=("site",), filter_text=None, group_by=(), extra_columns=("site",)
        )
        == ()
    )


def test_a_substring_does_not_satisfy_the_requirement():
    # "website" contains "site"; a word-boundary match must not be fooled.
    assert sg.missing_required_filters(
        required=("site",),
        filter_text="website = 'x'",
        group_by=(),
        extra_columns=(),
    ) == ("site",)


# ------------------------------------------------------------------ rollups


def test_a_coarse_bucket_routes_to_the_declared_rollup(tmp_path):
    p = _policy(
        tmp_path,
        '[rollups."gold.signals"]\ntable = "gold.signals_1h"\n'
        'native_bucket = "1 hour"\n',
    )
    assert (
        sg.choose_rollup(policy=p, table="gold.signals", bucket_seconds=86400.0)
        == "gold.signals_1h"
    )
    assert (
        sg.choose_rollup(policy=p, table="gold.signals", bucket_seconds=3600.0)
        == "gold.signals_1h"
    )


def test_a_fine_bucket_stays_on_the_raw_table(tmp_path):
    p = _policy(
        tmp_path,
        '[rollups."gold.signals"]\ntable = "gold.signals_1h"\n'
        'native_bucket = "1 hour"\n',
    )
    assert sg.choose_rollup(policy=p, table="gold.signals", bucket_seconds=60.0) is None


def test_negative_control_no_declaration_changes_nothing(tmp_path):
    p = _policy(tmp_path, "")
    assert sg.choose_rollup(policy=p, table="gold.signals", bucket_seconds=86400.0) is None
