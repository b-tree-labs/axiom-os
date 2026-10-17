# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The time-series contract, without a database: what a declaration means.

The chunk interval is derived from the declared rate, durations are read the
way people write them, and a declaration that cannot be applied safely is
refused when it is parsed rather than when it reaches a live table.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from axiom.extensions.builtins.data_platform.timeseries.config import (
    contracts_from_entries,
    specs_from_entries,
)
from axiom.extensions.builtins.data_platform.timeseries.contract import (
    DEFAULT_CHUNK_INTERVAL,
    DEFAULT_COMPRESS_AFTER,
    TARGET_ROWS_PER_CHUNK,
    TimeseriesContract,
    derive_chunk_interval,
    parse_duration,
)

H, D = timedelta(hours=1), timedelta(days=1)


# -- chunk interval derivation ------------------------------------------------


def test_no_declared_rate_means_the_default_chunk():
    assert derive_chunk_interval(None) == DEFAULT_CHUNK_INTERVAL == D


@pytest.mark.parametrize(
    "rows_per_day,expected",
    [
        (1_000, 30 * D),  # tiny rate: the ladder's ceiling, never a year-wide chunk
        (100_000, 30 * D),
        (2_000_000, D),  # 5 days of rows would fit; the largest rung at or under is 1d
        (TARGET_ROWS_PER_CHUNK, D),
        (20_000_000, 12 * H),
        (25_000_000, 6 * H),  # the rate measured on the node that motivated this
        (1_000_000_000, H),  # the floor: never narrower than an hour
    ],
)
def test_the_chunk_interval_holds_about_the_target_rows(rows_per_day, expected):
    assert derive_chunk_interval(rows_per_day) == expected


def test_a_derived_chunk_never_exceeds_the_target_except_at_the_floor():
    for rate in (1_000, 50_000, 1_000_000, 3_000_000, 12_000_000, 40_000_000, 90_000_000):
        chunk = derive_chunk_interval(rate)
        rows = rate * chunk.total_seconds() / 86400
        assert rows <= TARGET_ROWS_PER_CHUNK or chunk == H, (rate, chunk, rows)


# -- durations ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("7d", 7 * D),
        ("12h", 12 * H),
        ("30m", timedelta(minutes=30)),
        ("90 days", 90 * D),
        ("1 hour", H),
        ("2w", 14 * D),
        (7, 7 * D),  # a bare number is days, the unit everything else here uses
        ("400", 400 * D),
    ],
)
def test_durations_are_read_the_way_people_write_them(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("bad", ["", "soon", "7 fortnights", "-1d", "0d", None])
def test_a_duration_that_means_nothing_is_refused(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


# -- the declaration ----------------------------------------------------------


def _entry(**over):
    base = {
        "schema": "silver", "table": "readings", "engine": "hypertable",
        "identity": ["site", "feed", "channel"], "rows_per_day": 25_000_000,
    }
    base.update(over)
    return base


def test_a_declaration_carries_its_defaults():
    (c,) = contracts_from_entries([_entry()])
    assert c.qualified == "silver.readings"
    assert c.time_column == "ts"
    assert c.identity == ("site", "feed", "channel")
    assert c.chunk == 6 * H  # derived from 25M rows/day
    assert c.compress_after == DEFAULT_COMPRESS_AFTER == 7 * D
    assert c.retain is None, "deleting history is never a default"
    assert c.forward_guard is False
    assert c.segmentby == "site,feed,channel"
    assert c.orderby == "ts DESC"


def test_an_explicit_chunk_interval_wins_over_the_derived_one():
    (c,) = contracts_from_entries([_entry(chunk_interval="1d")])
    assert c.chunk == D


def test_retention_and_the_guard_are_read():
    (c,) = contracts_from_entries([_entry(retain="400d", forward_guard=True, compress_after="2d")])
    assert c.retain == 400 * D
    assert c.forward_guard is True
    assert c.compress_after == 2 * D


def test_a_declaration_without_an_identity_is_allowed_but_segments_by_nothing():
    (c,) = contracts_from_entries([_entry(identity=[])])
    assert c.identity == () and c.segmentby == ""


@pytest.mark.parametrize(
    "over",
    [
        {"schema": "silver; drop"},
        {"table": "readings\""},
        {"time_column": "ts desc"},
        {"identity": ["site", "a b"]},
        {"retain": "yesterday"},
        {"rows_per_day": -5},
    ],
)
def test_a_malformed_declaration_is_skipped_not_fatal(over):
    """One bad stanza must not stop the other tables being managed."""
    good = _entry(table="other")
    out = contracts_from_entries([_entry(**over), good])
    assert [c.table for c in out] == ["other"]


def test_a_native_partition_entry_is_not_a_contract_and_a_contract_is_not_a_spec():
    """Negative control both ways: the two engines share one config key and
    must not read each other's entries."""
    native = {"schema": "public", "table": "measurements", "granularity": "day"}
    hyper = _entry()
    assert [s.table for s in specs_from_entries([native, hyper])] == ["measurements"]
    assert [c.table for c in contracts_from_entries([native, hyper])] == ["readings"]


def test_nothing_declared_means_nothing_managed():
    assert contracts_from_entries(None) == []
    assert contracts_from_entries([]) == []


def test_a_contract_built_in_code_validates_the_same_way():
    with pytest.raises(ValueError):
        TimeseriesContract(schema="silver", table="x", identity=("site",), time_column="ts;")
    with pytest.raises(ValueError):
        TimeseriesContract(schema="silver", table="x", compress_after=timedelta(0))
