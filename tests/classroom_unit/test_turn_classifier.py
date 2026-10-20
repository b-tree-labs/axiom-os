# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for turn/session classifier (§5.4 / spec §2.8).

Classifies student-AI conversation turns into:
- q_and_a, generative, exploratory, debugging, metacognitive, fun
- learning objectives touched (via LO keyword matching)

Two backends:
1. Deterministic keyword heuristics (standalone — works offline)
2. LLM classifier callable (optional — higher quality)
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeLLMClassifier:
    labels: list[str] = field(default_factory=lambda: ["q_and_a"])
    topics: list[str] = field(default_factory=list)
    rationale: str = "fake"
    calls: list[dict] = field(default_factory=list)

    def __call__(self, turns: list[dict], learning_objectives: list[dict], **kw) -> dict:
        self.calls.append({"turns": turns, "los": learning_objectives})
        return {
            "labels": list(self.labels),
            "topics": list(self.topics),
            "rationale": self.rationale,
        }


class TestDeterministicClassifier:
    def test_question_marks_q_and_a(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_session,
        )

        turns = [
            {"role": "user", "content": "What is fission?"},
            {"role": "assistant", "content": "Fission is..."},
            {"role": "user", "content": "How does it differ from fusion?"},
        ]
        c = classify_session(
            turns=turns, session_id="a", student_id="s1",
            learning_objectives=[],
        )
        assert "q_and_a" in c.labels

    def test_generative_verbs(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_session,
        )

        turns = [
            {"role": "user", "content": "Write a Python function that computes decay rate"},
        ]
        c = classify_session(
            turns=turns, session_id="a", student_id="s1",
            learning_objectives=[],
        )
        assert "generative" in c.labels

    def test_debugging_keywords(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_session,
        )

        turns = [
            {"role": "user", "content": "I got an exception: ValueError in my simulation"},
        ]
        c = classify_session(
            turns=turns, session_id="a", student_id="s1",
            learning_objectives=[],
        )
        assert "debugging" in c.labels

    def test_metacognitive_reflection(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_session,
        )

        turns = [
            {"role": "user", "content": "I'm struggling to understand why I keep getting this wrong. Can you help me reflect on my approach?"},
        ]
        c = classify_session(
            turns=turns, session_id="a", student_id="s1",
            learning_objectives=[],
        )
        assert "metacognitive" in c.labels

    def test_default_falls_back_to_exploratory(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_session,
        )

        turns = [
            {"role": "user", "content": "Let's dig into the implications of Maxwell's demon"},
        ]
        c = classify_session(
            turns=turns, session_id="a", student_id="s1",
            learning_objectives=[],
        )
        assert "exploratory" in c.labels


class TestLearningObjectiveMatching:
    def test_topics_matched_by_keywords(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_session,
        )

        los = [
            {"id": "LO-1", "title": "Fission basics",
             "keywords": ["fission", "critical mass", "chain reaction"]},
            {"id": "LO-2", "title": "Fusion",
             "keywords": ["fusion", "tokamak"]},
        ]
        turns = [
            {"role": "user", "content": "explain critical mass"},
            {"role": "assistant", "content": "critical mass is..."},
        ]
        c = classify_session(
            turns=turns, session_id="a", student_id="s1",
            learning_objectives=los,
        )
        assert "LO-1" in c.topics
        assert "LO-2" not in c.topics


class TestLLMBackend:
    def test_llm_overrides_deterministic_when_provided(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_session,
        )

        llm = FakeLLMClassifier(labels=["exploratory", "metacognitive"],
                                topics=["LO-1"])
        turns = [{"role": "user", "content": "What is fission?"}]

        c = classify_session(
            turns=turns, session_id="a", student_id="s1",
            learning_objectives=[{"id": "LO-1", "keywords": ["fission"]}],
            classifier=llm,
        )
        # LLM results take precedence
        assert set(c.labels) == {"exploratory", "metacognitive"}
        assert c.topics == ["LO-1"]
        assert c.rationale == "fake"
        assert len(llm.calls) == 1


class TestBatch:
    def test_batch_classify_multiple_sessions(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_batch,
        )

        sessions = {
            "sess-a": [{"role": "user", "content": "What is fission?",
                        "student_id": "s1"}],
            "sess-b": [{"role": "user", "content": "Write a simulation",
                        "student_id": "s2"}],
        }
        results = classify_batch(
            sessions=sessions, learning_objectives=[],
        )
        assert len(results) == 2
        by_id = {r.session_id: r for r in results}
        assert "q_and_a" in by_id["sess-a"].labels
        assert "generative" in by_id["sess-b"].labels


class TestAnnotation:
    def test_annotate_traces_adds_labels_and_topics(self):
        from axiom.extensions.builtins.classroom.turn_classifier import (
            SessionClassification,
            annotate_traces,
        )

        traces = [
            {"trace_id": "t1", "session_id": "a", "student_id": "s1"},
            {"trace_id": "t2", "session_id": "a", "student_id": "s1"},
            {"trace_id": "t3", "session_id": "b", "student_id": "s2"},
        ]
        classifications = [
            SessionClassification(session_id="a", principal_id="s1",
                                  labels=["q_and_a"], topics=["LO-1"],
                                  rationale=""),
            SessionClassification(session_id="b", principal_id="s2",
                                  labels=["generative"], topics=[],
                                  rationale=""),
        ]

        annotated = annotate_traces(traces, classifications)
        assert annotated[0]["labels"] == ["q_and_a"]
        assert annotated[0]["topics"] == ["LO-1"]
        assert annotated[1]["labels"] == ["q_and_a"]
        assert annotated[2]["labels"] == ["generative"]


class TestUserOnlyFilter:
    def test_classifier_ignores_assistant_turns(self):
        """Classification looks at user intent, not assistant replies."""
        from axiom.extensions.builtins.classroom.turn_classifier import (
            classify_session,
        )

        turns = [
            {"role": "user", "content": "tell me about fusion"},
            {"role": "assistant",
             "content": "Can you explain how a tokamak works? Here's the error you might see..."},
        ]
        c = classify_session(
            turns=turns, session_id="a", student_id="s1",
            learning_objectives=[],
        )
        # User turn is exploratory; shouldn't pick up debugging from
        # the assistant's mention of "error"
        assert "debugging" not in c.labels
