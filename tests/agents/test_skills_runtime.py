# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for skills_runtime — wiring agent skills into the PromptComposer.

Closes the runtime loop so a SKILLS.md (plus optional extension fragments
declared via ``[[agent_skills.<agent>]]``) actually lands in the system
prompt the agent sends to the model. Until this commit, the composer
function existed but nothing invoked it against a running composer.
"""

from __future__ import annotations

from pathlib import Path


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ext_with_skill_fragment(root: Path, ext_name: str, agent: str, fragment_text: str) -> Path:
    """Create an extension directory with a manifest declaring an agent_skills
    fragment for ``agent``. Returns the extension root path.
    """
    ext = root / ext_name
    ext.mkdir(parents=True)
    _write(ext / "skill_fragment.md", fragment_text)
    manifest = (
        "[extension]\n"
        f'name = "{ext_name}"\n'
        'version = "0.1.0"\n\n'
        f"[[agent_skills.{agent}]]\n"
        'file = "skill_fragment.md"\n'
        'description = "Fragment for testing"\n'
        "priority = 50\n"
    )
    _write(ext / "axiom-extension.toml", manifest)
    return ext


# ---------------------------------------------------------------------------
# Core behavior
# ---------------------------------------------------------------------------


def test_weave_adds_base_skills_to_identity_layer(tmp_path: Path) -> None:
    from axiom.agents.skills_runtime import weave_agent_skills
    from axiom.infra.prompt_composer import PromptComposer

    base = tmp_path / "dfib_SKILLS.md"
    _write(base, "# TRIAGE\n\nCore diagnostic capabilities.\n")

    composer = PromptComposer()
    added = weave_agent_skills(
        composer,
        agent_name="triage",
        base_skills_path=base,
        extension_dirs=[],
    )

    assert added == 1
    rendered = composer.render_text()
    assert "Core diagnostic capabilities" in rendered


def test_weave_adds_base_plus_extension_fragment(tmp_path: Path) -> None:
    from axiom.agents.skills_runtime import weave_agent_skills
    from axiom.infra.prompt_composer import PromptComposer

    base = tmp_path / "dfib_SKILLS.md"
    _write(base, "# TRIAGE\n\nCore diagnostic capabilities.\n")

    ext_root = tmp_path / "exts"
    ext_root.mkdir()
    _ext_with_skill_fragment(
        ext_root,
        ext_name="classroom",
        agent="triage",
        fragment_text="## classroom-specific diagnostics\n\nCheck LMS connectivity.\n",
    )

    composer = PromptComposer()
    weave_agent_skills(
        composer,
        agent_name="triage",
        base_skills_path=base,
        extension_dirs=[ext_root / "classroom"],
    )

    rendered = composer.render_text()
    assert "Core diagnostic capabilities" in rendered
    assert "classroom-specific diagnostics" in rendered
    assert "Check LMS connectivity" in rendered
    # Boundary marker from compose_agent_skills identifies the contributing ext
    assert "Extension: classroom" in rendered


def test_weave_fragment_only_for_target_agent(tmp_path: Path) -> None:
    """An extension's agent_skills.<other> entries must not leak into this agent."""
    from axiom.agents.skills_runtime import weave_agent_skills
    from axiom.infra.prompt_composer import PromptComposer

    base = tmp_path / "dfib_SKILLS.md"
    _write(base, "# TRIAGE\n\nCore.\n")

    # Extension contributes to a DIFFERENT agent.
    ext_root = tmp_path / "exts"
    ext_root.mkdir()
    _ext_with_skill_fragment(
        ext_root,
        ext_name="other_ext",
        agent="scan",
        fragment_text="## scan-only\n\nShould not appear in triage's prompt.\n",
    )

    composer = PromptComposer()
    weave_agent_skills(
        composer,
        agent_name="triage",
        base_skills_path=base,
        extension_dirs=[ext_root / "other_ext"],
    )

    rendered = composer.render_text()
    assert "Core" in rendered
    assert "scan-only" not in rendered


# ---------------------------------------------------------------------------
# Robustness — a broken skill path must never break the prompt
# ---------------------------------------------------------------------------


def test_weave_missing_base_is_noop(tmp_path: Path) -> None:
    from axiom.agents.skills_runtime import weave_agent_skills
    from axiom.infra.prompt_composer import PromptComposer

    composer = PromptComposer()
    composer.add(
        "identity", name="existing", content="other identity content",
        source="test", required=True,
    )

    added = weave_agent_skills(
        composer,
        agent_name="triage",
        base_skills_path=tmp_path / "does-not-exist.md",
        extension_dirs=[],
    )

    assert added == 0
    rendered = composer.render_text()
    assert "other identity content" in rendered


def test_weave_broken_manifest_swallowed(tmp_path: Path) -> None:
    from axiom.agents.skills_runtime import weave_agent_skills
    from axiom.infra.prompt_composer import PromptComposer

    base = tmp_path / "SKILLS.md"
    _write(base, "# core\n")

    broken_ext = tmp_path / "broken"
    broken_ext.mkdir()
    _write(broken_ext / "axiom-extension.toml", "this is not = valid TOML ][")

    composer = PromptComposer()
    # Must NOT raise; must still add the base skills.
    added = weave_agent_skills(
        composer,
        agent_name="triage",
        base_skills_path=base,
        extension_dirs=[broken_ext],
    )

    assert added == 1
    assert "# core" in composer.render_text()


# ---------------------------------------------------------------------------
# Layer + naming discipline
# ---------------------------------------------------------------------------


def test_weave_uses_identity_layer_by_default(tmp_path: Path) -> None:
    from axiom.agents.skills_runtime import weave_agent_skills
    from axiom.infra.prompt_composer import PromptComposer

    base = tmp_path / "SKILLS.md"
    _write(base, "# core\n")

    composer = PromptComposer()
    weave_agent_skills(
        composer,
        agent_name="triage",
        base_skills_path=base,
        extension_dirs=[],
    )

    debug = composer.debug()
    layers = {c.layer for c in debug}
    assert "identity" in layers


def test_weave_names_contribution_by_agent(tmp_path: Path) -> None:
    from axiom.agents.skills_runtime import weave_agent_skills
    from axiom.infra.prompt_composer import PromptComposer

    base = tmp_path / "SKILLS.md"
    _write(base, "# core\n")

    composer = PromptComposer()
    weave_agent_skills(
        composer,
        agent_name="triage",
        base_skills_path=base,
        extension_dirs=[],
    )

    names = {c.name for c in composer.debug()}
    # Exact name is an implementation detail, but agent name should be referenced.
    assert any("triage" in name for name in names), f"agent name not in contribution names: {names}"
