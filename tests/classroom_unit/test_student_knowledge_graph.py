# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the per-student knowledge graph (#27)."""

from __future__ import annotations

import pytest


@pytest.fixture
def composition(tmp_path, monkeypatch):
    monkeypatch.setenv("AXIOM_RUNTIME_ROOT", str(tmp_path))
    from axiom.extensions.builtins.classroom.composition_boot import (
        build_classroom_composition,
    )

    return build_classroom_composition(classroom_id="cr-kg")


class TestEmptyGraph:
    def test_no_fragments_empty_graph(self, composition):
        from axiom.extensions.builtins.classroom.student_knowledge_graph import (
            build_knowledge_graph,
        )

        g = build_knowledge_graph(composition, student_id="s1")
        assert g.student_id == "s1"
        assert g.nodes == {}


class TestTraceContributions:
    def test_episodic_topics_populate_nodes(self, composition):
        from axiom.extensions.builtins.classroom.student_knowledge_graph import (
            build_knowledge_graph,
        )

        # Seed a trace fragment with topics
        composition.write(
            content={
                "event_time": "2026-04-01T10:00:00Z",
                "session_type": "chat",
                "classroom_id": "cr-kg",
                "course_id": "c",
                "topics": ["fission", "moderation"],
            },
            cognitive_type="episodic",
            principal_id="s1",
            agents=set(),
            resources=set(),
        )
        g = build_knowledge_graph(composition, student_id="s1")
        assert "fission" in g.nodes
        assert "moderation" in g.nodes
        assert g.nodes["fission"].encounter_count == 1

    def test_multiple_traces_increment_count(self, composition):
        from axiom.extensions.builtins.classroom.student_knowledge_graph import (
            build_knowledge_graph,
        )

        for i, ts in enumerate([
            "2026-04-01T10:00:00Z",
            "2026-04-05T10:00:00Z",
            "2026-04-10T10:00:00Z",
        ]):
            composition.write(
                content={
                    "event_time": ts, "session_type": "chat",
                    "classroom_id": "cr-kg", "course_id": "c",
                    "topics": ["fission"],
                },
                cognitive_type="episodic",
                principal_id="s1", agents=set(), resources=set(),
            )
        g = build_knowledge_graph(composition, student_id="s1")
        assert g.nodes["fission"].encounter_count == 3
        assert g.nodes["fission"].first_encountered == "2026-04-01T10:00:00Z"
        assert g.nodes["fission"].last_reviewed == "2026-04-10T10:00:00Z"


class TestMasteryFromQuizzes:
    def test_quiz_scores_set_mastery(self, composition):
        from axiom.extensions.builtins.classroom.student_knowledge_graph import (
            build_knowledge_graph,
        )

        # Seed a semantic fragment (quiz score) with topic
        composition.write(
            content={
                "student_id": "s1",
                "assessment_id": "pre",
                "question_id": "Q1",
                "question_type": "mcq",
                "topic": "fission",
                "final_score": 0.9,
            },
            cognitive_type="semantic",
            principal_id="s1", agents=set(), resources=set(),
        )
        g = build_knowledge_graph(composition, student_id="s1")
        assert g.nodes["fission"].mastery_score == 0.9
        assert g.nodes["fission"].quiz_attempts == 1

    def test_multiple_attempts_keep_best_score(self, composition):
        from axiom.extensions.builtins.classroom.student_knowledge_graph import (
            build_knowledge_graph,
        )

        for score in [0.5, 0.9, 0.7]:
            composition.write(
                content={
                    "student_id": "s1",
                    "assessment_id": "pre",
                    "question_id": "Q1",
                    "question_type": "mcq",
                    "topic": "fission",
                    "final_score": score,
                },
                cognitive_type="semantic",
                principal_id="s1", agents=set(), resources=set(),
            )
        g = build_knowledge_graph(composition, student_id="s1")
        assert g.nodes["fission"].mastery_score == 0.9
        assert g.nodes["fission"].quiz_attempts == 3


class TestStruggleSignals:
    def test_stuck_signal_increments_struggle(self, composition):
        from axiom.extensions.builtins.classroom.student_knowledge_graph import (
            build_knowledge_graph,
        )

        # Write via tracer-like path: signal fragment belongs to instructor
        # as master but content.student_id names the subject
        composition.write(
            content={
                "event_time": "2026-04-10T10:00:00Z",
                "signal_type": "student_stuck",
                "student_id": "s1",
                "topic": "moderation",
                "severity": "medium",
            },
            cognitive_type="episodic",
            principal_id="s1", agents=set(), resources=set(),
        )
        g = build_knowledge_graph(composition, student_id="s1")
        assert g.nodes["moderation"].struggle_count == 1


class TestSummary:
    def test_summary_classifies_mastered_vs_struggling(self, composition):
        from axiom.extensions.builtins.classroom.student_knowledge_graph import (
            build_knowledge_graph,
        )

        # Strong on fission
        composition.write(
            content={"student_id": "s1", "assessment_id": "pre",
                     "question_id": "Q1", "question_type": "mcq",
                     "topic": "fission", "final_score": 0.95},
            cognitive_type="semantic",
            principal_id="s1", agents=set(), resources=set(),
        )
        # Stuck on moderation
        composition.write(
            content={
                "event_time": "2026-04-10T10:00:00Z",
                "signal_type": "student_stuck",
                "student_id": "s1", "topic": "moderation",
                "severity": "medium",
            },
            cognitive_type="episodic",
            principal_id="s1", agents=set(), resources=set(),
        )
        g = build_knowledge_graph(composition, student_id="s1")
        summary = g.summary()
        assert summary["total_concepts"] == 2
        assert "fission" in summary["top_strengths"]
        assert "moderation" in summary["active_focus_areas"]


class TestSerialization:
    def test_to_dict_is_json_safe(self, composition):
        import json

        from axiom.extensions.builtins.classroom.student_knowledge_graph import (
            build_knowledge_graph,
        )

        composition.write(
            content={
                "event_time": "2026-04-01T10:00:00Z",
                "session_type": "chat",
                "classroom_id": "cr-kg", "course_id": "c",
                "topics": ["fission"],
            },
            cognitive_type="episodic",
            principal_id="s1", agents=set(), resources=set(),
        )
        g = build_knowledge_graph(
            composition, student_id="s1",
            classroom_id="cr-kg", course_id="c",
        )
        payload = g.to_dict()
        # Round-trips through json
        s = json.dumps(payload)
        assert "fission" in s
        assert payload["student_id"] == "s1"
        assert payload["classroom_id"] == "cr-kg"
