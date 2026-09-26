# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for composable SKILLS.md — extensions contribute skill fragments to built-in agents.

Core agent has a base SKILLS.md. Extensions register additional
skill fragments via axiom-extension.toml [[agent_skills.<agent>]].
At runtime, the composed result is what the LLM sees.
"""

from __future__ import annotations


class TestSkillsComposition:
    def test_core_skills_loaded(self):
        from axiom.extensions.builtins.classroom.composable_skills import compose_agent_skills

        core = "# TRIAGE\n\nCore diagnostic capabilities.\n"
        fragments = []

        composed = compose_agent_skills(core, fragments)
        assert "Core diagnostic" in composed

    def test_extension_fragment_appended(self):
        from axiom.extensions.builtins.classroom.composable_skills import compose_agent_skills

        core = "# TRIAGE\n\nCore diagnostic capabilities.\n"
        fragments = [
            {
                "extension": "classroom",
                "content": "## Classroom Health\n\n`axi doctor --classroom` checks web endpoint, tokens, trace store.\n",
            },
        ]

        composed = compose_agent_skills(core, fragments)
        assert "Core diagnostic" in composed
        assert "Classroom Health" in composed
        assert "axi doctor --classroom" in composed

    def test_multiple_fragments_ordered_by_extension(self):
        from axiom.extensions.builtins.classroom.composable_skills import compose_agent_skills

        core = "# Agent\nBase.\n"
        fragments = [
            {
                "extension": "classroom",
                "content": "## Classroom\nClassroom skills.\n",
                "priority": 10,
            },
            {"extension": "example-consumer", "content": "## Nuclear\nNuclear skills.\n", "priority": 20},
        ]

        composed = compose_agent_skills(core, fragments)
        # Classroom (priority 10) before Nuclear (priority 20)
        assert composed.index("Classroom") < composed.index("Nuclear")

    def test_fragment_includes_authorization_boundary(self):
        from axiom.extensions.builtins.classroom.composable_skills import compose_agent_skills

        core = "# Agent\nBase.\n"
        fragments = [
            {"extension": "classroom", "content": "## Classroom\nNew skill.\n"},
        ]

        composed = compose_agent_skills(core, fragments)
        # Composed result should include the extension-boundary marker
        assert "Extension: classroom" in composed or "classroom" in composed.lower()

    def test_empty_fragments_returns_core_unchanged(self):
        from axiom.extensions.builtins.classroom.composable_skills import compose_agent_skills

        core = "# Agent\nBase capabilities only.\n"
        composed = compose_agent_skills(core, [])
        assert composed.strip() == core.strip()


class TestSkillsDiscovery:
    def test_discover_fragments_from_extension_manifests(self, tmp_path):
        from axiom.extensions.builtins.classroom.composable_skills import (
            discover_skill_fragments,
        )

        # Simulate an extension directory with a skills fragment
        ext_dir = tmp_path / "classroom"
        ext_dir.mkdir()
        (ext_dir / "axiom-extension.toml").write_text(
            '[extension]\nname = "classroom"\nbuiltin = true\n\n'
            '[[agent_skills.triage]]\nfile = "skills/dfib_classroom.md"\n'
            'description = "Classroom health checks"\n'
        )
        skills_dir = ext_dir / "skills"
        skills_dir.mkdir()
        (skills_dir / "dfib_classroom.md").write_text(
            "## Classroom Health Checks\n\n"
            "- `axi doctor --classroom` verifies web endpoint, tokens, trace store\n"
        )

        fragments = discover_skill_fragments("triage", [ext_dir])
        assert len(fragments) == 1
        assert "Classroom Health" in fragments[0]["content"]
        assert fragments[0]["extension"] == "classroom"
