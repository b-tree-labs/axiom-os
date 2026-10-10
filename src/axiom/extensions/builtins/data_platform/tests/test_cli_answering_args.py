# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The answering verbs' flags reach the skill as the window it expects.

When the verbs were renamed (gold-aggregate -> aggregate, gold-series ->
series) the window assembly kept matching the old names, so --start / --end
were passed through as stray params and the aggregate silently ran over the
whole table."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.data_platform.cli import _args_to_params, _parser


@pytest.mark.parametrize(
    "argv",
    [
        ["aggregate", "--table", "signals", "--column", "value", "--fn", "mean"],
        ["series", "--table", "signals", "--column", "value", "--bucket", "1 hour"],
    ],
)
def test_start_and_end_become_a_window(argv):
    args = _parser().parse_args(
        [*argv, "--time-column", "ts", "--start", "2026-09-01", "--end", "2026-09-02"]
    )
    params = _args_to_params(args)
    assert params["window"] == {"column": "ts", "start": "2026-09-01", "end": "2026-09-02"}
    assert "start" not in params and "end" not in params


@pytest.mark.parametrize(
    "argv",
    [
        ["aggregate", "--table", "signals", "--column", "value", "--fn", "mean"],
        ["series", "--table", "signals", "--column", "value", "--bucket", "1 hour"],
    ],
)
def test_a_window_without_a_time_column_is_over_ts(argv):
    """Every other answering verb defaults the window to ``ts``; these two sent
    ``column: None`` and the skill answered "no column None for the window",
    which names neither the flag nor the default (found 2026-10-08 proving the
    archive role's data access)."""
    args = _parser().parse_args([*argv, "--start", "2026-09-01", "--end", "2026-09-02"])
    params = _args_to_params(args)
    assert params["window"] == {"column": "ts", "start": "2026-09-01", "end": "2026-09-02"}


def test_uncertainty_coverage_has_a_cli_verb():
    args = _parser().parse_args(["uncertainty-coverage", "--site", "site-a", "--include-sources"])
    params = _args_to_params(args)
    assert params == {"site": "site-a", "include_sources": True}
