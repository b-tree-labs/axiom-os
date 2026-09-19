# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Regression test: AXI persona reaches the LLM via PromptComposer.

Per the 2026-04-28 audit + persona_loader wire-up: AXI's
`_build_system_prompt` must include the agent's `persona.md` text in
the identity layer. Before the fix, only `neut_agent_base` (registry
prompt) made it to identity; persona.md was decorative.
"""

from __future__ import annotations

from pathlib import Path


class TestWallEPersonaInSystemPrompt:
    def test_persona_md_text_appears_in_built_system_prompt(self):
        # Verify the wire by exercising the same primitives _build_system_prompt
        # uses: load_agent_persona pointed at AXI's package, then a
        # PromptComposer.add(...) into the identity layer. The full
        # _build_system_prompt also touches workspace/session/composition
        # state which would require a full ChatAgent fixture; the wire
        # itself is what matters here.
        from axiom.agents.persona_loader import load_agent_persona
        from axiom.infra.prompt_composer import PromptComposer

        persona_path = (
            Path(__file__).parent.parent / "agents" / "axi" / "persona.md"
        )
        persona_text = persona_path.read_text(encoding="utf-8").strip()
        assert persona_text, "AXI persona.md should not be empty"
        first_heading = persona_text.split("\n", 1)[0]

        # The exact call _build_system_prompt makes
        loaded = load_agent_persona(persona_path.parent)
        assert loaded == persona_text

        composer = PromptComposer()
        composer.add(
            "identity", name="persona:axi",
            content=loaded, source="agent:axi", required=True,
        )

        rendered = composer.render_text()
        assert first_heading in rendered

    def test_build_system_prompt_call_site_loads_wall_e_persona(self):
        # Static guard: the wire must remain in _build_system_prompt so
        # AXI's persona reaches the LLM during real chat turns. If
        # someone removes the call, this test fails fast.
        agent_source = (
            Path(__file__).parent.parent / "agent.py"
        ).read_text(encoding="utf-8")
        assert 'load_agent_persona(' in agent_source
        assert 'axi' in agent_source
        assert 'persona:axi' in agent_source
