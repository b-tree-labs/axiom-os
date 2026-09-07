# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Composable SKILLS.md — extensions contribute skill fragments to built-in agents.

Core agent has a monolithic SKILLS.md. Extensions register additional
skill fragments via axiom-extension.toml [[agent_skills.<agent>]].
At runtime, fragments compose into the agent's full skill set.

Composition is ADDITIVE: extensions can add skills but never remove
core skills. Fragments are SCOPED: only active when the extension is
loaded. Fragments follow the same Authorization Model pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def compose_agent_skills(
    core_skills: str,
    fragments: list[dict[str, Any]],
) -> str:
    """Compose an agent's core SKILLS.md with extension fragments.

    Args:
        core_skills: the agent's base SKILLS.md content
        fragments: list of {extension, content, priority?} dicts

    Returns:
        Composed markdown: core + sorted fragments with boundary markers.
    """
    if not fragments:
        return core_skills

    # Sort by priority (lower = first)
    sorted_fragments = sorted(fragments, key=lambda f: f.get("priority", 50))

    parts = [core_skills.rstrip()]

    for frag in sorted_fragments:
        ext_name = frag.get("extension", "unknown")
        content = frag.get("content", "")
        parts.append(f"\n\n---\n\n<!-- Extension: {ext_name} -->\n\n{content.rstrip()}")

    return "\n".join(parts) + "\n"


def discover_skill_fragments(
    agent_name: str,
    extension_dirs: list[Path],
) -> list[dict[str, Any]]:
    """Discover skill fragments for an agent from loaded extensions.

    Scans each extension directory for axiom-extension.toml entries
    matching [[agent_skills.<agent_name>]], loads the referenced
    markdown file, and returns the fragments.

    Args:
        agent_name: the agent to find fragments for (e.g. "triage", "neut")
        extension_dirs: list of extension root directories to scan

    Returns:
        List of {extension, content, description, priority} dicts.
    """
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

    fragments = []

    for ext_dir in extension_dirs:
        manifest_path = ext_dir / "axiom-extension.toml"
        if not manifest_path.exists():
            continue

        with open(manifest_path, "rb") as f:
            manifest = tomllib.load(f)

        ext_name = manifest.get("extension", {}).get("name", ext_dir.name)

        # Look for [[agent_skills.<agent_name>]] entries
        agent_skills = manifest.get("agent_skills", {})
        skills_for_agent = agent_skills.get(agent_name, [])

        # Handle both list-of-dicts (TOML array of tables) and single dict
        if isinstance(skills_for_agent, dict):
            skills_for_agent = [skills_for_agent]

        for skill_entry in skills_for_agent:
            skill_file = skill_entry.get("file", "")
            skill_path = ext_dir / skill_file
            if not skill_path.exists():
                continue

            content = skill_path.read_text()
            fragments.append(
                {
                    "extension": ext_name,
                    "content": content,
                    "description": skill_entry.get("description", ""),
                    "priority": skill_entry.get("priority", 50),
                }
            )

    return fragments
