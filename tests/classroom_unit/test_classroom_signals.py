# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for SCAN classroom signals (§2.9).

Emits instructor-facing signals from traces + metrics:
- student_stuck
- misconception_detected
- low_engagement
- high_engagement
- objective_gap

Pure functions over trace dicts; signals downstream are routed into
SCAN's pipeline. Standalone: deterministic rules. No external calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _trace(student_id, session_id, turn_index=0, topics=None,
           timestamp=None, content=""):
    return {
        "trace_id": f"{session_id}-{turn_index}",
        "student_id": student_id,
        "session_id": session_id,
        "turn_index": turn_index,
        "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
        "topics": topics or [],
        "content": content,
        "tokens": {"prompt": 100, "completion": 50},
        "rag_results_count": 1,
        "labels": [],
    }


class TestStudentStuck:
    def test_many_turns_same_topic_flags_stuck(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_student_stuck,
        )

        # 6 turns from s1 all on LO-1
        traces = [_trace("s1", "a", turn_index=i, topics=["LO-1"]) for i in range(6)]
        signals = detect_student_stuck(traces, threshold=5)

        assert len(signals) == 1
        assert signals[0]["signal_type"] == "student_stuck"
        assert signals[0]["student_id"] == "s1"
        assert signals[0]["topic"] == "LO-1"
        assert signals[0]["turn_count"] == 6

    def test_below_threshold_no_signal(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_student_stuck,
        )

        traces = [_trace("s1", "a", turn_index=i, topics=["LO-1"]) for i in range(3)]
        signals = detect_student_stuck(traces, threshold=5)
        assert signals == []


class TestMisconception:
    def test_matches_known_pattern(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_misconceptions,
        )

        traces = [
            _trace("s1", "a", content="I think fission releases more energy than fusion"),
        ]
        patterns = [
            {
                "id": "fission-vs-fusion-energy",
                "keywords": ["fission releases more energy than fusion"],
                "note": "Fusion per unit mass releases more.",
            },
        ]
        signals = detect_misconceptions(traces, patterns=patterns)
        assert len(signals) == 1
        assert signals[0]["signal_type"] == "misconception_detected"
        assert signals[0]["student_id"] == "s1"
        assert signals[0]["misconception_id"] == "fission-vs-fusion-energy"

    def test_no_match_no_signal(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_misconceptions,
        )

        traces = [_trace("s1", "a", content="fission splits heavy nuclei")]
        patterns = [{"id": "m1", "keywords": ["totally different phrase"]}]
        assert detect_misconceptions(traces, patterns=patterns) == []


class TestLowEngagement:
    def test_student_inactive_for_48_hours_flagged(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_low_engagement,
        )

        now = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        traces = [
            _trace("s1", "a", timestamp=now - timedelta(hours=60)),  # stale
            _trace("s2", "b", timestamp=now - timedelta(hours=1)),   # fresh
        ]
        signals = detect_low_engagement(
            traces, now=now, inactive_hours=48, active_student_ids=["s1", "s2"]
        )
        assert len(signals) == 1
        assert signals[0]["student_id"] == "s1"

    def test_student_with_no_traces_flagged(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_low_engagement,
        )

        now = datetime(2026, 4, 15, 12, 0, tzinfo=UTC)
        traces = [_trace("s1", "a", timestamp=now - timedelta(hours=1))]
        signals = detect_low_engagement(
            traces, now=now, inactive_hours=48,
            active_student_ids=["s1", "s2"],  # s2 has never interacted
        )
        s2_signals = [s for s in signals if s["student_id"] == "s2"]
        assert len(s2_signals) == 1


class TestHighEngagement:
    def test_significantly_above_cohort_mean_flagged(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_high_engagement,
        )

        traces = (
            # s1: 40 turns — outlier
            [_trace("s1", "a", turn_index=i) for i in range(40)]
            # s2..s4: 5 turns each — baseline
            + [_trace("s2", "b", turn_index=i) for i in range(5)]
            + [_trace("s3", "c", turn_index=i) for i in range(5)]
            + [_trace("s4", "d", turn_index=i) for i in range(5)]
        )
        signals = detect_high_engagement(traces, stdev_threshold=2.0)
        flagged = {s["student_id"] for s in signals}
        assert "s1" in flagged
        assert "s2" not in flagged


class TestObjectiveGap:
    def test_low_coverage_lo_flagged(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_objective_gaps,
        )

        # 10 students, only 1 touched LO-2
        traces = [
            _trace(f"s{i}", f"sess-{i}", topics=["LO-1"])
            for i in range(1, 11)
        ] + [_trace("s1", "extra", topics=["LO-2"])]

        learning_objectives = [
            {"id": "LO-1", "title": "Basics"},
            {"id": "LO-2", "title": "Advanced"},
            {"id": "LO-3", "title": "Never touched"},
        ]

        signals = detect_objective_gaps(
            traces,
            learning_objectives=learning_objectives,
            student_ids=[f"s{i}" for i in range(1, 11)],
            coverage_threshold=0.2,
        )
        flagged = {s["objective_id"] for s in signals}
        # LO-2: 1/10 = 0.1, below 0.2 → flagged
        # LO-3: 0/10 = 0.0, flagged
        # LO-1: 10/10 = 1.0, not flagged
        assert "LO-2" in flagged
        assert "LO-3" in flagged
        assert "LO-1" not in flagged


class TestSignalPayloadShape:
    def test_all_signals_share_common_fields(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_student_stuck,
        )

        traces = [_trace("s1", "a", turn_index=i, topics=["LO-1"]) for i in range(6)]
        signals = detect_student_stuck(traces, threshold=5)
        s = signals[0]
        # SCAN expects these:
        assert "signal_type" in s
        assert "student_id" in s
        assert "emitted_at" in s  # ISO timestamp
        assert "severity" in s  # low / medium / high


class TestAllSignalsBatch:
    def test_detect_all_runs_each_rule(self):
        from axiom.extensions.builtins.classroom.classroom_signals import (
            detect_all_signals,
        )

        traces = [_trace("s1", "a", turn_index=i, topics=["LO-1"]) for i in range(6)]
        signals = detect_all_signals(
            traces=traces,
            learning_objectives=[{"id": "LO-1", "title": "x"}],
            misconception_patterns=[],
            active_student_ids=["s1"],
            now=datetime.now(UTC),
        )
        signal_types = {s["signal_type"] for s in signals}
        assert "student_stuck" in signal_types
