# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression test: TIDY persona reaches the LLM in the diagnose path.

Per the 2026-04-28 audit: `axi hygiene diagnose` is TIDY's one LLM-mediated
path. The hardcoded `_MO_SYSTEM_PROMPT` was the only thing reaching the
gateway; persona.md was decorative. This test pins the wire that
prepends persona text ahead of the operational prompt.
"""

from __future__ import annotations

from pathlib import Path


class TestMoDiagnosePersona:
    def test_persona_text_in_system_prompt(self):
        from axiom.extensions.builtins.hygiene.agent import MoAgent

        persona_path = (
            Path(__file__).parent.parent / "agents" / "tidy" / "persona.md"
        )
        persona_text = persona_path.read_text(encoding="utf-8").strip()
        assert persona_text, "TIDY persona.md should not be empty"
        first_heading = persona_text.split("\n", 1)[0]

        captured: dict = {}

        class FakeResponse:
            success = False
            error = "test"
            text = ""
            tool_use = None

        class FakeGateway:
            def complete_with_tools(self, **kwargs):
                captured["system"] = kwargs.get("system", "")
                return FakeResponse()

        agent = MoAgent(gateway=FakeGateway())

        # Stub manager so diagnose() proceeds past the early return
        class StubMgr:
            pass

        agent.set_manager(StubMgr(), monitor=None)
        agent.diagnose({"type": "test_signal", "level": "info"})

        assert first_heading in captured["system"], (
            f"Expected TIDY persona heading {first_heading!r} in diagnose "
            f"system prompt; got prefix: {captured['system'][:300]!r}"
        )
        # The operational prompt must still be present after the persona
        assert "TIDY (Micro-Obliterator)" in captured["system"]
