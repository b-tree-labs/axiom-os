# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Collapsing a state channel, and what must not vanish when it collapses."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from axiom.extensions.builtins.data_platform.runs.states import collapse_states

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
MAP = {
    "STEADY STATE": "steady_state",
    "SCRAM": "fault",
    "PULSE": "pulse",
    "BOOT UP": "startup",
    "SQUARE - RAMP UP": "transient",
}


def _s(i):
    return T0 + timedelta(seconds=i)


def test_repeated_states_collapse_into_one_segment():
    """Half a billion rows of the same string answers nothing; a segment does."""
    result = collapse_states(
        [(_s(i), "STEADY STATE") for i in range(600)], MAP)
    (seg,) = result.segments
    assert seg.label == "steady_state"
    assert seg.started_at == _s(0)
    assert seg.ended_at == _s(599)


def test_a_change_closes_the_previous_segment_at_the_new_sample():
    """The span is the interval the state covered, not its last identical sample."""
    result = collapse_states([
        (_s(0), "STEADY STATE"), (_s(10), "STEADY STATE"),
        (_s(20), "SCRAM"), (_s(30), "SCRAM"),
    ], MAP)
    steady, scram = result.segments
    assert steady.label == "steady_state"
    assert steady.ended_at == _s(20)      # ends where SCRAM begins
    assert scram.label == "fault"
    assert scram.started_at == _s(20)


def test_the_label_source_is_recorded_not_curated_or_inferred():
    """The plant said so. No person typed it and no detector guessed it."""
    (seg,) = collapse_states([(_s(0), "PULSE")], MAP).segments
    assert seg.label_source == "recorded"
    assert seg.labelled_by is None
    assert seg.confidence is None
    assert seg.validate() == []


def test_the_literal_string_None_counts_as_absent():
    """538 million rows saying 'None' is not 538 million labelled rows."""
    result = collapse_states([
        (_s(0), "None"), (_s(1), "None"), (_s(2), "STEADY STATE"), (_s(3), "None"),
    ], MAP)
    assert result.absent_samples == 3
    (seg,) = result.segments
    assert seg.label == "steady_state"


def test_real_none_is_absent_too():
    result = collapse_states([(_s(0), None), (_s(1), "SCRAM")], MAP)
    assert result.absent_samples == 1
    assert [s.label for s in result.segments] == ["fault"]


def test_an_unmapped_state_is_kept_with_its_raw_value_not_dropped():
    """Dropping it makes an unmapped state look like a period of silence."""
    result = collapse_states([(_s(0), "PRETEST"), (_s(10), "PRETEST")], MAP)
    (seg,) = result.segments
    assert seg.label == "unknown"
    assert "PRETEST" in (seg.notes or "")
    assert result.unmapped == {"PRETEST": 2}
    assert any("no label mapping" in line for line in result.report)


def test_two_different_unmapped_states_do_not_merge():
    """Both become 'unknown', but they are not the same window."""
    result = collapse_states([
        (_s(0), "PRETEST"), (_s(10), "AUTO"),
    ], MAP)
    assert [s.label for s in result.segments] == ["unknown", "unknown"]
    assert "PRETEST" in (result.segments[0].notes or "")
    assert "AUTO" in (result.segments[1].notes or "")


def test_a_gap_closes_a_segment_rather_than_spanning_an_outage():
    """One segment across a week of silence asserts the plant held that state."""
    result = collapse_states([
        (_s(0), "STEADY STATE"), (_s(10), "STEADY STATE"),
        (_s(100_000), "STEADY STATE"),
    ], MAP, max_gap=timedelta(minutes=5))
    assert len(result.segments) == 2
    assert result.segments[0].ended_at == _s(10)
    assert result.segments[1].started_at == _s(100_000)


def test_without_max_gap_the_caller_gets_one_span_and_owns_that_choice():
    result = collapse_states([
        (_s(0), "STEADY STATE"), (_s(100_000), "STEADY STATE"),
    ], MAP)
    assert len(result.segments) == 1


def test_a_mapping_to_an_invalid_label_is_refused_loudly():
    """A typo in a domain package's map must not silently produce garbage."""
    with pytest.raises(ValueError, match="not one of"):
        collapse_states([(_s(0), "STEADY STATE")], {"STEADY STATE": "steady-state"})


def test_samples_out_of_order_are_sorted_first():
    result = collapse_states([
        (_s(20), "SCRAM"), (_s(0), "STEADY STATE"), (_s(10), "STEADY STATE"),
    ], MAP)
    assert [s.label for s in result.segments] == ["steady_state", "fault"]


def test_every_segment_validates():
    result = collapse_states([
        (_s(0), "BOOT UP"), (_s(5), "SQUARE - RAMP UP"),
        (_s(9), "STEADY STATE"), (_s(60), "SCRAM"), (_s(70), "WHAT IS THIS"),
    ], MAP)
    assert [s.label for s in result.segments] == [
        "startup", "transient", "steady_state", "fault", "unknown"]
    for seg in result.segments:
        assert seg.validate() == [], f"{seg.segment_id}: {seg.validate()}"
    assert len({s.segment_id for s in result.segments}) == len(result.segments)
