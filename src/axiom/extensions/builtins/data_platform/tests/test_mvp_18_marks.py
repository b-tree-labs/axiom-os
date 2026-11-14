# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""MVP-18-A1: a held span is a mark, apart from ADR-132's closed QUALITY contract.

The marks vocabulary says what a surface tells a reader about a span it cannot
read as drawn. It is closed, and it never leaks into QUALITY.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import company, marks


def test_mvp_18_a1_marks_are_a_closed_vocabulary_apart_from_quality() -> None:
    assert marks.MARKS == ("held", "gap", "stale", "suspect", "refused")
    assert company.QUALITY == ("good", "suspect", "bad", "saturated", "stale")
    assert "held" not in company.QUALITY and "gap" not in company.QUALITY


def test_mvp_18_a1_a_point_maps_to_its_mark() -> None:
    m = marks.mark_for
    assert m({"value": 1.0, "quality": "good"}) is None
    assert m({"value": 1.0, "quality": "saturated"}) is None
    assert m({"value": 1.0}) is None
    assert m({"value": 1.0, "quality": "stale"}) == "stale"
    assert m({"value": 1.0, "quality": "suspect"}) == "suspect"
    assert m({"value": 1.0, "quality": "bad"}) == "suspect"
    # A verdict that nulled the value is a hold, not a gap (ADR-132 D3).
    assert m({"value": None, "quality": "bad"}) == "held"
    assert m({"value": 2.0, "flags": ["withheld"]}) == "held"
    assert m({"value": None}) == "gap"
    assert m({"value": None, "refused": True}) == "refused"


def test_mvp_18_a1_points_become_spans_with_a_reason() -> None:
    points = [
        {"t": "2026-10-30T14:00:00Z", "value": 1.0, "quality": "good"},
        {
            "t": "2026-10-30T14:02:00Z",
            "value": None,
            "quality": "bad",
            "quality_reason": "company.a_vs_b",
        },
        {"t": "2026-10-30T14:10:00Z", "value": None, "quality": "bad"},
        {"t": "2026-10-30T14:20:00Z", "value": None, "quality": "bad"},
        {"t": "2026-10-30T14:22:00Z", "value": 3.0, "quality": "good"},
        {"t": "2026-10-30T14:24:00Z", "value": None},
    ]
    spans = marks.mark_spans(points)
    assert spans == [
        {
            "mark": "held",
            "start": "2026-10-30T14:02:00Z",
            "end": "2026-10-30T14:20:00Z",
            "reason": "company.a_vs_b",
        },
        {
            "mark": "gap",
            "start": "2026-10-30T14:24:00Z",
            "end": "2026-10-30T14:24:00Z",
            "reason": marks.DEFAULT_REASON["gap"],
        },
    ]
    assert marks.describe_span(spans[0], label="Power") == (
        "Power held from 14:02 to 14:20 (company.a_vs_b)"
    )


def test_mvp_18_a1_bucketed_series_marks_held_buckets_and_long_gaps() -> None:
    pts = [
        {"channel": "a", "ts": "2026-10-30T00:00:00Z", "avg": 1.0, "n": 3, "held": 0},
        {"channel": "a", "ts": "2026-10-30T00:01:00Z", "avg": None, "n": 0, "held": 2},
        {"channel": "a", "ts": "2026-10-30T00:02:00Z", "avg": None, "n": 0, "held": 1},
        {"channel": "a", "ts": "2026-10-30T00:03:00Z", "avg": 2.0, "n": 1, "held": 0},
        # one missing bucket: not marked; five missing: a gap
        {"channel": "a", "ts": "2026-10-30T00:05:00Z", "avg": 2.0, "n": 1, "held": 0},
        {"channel": "a", "ts": "2026-10-30T00:11:00Z", "avg": 2.0, "n": 1, "held": 0},
    ]
    got = marks.bucket_marks(pts, step_s=60)
    assert got == [
        {
            "channel": "a",
            "mark": "held",
            "start": "2026-10-30T00:01:00Z",
            "end": "2026-10-30T00:03:00Z",
            "reason": marks.DEFAULT_REASON["held"],
        },
        {
            "channel": "a",
            "mark": "gap",
            "start": "2026-10-30T00:06:00Z",
            "end": "2026-10-30T00:11:00Z",
            "reason": marks.DEFAULT_REASON["gap"],
        },
    ]
