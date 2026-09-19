# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Promoting pushed event rows, and what must never be quietly filled in."""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.extensions.builtins.data_platform.runs.promote import promote_segments

T0 = "2026-09-14T09:00:00Z"
T1 = "2026-09-14T10:30:00Z"


def _row(**over):
    row = {
        "site": "ut-triga",
        "run_id": "2026-09-14-A",
        "segment_id": "seg-001",
        "label": "steady_state",
        "started_at": T0,
        "ended_at": T1,
        "label_source": "curated",
        "labelled_by": "nick",
    }
    row.update(over)
    return row


def test_a_curated_row_promotes_with_its_curator_intact():
    result = promote_segments([_row()])
    assert result.ok
    (run,) = result.runs.values()
    (segment,) = run.segments
    assert segment.label_source == "curated"
    assert segment.labelled_by == "nick"
    assert segment.confidence is None


def test_a_curated_row_with_no_curator_is_rejected_never_defaulted():
    """The one fact this path carries is that a person marked this window.

    Filling in 'unknown' would destroy exactly what is being preserved, and do
    it invisibly.
    """
    result = promote_segments([_row(labelled_by=None)])
    assert not result.ok
    assert result.runs == {}
    (bad,) = result.rejected
    assert "who labelled it" in bad.reasons[0]
    assert bad.where == "ut-triga/2026-09-14-A/seg-001"


def test_one_bad_row_does_not_sink_the_batch():
    result = promote_segments([_row(), _row(segment_id="seg-002", labelled_by=None)])
    assert len(result.segments) == 1
    assert len(result.rejected) == 1
    assert "REJECTED" in "\n".join(result.report())


def test_an_inferred_row_needs_a_confidence():
    bad = promote_segments([_row(segment_id="s", label_source="inferred",
                                 labelled_by=None, confidence=None)])
    assert not bad.ok
    good = promote_segments([_row(segment_id="s", label_source="inferred",
                                  labelled_by=None, confidence=0.8)])
    assert good.ok
    assert good.segments[0].confidence == 0.8


def test_an_unparseable_timestamp_is_rejected_not_guessed():
    result = promote_segments([_row(started_at="last tuesday")])
    assert not result.ok
    assert "not a timestamp" in result.rejected[0].reasons[0]


def test_the_run_is_created_implicitly_from_its_segments():
    """A publisher should not have to declare the run first.

    Requiring it would mean the first push after a run starts silently drops
    its own segments.
    """
    result = promote_segments([_row()])
    run = result.runs[("ut-triga", "2026-09-14-A")]
    assert run.started_at == datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    assert run.ended_at == datetime(2026, 9, 14, 10, 30, tzinfo=UTC)


def test_the_run_span_widens_to_hold_every_segment():
    result = promote_segments([
        _row(segment_id="a", started_at="2026-09-14T09:00:00Z",
             ended_at="2026-09-14T09:30:00Z"),
        _row(segment_id="b", started_at="2026-09-14T11:00:00Z",
             ended_at="2026-09-14T12:00:00Z"),
    ])
    run = result.runs[("ut-triga", "2026-09-14-A")]
    assert run.started_at == datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
    assert run.ended_at == datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def test_an_open_segment_leaves_the_run_open():
    """A run being labelled while it is still going is the normal case."""
    result = promote_segments([_row(ended_at=None)])
    assert result.runs[("ut-triga", "2026-09-14-A")].open


def test_a_duplicate_segment_id_in_one_batch_is_refused():
    """Ids are how a correction finds the row it replaces."""
    result = promote_segments([_row(), _row(label="transient")])
    assert len(result.segments) == 1
    assert any("appears twice" in r.reasons[0] for r in result.rejected)


def test_segments_for_two_runs_stay_apart():
    result = promote_segments([
        _row(),
        _row(run_id="2026-09-15-B", segment_id="seg-009", labelled_by="sam"),
    ])
    assert set(result.runs) == {("ut-triga", "2026-09-14-A"), ("ut-triga", "2026-09-15-B")}


def test_config_rides_along_when_the_publisher_sends_it():
    result = promote_segments([_row(config={"power_setpoint_w": 900.0})])
    run = result.runs[("ut-triga", "2026-09-14-A")]
    assert run.config == {"power_setpoint_w": 900.0}
    assert run.config_source == "declared"


def test_rows_in_counts_everything_including_what_was_refused():
    result = promote_segments([_row(), _row(segment_id="x", started_at="nope")])
    assert result.rows_in == 2
