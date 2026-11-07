# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for axiom/agents/turn_classifier.py — generic core.

Domain-specific behavior (learning-objective matching) lives in
extensions and is covered by their own tests.
"""

from __future__ import annotations


class TestDeterministicLabels:
    def test_question_marks_flag_q_and_a(self):
        from axiom.agents.turn_classifier import deterministic_labels

        assert "q_and_a" in deterministic_labels("what is fission?")

    def test_generative_verbs_flag_generative(self):
        from axiom.agents.turn_classifier import deterministic_labels

        assert "generative" in deterministic_labels("write a function")

    def test_no_match_defaults_to_exploratory(self):
        from axiom.agents.turn_classifier import deterministic_labels

        assert deterministic_labels("let's think about it") == ["exploratory"]


class TestSessionClassification:
    def test_classify_session_uses_principal_id(self):
        from axiom.agents.turn_classifier import classify_session

        c = classify_session(
            turns=[{"role": "user", "content": "What is X?"}],
            session_id="s",
            principal_id="u1",
            classifier=None,
        )
        assert c.session_id == "s"
        assert c.principal_id == "u1"
        assert "q_and_a" in c.labels


class TestLLMBackend:
    def test_llm_takes_precedence(self):
        from axiom.agents.turn_classifier import classify_session

        def fake_llm(turns, **kw):
            return {"labels": ["exploratory"], "topics": ["T1"], "rationale": "LLM"}

        c = classify_session(
            turns=[{"role": "user", "content": "what?"}],
            session_id="s", principal_id="u1",
            classifier=fake_llm,
        )
        assert c.labels == ["exploratory"]
        assert c.topics == ["T1"]
        assert c.rationale == "LLM"


class TestAnnotate:
    def test_annotate_propagates_labels(self):
        from axiom.agents.turn_classifier import (
            SessionClassification,
            annotate_traces,
        )

        traces = [{"trace_id": "t1", "session_id": "s"}]
        cs = [SessionClassification(
            session_id="s", principal_id="u1",
            labels=["q_and_a"], topics=["T1"],
        )]
        out = annotate_traces(traces, cs)
        assert out[0]["labels"] == ["q_and_a"]
        assert out[0]["topics"] == ["T1"]
