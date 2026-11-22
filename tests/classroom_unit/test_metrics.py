# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for classroom metrics aggregation (§5.4).

Per spec-classroom.md §5.4 / PRD §5.4: per-student + cohort
aggregates over the trace stream. Sessions, turns, tokens,
RAG hit rate, topic distribution, label distribution.

Standalone-first: aggregation runs on local traces with no
external deps. Federation-aware: metrics summarize into signed
claims for cross-node cohort rollup.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _trace(
    student_id: str,
    session_id: str,
    turn_index: int = 0,
    session_type: str = "chat",
    prompt_tokens: int = 100,
    completion_tokens: int = 50,
    rag_results_count: int = 3,
    labels=None,
    topics=None,
    timestamp=None,
) -> dict:
    return {
        "trace_id": f"{session_id}-{turn_index}",
        "student_id": student_id,
        "session_id": session_id,
        "session_type": session_type,
        "turn_index": turn_index,
        "timestamp": (timestamp or datetime.now(UTC)).isoformat(),
        "tokens": {"prompt": prompt_tokens, "completion": completion_tokens},
        "rag_results_count": rag_results_count,
        "labels": labels or [],
        "topics": topics or [],
    }


class TestPerStudentMetrics:
    def test_sessions_and_turns(self):
        from axiom.extensions.builtins.classroom.metrics import per_student_metrics

        traces = [
            _trace("s1", "sess-a", turn_index=0),
            _trace("s1", "sess-a", turn_index=1),
            _trace("s1", "sess-b", turn_index=0),
            _trace("s2", "sess-c", turn_index=0),
        ]
        m = per_student_metrics(traces, student_id="s1")

        assert m["student_id"] == "s1"
        assert m["sessions"] == 2
        assert m["turns"] == 3

    def test_token_consumption(self):
        from axiom.extensions.builtins.classroom.metrics import per_student_metrics

        traces = [
            _trace("s1", "a", prompt_tokens=100, completion_tokens=50),
            _trace("s1", "a", turn_index=1, prompt_tokens=200, completion_tokens=100),
        ]
        m = per_student_metrics(traces, student_id="s1")
        assert m["tokens"]["prompt"] == 300
        assert m["tokens"]["completion"] == 150
        assert m["tokens"]["total"] == 450

    def test_rag_hit_rate(self):
        from axiom.extensions.builtins.classroom.metrics import per_student_metrics

        traces = [
            _trace("s1", "a", turn_index=0, rag_results_count=3),
            _trace("s1", "a", turn_index=1, rag_results_count=0),  # miss
            _trace("s1", "a", turn_index=2, rag_results_count=5),
            _trace("s1", "a", turn_index=3, rag_results_count=0),  # miss
        ]
        m = per_student_metrics(traces, student_id="s1")
        assert m["rag_hit_rate"] == 0.5

    def test_label_distribution(self):
        from axiom.extensions.builtins.classroom.metrics import per_student_metrics

        traces = [
            _trace("s1", "a", labels=["q_and_a"]),
            _trace("s1", "a", turn_index=1, labels=["q_and_a", "exploratory"]),
            _trace("s1", "a", turn_index=2, labels=["exploratory"]),
        ]
        m = per_student_metrics(traces, student_id="s1")
        assert m["labels"]["q_and_a"] == 2
        assert m["labels"]["exploratory"] == 2

    def test_topic_distribution(self):
        from axiom.extensions.builtins.classroom.metrics import per_student_metrics

        traces = [
            _trace("s1", "a", topics=["LO-1"]),
            _trace("s1", "a", turn_index=1, topics=["LO-1", "LO-2"]),
            _trace("s1", "a", turn_index=2, topics=["LO-3"]),
        ]
        m = per_student_metrics(traces, student_id="s1")
        assert m["topics"]["LO-1"] == 2
        assert m["topics"]["LO-2"] == 1
        assert m["topics"]["LO-3"] == 1

    def test_session_type_breakdown(self):
        from axiom.extensions.builtins.classroom.metrics import per_student_metrics

        traces = [
            _trace("s1", "a", session_type="chat"),
            _trace("s1", "b", session_type="quiz"),
            _trace("s1", "b", turn_index=1, session_type="quiz"),
            _trace("s1", "c", session_type="interview"),
        ]
        m = per_student_metrics(traces, student_id="s1")
        assert m["session_types"]["chat"] == 1
        assert m["session_types"]["quiz"] == 1
        assert m["session_types"]["interview"] == 1


