# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression test: TRIAGE persona reaches the LLM in both Doctor + Reviewer paths.

Per the 2026-04-28 audit + persona_loader wire-up: TRIAGE's
`DoctorAgent._build_system_prompt` and `Reviewer.evaluate`'s system
text must include the agent's `persona.md` text. Before the fix, only
`_DOCTOR_SYSTEM_PROMPT` / `_REVIEWER_SYSTEM_PROMPT` made it to the
gateway; persona.md was decorative.
"""

from __future__ import annotations

from pathlib import Path


class TestDoctorPersona:
    def test_persona_text_in_doctor_system_prompt(self):
        from axiom.extensions.builtins.diagnostics.agent import DoctorAgent

        persona_path = (
            Path(__file__).parent.parent / "agents" / "triage" / "persona.md"
        )
        persona_text = persona_path.read_text(encoding="utf-8").strip()
        assert persona_text, "TRIAGE persona.md should not be empty"
        first_heading = persona_text.split("\n", 1)[0]

        agent = DoctorAgent(gateway=None, bus=None)
        system = agent._build_system_prompt({})

        assert first_heading in system, (
            f"Expected TRIAGE persona heading {first_heading!r} in Doctor "
            f"system prompt; got prefix: {system[:300]!r}"
        )


class TestReviewerPersona:
    def test_persona_text_prepended_in_reviewer_system_prompt(self):
        from axiom.extensions.builtins.diagnostics.reviewer import Reviewer

        persona_path = (
            Path(__file__).parent.parent / "agents" / "triage" / "persona.md"
        )
        persona_text = persona_path.read_text(encoding="utf-8").strip()
        first_heading = persona_text.split("\n", 1)[0]

        captured: dict = {}

        class FakeResponse:
            success = False
            text = ""
            tool_use = None

        class FakeGateway:
            def complete_with_tools(self, **kwargs):
                captured["system"] = kwargs.get("system", "")
                return FakeResponse()

        reviewer = Reviewer(gateway=FakeGateway())
        reviewer.evaluate({"fingerprint": "test", "diff": "", "diagnosis": ""})

        assert first_heading in captured["system"], (
            f"Expected TRIAGE persona heading {first_heading!r} in Reviewer "
            f"system prompt; got prefix: {captured['system'][:300]!r}"
        )
