# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Runs, and the two things that must not be conflated inside them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.data_platform.runs import (
    Run,
    RunSegment,
    similar_runs,
)

T0 = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)


def _run(**over) -> Run:
    base = dict(
        site="tamu-bubbleloop",
        run_id="run-001",
        started_at=T0,
        ended_at=T0 + timedelta(hours=2),
        config={"power_setpoint_w": 500.0, "flow_rate_lpm": 35.0},
    )
    base.update(over)
    return Run(**base)


# --- curated and inferred are different claims ------------------------------


def test_a_curated_segment_must_name_who_labelled_it():
    """An unattributable human judgement cannot be questioned or corrected."""
    seg = RunSegment("s1", "steady_state", T0, "curated")
    assert any("who labelled it" in e for e in seg.validate())

    named = RunSegment("s1", "steady_state", T0, "curated", labelled_by="nick")
    assert named.validate() == []


def test_a_curated_segment_carries_no_confidence():
    """A person did not assign themselves a probability."""
    seg = RunSegment("s1", "steady_state", T0, "curated", labelled_by="sam", confidence=0.9)
    assert any("did not assign themselves" in e for e in seg.validate())


def test_an_inferred_segment_must_state_its_confidence():
    """Otherwise it reads with the same authority as a person's judgement."""
    seg = RunSegment("s1", "transient", T0, "inferred")
    assert any("must state its confidence" in e for e in seg.validate())

    scored = RunSegment("s1", "transient", T0, "inferred", confidence=0.72)
    assert scored.validate() == []


def test_curated_and_inferred_segments_stay_separable():
    """The whole reason label_source is a column.

    A hand-labelled window is ground truth; a detected one is a hypothesis to
    be scored against it. A schema that merged them would make that scoring
    impossible, which is the mistake worth designing against.
    """
    run = _run(segments=(
        RunSegment("s1", "steady_state", T0, "curated", labelled_by="nick"),
        RunSegment("s2", "transient", T0 + timedelta(minutes=30), "inferred", confidence=0.6),
    ))
    assert run.validate() == []
    assert [s.segment_id for s in run.curated_segments] == ["s1"]
    assert [s.segment_id for s in run.inferred_segments] == ["s2"]


def test_a_segment_cannot_escape_its_run():
    run = _run(segments=(
        RunSegment("s1", "steady_state", T0 - timedelta(minutes=1), "curated", labelled_by="nick"),
    ))
    assert any("starts before its run" in e for e in run.validate())


def test_an_unknown_label_is_refused():
    seg = RunSegment("s1", "wobbling", T0, "curated", labelled_by="nick")
    assert any("not one of" in e for e in seg.validate())


# --- a simulated run is not a short real one --------------------------------


def test_a_simulated_run_must_name_its_generator():
    assert any("model_ref" in e for e in _run(source_class="simulated").validate())
    assert _run(source_class="simulated", model_ref="flowloop-sim@0.3.1").validate() == []


def test_simulated_runs_are_left_out_of_a_comparison_unless_asked_for():
    """A comparison that quietly includes synthetic runs is worse than an empty one."""
    ref = _run()
    sim = _run(site="vcu-flowloop", run_id="sim-1", source_class="simulated",
               model_ref="flowloop-sim@0.3.1")
    assert similar_runs(ref, [sim]) == []
    assert len(similar_runs(ref, [sim], include_simulated=True)) == 1


# --- similarity -------------------------------------------------------------


def test_a_run_with_no_configuration_matches_nothing():
    """Two runs are not alike because nothing is known about either.

    Returning everything here would be a confident wrong answer in exactly the
    place a user is least able to check it.
    """
    assert similar_runs(_run(config={}), [_run(run_id="other")]) == []


def test_runs_at_a_similar_setpoint_match_within_tolerance():
    ref = _run()
    close = _run(site="vcu-flowloop", run_id="v-1",
                 config={"power_setpoint_w": 505.0, "flow_rate_lpm": 35.2})
    far = _run(site="vcu-flowloop", run_id="v-2",
               config={"power_setpoint_w": 2000.0, "flow_rate_lpm": 35.0})

    matches = similar_runs(ref, [close, far])
    assert matches[0].run.run_id == "v-1"
    assert set(matches[0].matched) == {"power_setpoint_w", "flow_rate_lpm"}
    assert matches[0].score == 1.0
    # `far` still appears — it matched on flow — but ranks below and says why.
    assert matches[1].run.run_id == "v-2"
    assert matches[1].matched == ("flow_rate_lpm",)
    assert matches[1].unshared == ("power_setpoint_w",)
    assert matches[1].score == 0.5


def test_the_caller_chooses_which_dimensions_matter():
    """Same power, different flow: alike for one question, not for another."""
    ref = _run()
    other = _run(site="vcu-flowloop", run_id="v-3",
                 config={"power_setpoint_w": 500.0, "flow_rate_lpm": 12.0})

    on_power = similar_runs(ref, [other], keys=["power_setpoint_w"])
    assert on_power[0].score == 1.0

    on_both = similar_runs(ref, [other])
    assert on_both[0].score == 0.5


def test_a_key_the_candidate_never_declared_is_unshared_not_matched():
    """A match on two of six keys is a different statement from two of two."""
    ref = _run()
    partial = _run(site="acu-flowloop", run_id="a-1", config={"power_setpoint_w": 500.0})
    (match,) = similar_runs(ref, [partial])
    assert match.matched == ("power_setpoint_w",)
    assert match.unshared == ("flow_rate_lpm",)


def test_per_key_tolerance_beats_one_global_number():
    ref = _run()
    candidate = _run(site="vcu-flowloop", run_id="v-4",
                     config={"power_setpoint_w": 550.0, "flow_rate_lpm": 35.0})
    assert similar_runs(ref, [candidate])[0].matched == ("flow_rate_lpm",)
    loose = similar_runs(ref, [candidate], tolerances={"power_setpoint_w": 0.2})
    assert set(loose[0].matched) == {"power_setpoint_w", "flow_rate_lpm"}


def test_a_run_never_matches_itself():
    ref = _run()
    assert similar_runs(ref, [ref]) == []


def test_non_numeric_config_compares_by_equality():
    ref = _run(config={"working_fluid": "FLiNaK"})
    same = _run(site="vcu-flowloop", run_id="v-5", config={"working_fluid": "FLiNaK"})
    diff = _run(site="vcu-flowloop", run_id="v-6", config={"working_fluid": "water"})
    assert [m.run.run_id for m in similar_runs(ref, [same, diff])] == ["v-5"]


def test_an_open_run_is_representable():
    """NULL end, not a sentinel far-future date that sorts as though it ended."""
    run = _run(ended_at=None)
    assert run.open and run.validate() == []
