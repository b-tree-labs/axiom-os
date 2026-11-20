# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for the agent persona loader.

Until 2026-04-28, every Axiom agent had a `persona.md` next to its
package, and zero of them were ever read at runtime — the LLM saw
hardcoded one-liners. The persona loader closes that gap by exposing a
single function that resolves an agent's persona text by package path,
so callers (extension manifests, classroom pipeline, RIVET narrative
path) can compose it into the system prompt.
"""

from __future__ import annotations

import textwrap


class TestLoadAgentPersona:
    def test_reads_persona_md_relative_to_package(self, tmp_path):
        from axiom.agents.persona_loader import load_agent_persona

        agent_dir = tmp_path / "fake_agent"
        agent_dir.mkdir()
        persona_text = textwrap.dedent(
            """
            # FAKE — Test Agent

            ## Identity
            A test agent for verifying the loader.
            """
        ).strip()
        (agent_dir / "persona.md").write_text(persona_text)

        result = load_agent_persona(agent_dir)
        assert result == persona_text

    def test_returns_empty_string_when_persona_missing(self, tmp_path):
        from axiom.agents.persona_loader import load_agent_persona

        result = load_agent_persona(tmp_path / "no_such_dir")
        assert result == ""

    def test_returns_empty_string_when_persona_is_empty_file(self, tmp_path):
        from axiom.agents.persona_loader import load_agent_persona

        agent_dir = tmp_path / "fake_agent"
        agent_dir.mkdir()
        (agent_dir / "persona.md").write_text("")

        assert load_agent_persona(agent_dir) == ""


class TestComposeWithPersona:
    def test_persona_lands_in_identity_layer(self, tmp_path):
        from axiom.agents.persona_loader import compose_system_prompt
        from axiom.infra.prompt_composer import PromptComposer

        agent_dir = tmp_path / "fake_agent"
        agent_dir.mkdir()
        (agent_dir / "persona.md").write_text("# IDENTITY\nFake persona.")

        composer = PromptComposer()
        compose_system_prompt(
            composer,
            agent_persona_dir=agent_dir,
            agent_name="fake",
            course_system_prompt="Course-specific framing here.",
        )

        text = composer.render_text()
        assert "# IDENTITY" in text
        assert "Fake persona." in text
        assert "Course-specific framing here." in text
        # Identity layer must come before domain_context layer
        assert text.index("Fake persona.") < text.index("Course-specific framing here.")

    def test_missing_persona_still_composes_course_prompt(self, tmp_path):
        from axiom.agents.persona_loader import compose_system_prompt
        from axiom.infra.prompt_composer import PromptComposer

        composer = PromptComposer()
        compose_system_prompt(
            composer,
            agent_persona_dir=tmp_path / "no_such_dir",
            agent_name="ghost",
            course_system_prompt="Just the course.",
        )

        text = composer.render_text()
        assert "Just the course." in text
