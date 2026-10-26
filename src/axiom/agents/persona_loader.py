# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Load agent persona.md text and compose it into system prompts.

Until 2026-04-28 every Axiom agent had a `persona.md` next to its
package and zero of them were read at runtime — the LLM saw a
one-line hardcoded string. This module closes that gap.

Usage:

    from axiom.agents.persona_loader import compose_system_prompt
    from axiom.infra.prompt_composer import PromptComposer

    composer = PromptComposer()
    compose_system_prompt(
        composer,
        agent_persona_dir=Path(__file__).parent / "agents" / "chalke",
        agent_name="chalke",
        course_system_prompt=manifest.get("system_prompt", ""),
    )
    system_text = composer.render_text()
"""

from __future__ import annotations

from pathlib import Path

from axiom.infra.prompt_composer import PromptComposer


def load_agent_persona(agent_dir: Path | str) -> str:
    """Read `persona.md` from an agent's package directory.

    Returns the file's text stripped of trailing whitespace, or an empty
    string if the file is missing or empty. Never raises.
    """
    path = Path(agent_dir) / "persona.md"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.strip()


def compose_system_prompt(
    composer: PromptComposer,
    *,
    agent_persona_dir: Path | str,
    agent_name: str,
    course_system_prompt: str = "",
) -> None:
    """Wire an agent's persona + course-specific framing into a composer.

    `persona` lands in the `identity` layer; `course_system_prompt`
    lands in `domain_context`. Either may be empty — the composer is
    happy to render with one, both, or neither contribution.
    """
    persona = load_agent_persona(agent_persona_dir)
    if persona:
        composer.add(
            "identity",
            name=f"persona:{agent_name}",
            content=persona,
            source=f"agent:{agent_name}",
            required=True,
        )
    if course_system_prompt:
        composer.add(
            "domain_context",
            name="course_system_prompt",
            content=course_system_prompt,
            source="classroom",
            required=True,
        )
