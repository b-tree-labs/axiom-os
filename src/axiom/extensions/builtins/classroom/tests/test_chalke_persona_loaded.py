# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for CHALKE persona-into-prompt wiring.

Per the 2026-04-28 audit: the persona used to be decorative. These
tests pin the wire that makes it land in actual LLM calls — both in
the `StudentView.explain` path and in the classroom chat pipeline's
composed system prompt.
"""

from __future__ import annotations


class TestStudentExplainSendsPersona:
    def test_system_prompt_contains_chalk_e_identity(self):
        from axiom.extensions.builtins.classroom.agents.chalke.chalke import Chalke

        captured: dict = {}

        def capture_llm(messages, **kw):
            captured["messages"] = messages
            return "ok"

        chalke = Chalke(
            classroom_id="test-room",
            composition=None,  # not exercised by explain()
            llm_backend=capture_llm,
        )
        chalke.for_student("s1").explain(topic="reactor kinetics")

        system_msg = next(m for m in captured["messages"] if m["role"] == "system")
        assert "CHALKE" in system_msg["content"]
        # Persona content should land — these phrases come from persona.md
        assert (
            "two perspectives" in system_msg["content"].lower()
            or "instructor" in system_msg["content"].lower()
        )
        # Student overlay should narrow the perspective for this turn
        assert "s1" in system_msg["content"]


class TestClassroomCliComposesPersona:
    """Pin the wire that puts CHALKE persona ahead of course framing.

    `create_classroom` itself is integration-heavy (Canvas LMS, real
    enrollment), so we exercise the composition step directly using the
    same primitives the CLI uses. If anyone removes the
    `compose_system_prompt(...)` call from classroom_cli.py, this test
    won't catch it — but `test_chalke_persona_lands_in_pipeline_path`
    in tests/agents/test_persona_loader.py covers that primitive.
    """

    def test_persona_then_course_framing_in_composer(self):
        from pathlib import Path

        from axiom.agents.persona_loader import compose_system_prompt
        from axiom.infra.prompt_composer import PromptComposer

        chalke_dir = (
            Path(__file__).parent.parent / "agents" / "chalke"
        )
        composer = PromptComposer()
        compose_system_prompt(
            composer,
            agent_persona_dir=chalke_dir,
            agent_name="chalke",
            course_system_prompt="Course: NE 101. Tone: rigorous.",
        )

        text = composer.render_text()
        assert "CHALKE" in text
        assert "Course: NE 101" in text
        assert text.index("CHALKE") < text.index("Course: NE 101")