class TestCohortMetrics:
    def test_aggregates_across_students(self):
        from axiom.extensions.builtins.classroom.metrics import cohort_metrics

        traces = [
            _trace("s1", "a"),
            _trace("s2", "b"),
            _trace("s2", "c"),
            _trace("s3", "d"),
        ]
        m = cohort_metrics(traces)
        assert m["total_students"] == 3
        assert m["total_sessions"] == 4
        assert m["total_turns"] == 4

    def test_cohort_means(self):
        from axiom.extensions.builtins.classroom.metrics import cohort_metrics

        traces = [
            _trace("s1", "a", turn_index=0),
            _trace("s1", "a", turn_index=1),  # s1: 2 turns
            _trace("s2", "b", turn_index=0),  # s2: 1 turn
        ]
        m = cohort_metrics(traces)
        # Per-student turn counts: [2, 1] → mean = 1.5
        assert abs(m["mean_turns_per_student"] - 1.5) < 1e-9

    def test_per_student_rollup(self):
        from axiom.extensions.builtins.classroom.metrics import cohort_metrics

        traces = [
            _trace("s1", "a"),
            _trace("s2", "b"),
        ]
        m = cohort_metrics(traces)
        assert len(m["students"]) == 2
        sids = {s["student_id"] for s in m["students"]}
        assert sids == {"s1", "s2"}


class TestTimeSeriesBinning:
    def test_weekly_bins(self):
        from axiom.extensions.builtins.classroom.metrics import weekly_turns

        base = datetime(2026, 1, 5, tzinfo=UTC)  # Monday
        traces = [
            _trace("s1", "a", timestamp=base),
            _trace("s1", "a", turn_index=1, timestamp=base + timedelta(days=2)),
            _trace("s1", "b", timestamp=base + timedelta(days=8)),  # next week
        ]
        bins = weekly_turns(traces, student_id="s1")
        assert len(bins) >= 2
        # First week has 2 turns
        assert bins[0]["turns"] == 2
        # Second week has 1 turn
        assert bins[1]["turns"] == 1


class TestEmptyTraces:
    def test_empty_returns_zero_metrics(self):
        from axiom.extensions.builtins.classroom.metrics import per_student_metrics

        m = per_student_metrics([], student_id="s1")
        assert m["sessions"] == 0
        assert m["turns"] == 0
        assert m["tokens"]["total"] == 0
        assert m["rag_hit_rate"] is None  # no turns = undefined

    def test_empty_cohort(self):
        from axiom.extensions.builtins.classroom.metrics import cohort_metrics

        m = cohort_metrics([])
        assert m["total_students"] == 0


class TestFederationMetricsClaim:
    """Federation stretch: cross-node metrics aggregation (ADR-023).

    Student traces may live on their home node; the instructor hub
    receives signed per-student-metric summaries that can be merged
    into a cohort view without moving raw traces.
    """

    def test_serialize_metrics_claim(self):
        from axiom.extensions.builtins.classroom.metrics import (
            per_student_metrics,
            serialize_metrics_claim,
        )

        traces = [_trace("s1", "a"), _trace("s1", "a", turn_index=1)]
        m = per_student_metrics(traces, student_id="s1")
        claim = serialize_metrics_claim(m, signer_node="prague.axiom.eu",
                                         classroom_id="cr")

        assert claim["student_id"] == "s1"
        assert claim["classroom_id"] == "cr"
        assert claim["signer_node"] == "prague.axiom.eu"
        assert claim["metrics"] == m
        assert "signature" in claim

    def test_merge_metrics_claims_into_cohort(self):
        from axiom.extensions.builtins.classroom.metrics import (
            merge_metrics_claims,
        )

        claims = [
            {"student_id": "s1", "classroom_id": "cr", "signer_node": "a",
             "metrics": {"student_id": "s1", "sessions": 2, "turns": 5,
                         "tokens": {"total": 500, "prompt": 400, "completion": 100},
                         "rag_hit_rate": 0.8, "labels": {}, "topics": {},
                         "session_types": {}}},
            {"student_id": "s2", "classroom_id": "cr", "signer_node": "b",
             "metrics": {"student_id": "s2", "sessions": 1, "turns": 3,
                         "tokens": {"total": 300, "prompt": 250, "completion": 50},
                         "rag_hit_rate": 0.5, "labels": {}, "topics": {},
                         "session_types": {}}},
        ]
        cohort = merge_metrics_claims(claims, trust_verifier=lambda c: True)

        assert cohort["total_students"] == 2
        assert cohort["total_sessions"] == 3
        assert cohort["total_turns"] == 8

    def test_untrusted_claims_excluded(self):
        from axiom.extensions.builtins.classroom.metrics import (
            merge_metrics_claims,
        )

        claims = [
            {"student_id": "attacker", "classroom_id": "cr", "signer_node": "bad",
             "metrics": {"student_id": "a", "sessions": 1000, "turns": 5000,
                         "tokens": {"total": 0, "prompt": 0, "completion": 0},
                         "rag_hit_rate": 1.0, "labels": {}, "topics": {},
                         "session_types": {}}},
        ]
        cohort = merge_metrics_claims(claims, trust_verifier=lambda c: False)
        assert cohort["total_students"] == 0
        assert cohort["rejected"] == 1
