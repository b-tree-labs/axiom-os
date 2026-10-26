# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Completion candidates come from the rows, once, for every surface.

A search box with no candidates is a guessing game: a partner has to
already know their channel is spelled `NCDT1:COOLER:VELO1` before they can
narrow to it. Shell completion, a web typeahead and an agent choosing a
filter are the same question asked three ways, and three implementations of
it is how they disagree about what exists.

The count is the part that makes it worth having. A list of channel names
says what exists; `channel=fuel_temp_1 (900)` says what you would get,
which is what the reader was asking when they started typing.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.scidisplay.table_spec import (
    SUGGESTION_LIMIT,
    SpecFormatError,
    completions,
)

COLUMNS = (("ts", "time", True, "left"),
           ("channel", "channel", True, "left"),
           ("value", "value", True, "right"),
           ("unit", "unit", True, "left"),
           ("source_class", "class", True, "left"))

#: The real shape: many instants, a handful of channels, one class.
ROWS = [
    {"ts": f"2026-09-18T09:{m:02d}:{s:02d}Z", "channel": channel,
     "value": float(s), "unit": unit, "source_class": "measured"}
    for m in range(3) for s in range(60)
    for channel, unit in (("power", "W"), ("fuel_temp_1", "degC"),
                          ("fuel_temp_2", "degC"), ("rod_shim_1", "step"))
]


def _offered(**kw):
    return [s["insert"] for s in completions(ROWS, columns=COLUMNS, **kw)["suggestions"]]


class TestWhichColumnsAreWorthFiltering:
    def test_the_columns_that_group_come_first(self):
        """`channel` with four values and `unit` with three beat `value`
        with sixty, which narrows to a handful of rows and answers nothing
        anybody asked.

        Which of `channel` and `unit` leads is not asserted: three against
        four is noise, and picking `channel` would take domain knowledge
        this layer deliberately does not have."""
        offered = _offered()
        assert set(offered[:2]) == {"channel=", "unit="}
        assert offered.index("value=") > 1

    def test_a_column_with_one_value_is_not_offered(self):
        """Every row is `measured`, so filtering on it returns the table
        back. That is a fact about the table, not a filter."""
        assert "source_class=" not in _offered()

    def test_a_column_with_a_value_per_row_is_not_offered(self):
        rows = [{"id": str(i), "channel": "a"} for i in range(20)]
        assert "id=" not in [
            s["insert"] for s in completions(rows, columns=(("id", "id", True, "left"),
                                                            ("channel", "c", True, "left")))
            ["suggestions"]
        ]

    def test_a_timestamp_sinks_without_being_hidden(self):
        """Filtering to one instant is a real thing to want, just never the
        first thing."""
        offered = _offered()
        assert "ts=" in offered
        assert offered.index("ts=") > offered.index("channel=")


class TestWhichValuesAColumnHolds:
    def test_they_come_back_as_whole_tokens(self):
        """A shell completer has to hand back what goes on the line."""
        assert all(s.startswith("channel=") for s in _offered(column="channel"))

    def test_the_commoner_value_comes_first(self):
        rows = [*ROWS, {"ts": "x", "channel": "power", "value": 1.0,
                        "unit": "W", "source_class": "measured"}]
        first = completions(rows, columns=COLUMNS, column="channel")["suggestions"][0]
        assert first["insert"] == "channel=power"

    def test_each_says_how_many_rows_it_would_leave(self):
        counts = {s["insert"]: s["count"]
                  for s in completions(ROWS, columns=COLUMNS, column="channel")["suggestions"]}
        assert counts["channel=power"] == 180

    def test_a_column_that_is_not_there_is_refused(self):
        with pytest.raises(SpecFormatError, match="no column"):
            completions(ROWS, columns=COLUMNS, column="nope")


class TestTypingNarrowsIt:
    def test_a_prefix_matches(self):
        assert _offered(column="channel", prefix="fuel") == [
            "channel=fuel_temp_1", "channel=fuel_temp_2"
        ]

    def test_a_substring_matches_too(self):
        """Somebody typing `temp` means the temperatures, and neither one
        starts with it."""
        assert _offered(column="channel", prefix="temp") == [
            "channel=fuel_temp_1", "channel=fuel_temp_2"
        ]

    def test_a_prefix_match_outranks_a_substring_one(self):
        rows = [{"channel": "temp_main"}] * 3 + [{"channel": "fuel_temp"}] * 99
        offered = [
            s["insert"] for s in
            completions(rows, columns=(("channel", "c", True, "left"),),
                        column="channel", prefix="temp")["suggestions"]
        ]
        assert offered == ["channel=temp_main", "channel=fuel_temp"], (
            "the commoner value won over the one they were spelling"
        )

    def test_case_does_not_matter(self):
        assert _offered(column="channel", prefix="FUEL") == _offered(
            column="channel", prefix="fuel"
        )

    def test_nothing_matching_is_an_empty_list_not_an_error(self):
        assert _offered(column="channel", prefix="zzz") == []


class TestALongListSaysItWasCut:
    def test_it_is_capped(self):
        rows = [{"channel": f"ch{i:04d}"} for i in range(SUGGESTION_LIMIT * 3)]
        found = completions(rows, columns=(("channel", "c", True, "left"),), column="channel")
        assert len(found["suggestions"]) == SUGGESTION_LIMIT

    def test_it_says_so(self):
        """A short list presented as the whole answer is how somebody
        concludes their channel does not exist."""
        rows = [{"channel": f"ch{i:04d}"} for i in range(SUGGESTION_LIMIT * 3)]
        found = completions(rows, columns=(("channel", "c", True, "left"),), column="channel")
        assert found["truncated"] is True
        assert found["matched"] == SUGGESTION_LIMIT * 3

    def test_a_short_list_does_not_claim_to_be_cut(self):
        assert completions(ROWS, columns=COLUMNS, column="channel")["truncated"] is False
