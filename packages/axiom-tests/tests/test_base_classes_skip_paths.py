# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Exercise the opt-in ``pytest.skip`` paths and remaining assertion branches.

These tests complement ``test_base_classes.py`` by driving the "nothing
provided" branches — where the subclass hasn't supplied a manifest fixture
or hasn't enabled a capability — so coverage reaches the skip calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom_tests.unit_tests import (
    AdapterTests,
    AgentTests,
    CommandTests,
    ExtensionStandardTests,
    HookTests,
    ServiceTests,
    SkillTests,
    ToolTests,
)

# ---- ExtensionStandardTests skip paths ----------------------------------


def test_extension_agents_md_skips_when_not_required(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception):
        ExtensionStandardTests().test_agents_md_present_if_required(tmp_path)


def test_extension_agents_md_required_and_missing(tmp_path: Path) -> None:
    class _Strict(ExtensionStandardTests):
        @property
        def require_agents_md(self) -> bool:
            return True

    with pytest.raises(AssertionError, match="AGENTS.md"):
        _Strict().test_agents_md_present_if_required(tmp_path)


def test_extension_docs_dir_skips_when_not_required(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception):
        ExtensionStandardTests().test_docs_dir_present_if_required(tmp_path)


def test_extension_docs_dir_required_and_missing(tmp_path: Path) -> None:
    class _Strict(ExtensionStandardTests):
        @property
        def require_docs_dir(self) -> bool:
            return True

    with pytest.raises(AssertionError, match="docs/"):
        _Strict().test_docs_dir_present_if_required(tmp_path)


def test_extension_manifest_pyproject_version_mismatch(valid_extension: Path) -> None:
    suite = ExtensionStandardTests()
    bad_pyproject = {"project": {"name": "demo_extension", "version": "9.9.9"}}
    bad_manifest = {"extension": {"name": "demo_extension", "version": "0.1.0"}}
    with pytest.raises(AssertionError, match="version"):
        suite.test_manifest_and_pyproject_agree_on_version(bad_manifest, bad_pyproject)


def test_extension_pyproject_missing_name(valid_extension: Path) -> None:
    suite = ExtensionStandardTests()
    with pytest.raises(AssertionError, match="missing \\[project\\].name"):
        suite.test_manifest_and_pyproject_agree_on_name(
            {"extension": {"name": "demo_extension"}},
            {"project": {}},
        )


def test_extension_pyproject_missing_version(valid_extension: Path) -> None:
    suite = ExtensionStandardTests()
    with pytest.raises(AssertionError, match="missing \\[project\\].version"):
        suite.test_manifest_and_pyproject_agree_on_version(
            {"extension": {"version": "0.1.0"}},
            {"project": {}},
        )


def test_extension_missing_init_file(tmp_path: Path) -> None:
    # Lay out a root with pyproject + manifest but no __init__.py anywhere.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='no_init'\n", encoding="utf-8")
    manifest = {"extension": {"name": "no_init"}}
    with pytest.raises(AssertionError, match="__init__.py"):
        ExtensionStandardTests().test_public_api_declared(tmp_path, manifest)


def test_extension_missing_aeos_version() -> None:
    with pytest.raises(AssertionError, match="aeos_version"):
        ExtensionStandardTests().test_aeos_version_declared({"extension": {"name": "x"}})


def test_extension_no_provides_block() -> None:
    with pytest.raises(AssertionError, match="provides"):
        ExtensionStandardTests().test_has_at_least_one_provides_block({"extension": {}})


# ---- Tool skip paths ----------------------------------------------------


def test_tool_idempotent_skip_when_no_block() -> None:
    with pytest.raises(pytest.skip.Exception):
        ToolTests().test_idempotent_flag_matches_manifest(None)


def test_tool_idempotent_skip_when_block_lacks_field() -> None:
    with pytest.raises(pytest.skip.Exception):
        ToolTests().test_idempotent_flag_matches_manifest({"name": "x"})


def test_tool_side_effects_skip_when_no_block() -> None:
    with pytest.raises(pytest.skip.Exception):
        ToolTests().test_side_effects_matches_manifest(None)


def test_tool_side_effects_skip_when_block_lacks_field() -> None:
    with pytest.raises(pytest.skip.Exception):
        ToolTests().test_side_effects_matches_manifest({"name": "x"})


def test_tool_side_effects_agree() -> None:
    ToolTests().test_side_effects_matches_manifest({"side_effects": "none"})


def test_tool_idempotent_agree() -> None:
    ToolTests().test_idempotent_flag_matches_manifest({"idempotent": True})


# ---- Agent skip paths ---------------------------------------------------


def test_agent_persona_skip_when_no_manifest() -> None:
    with pytest.raises(pytest.skip.Exception):
        AgentTests().test_persona_file_present_if_declared(None, None)


def test_agent_persona_skip_when_block_has_no_persona(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception):
        AgentTests().test_persona_file_present_if_declared({"name": "x"}, tmp_path)


def test_agent_persona_skip_when_subclass_opted_out() -> None:
    class _NoPersona(AgentTests):
        @property
        def requires_persona(self) -> bool:
            return False

    with pytest.raises(pytest.skip.Exception):
        _NoPersona().test_persona_file_present_if_declared({"persona": "x"}, None)


def test_agent_uses_skills_skip_when_missing_context() -> None:
    with pytest.raises(pytest.skip.Exception):
        AgentTests().test_uses_skills_resolve(None, None)


def test_agent_uses_skills_skip_when_empty() -> None:
    with pytest.raises(pytest.skip.Exception):
        AgentTests().test_uses_skills_resolve({"uses_skills": []}, {"extension": {"provides": []}})


def test_agent_uses_skills_ignores_dotted_references() -> None:
    """Dotted skill references live in another extension and are allowed."""
    AgentTests().test_uses_skills_resolve(
        {"uses_skills": ["other_ext.their_skill"]},
        {"extension": {"provides": []}},
    )


def test_agent_missing_execute_when_declared() -> None:
    class _Missing:
        name = "SCAN"

    suite = AgentTests()
    suite.test_agent_class_exists(_Missing)
    suite.test_agent_has_name_attribute(_Missing)
    # execute is required by default
    with pytest.raises(AssertionError, match="execute"):
        suite.test_implements_declared_interface(_Missing)


def test_agent_missing_name_attribute() -> None:
    class _NoName:
        pass

    with pytest.raises(AssertionError, match="name"):
        AgentTests().test_agent_has_name_attribute(_NoName)


def test_agent_empty_name_attribute() -> None:
    class _Empty:
        name = ""

    with pytest.raises(AssertionError, match="non-empty"):
        AgentTests().test_agent_has_name_attribute(_Empty)


def test_agent_non_class_fixture() -> None:
    with pytest.raises(AssertionError, match="class"):
        AgentTests().test_agent_class_exists("not-a-class")


# ---- Command skip paths -------------------------------------------------


def test_command_noun_skip_when_no_block() -> None:
    with pytest.raises(pytest.skip.Exception):
        CommandTests().test_manifest_declares_noun(None)


def test_command_noun_skip_when_no_expected() -> None:
    with pytest.raises(pytest.skip.Exception):
        CommandTests().test_noun_matches_expected({"noun": "enrollment"})


def test_command_subcommands_skip_when_no_block() -> None:
    with pytest.raises(pytest.skip.Exception):
        CommandTests().test_subcommands_registered_in_manifest(None)


def test_command_subcommands_skip_when_nothing_expected() -> None:
    with pytest.raises(pytest.skip.Exception):
        CommandTests().test_subcommands_registered_in_manifest({"noun": "n"})


def test_command_noun_missing_raises() -> None:
    with pytest.raises(AssertionError, match="noun"):
        CommandTests().test_manifest_declares_noun({"subcommands": ["a"]})


# ---- Service skip paths -------------------------------------------------


def test_service_manifest_skip_when_no_block() -> None:
    with pytest.raises(pytest.skip.Exception):
        ServiceTests().test_manifest_deployment_profile_matches(None)


def test_service_manifest_skip_when_block_lacks_profile() -> None:
    with pytest.raises(pytest.skip.Exception):
        ServiceTests().test_manifest_deployment_profile_matches({"name": "x"})


def test_service_manifest_profile_agreement() -> None:
    ServiceTests().test_manifest_deployment_profile_matches({"deployment_profile": "workstation"})


def test_service_non_callable_method() -> None:
    class _Weird:
        start = "not a function"

        def stop(self) -> None: ...
        def status(self) -> None: ...
        def health_check(self) -> None: ...

    with pytest.raises(AssertionError, match="not callable"):
        ServiceTests().test_service_methods_are_callable(_Weird)


# ---- Adapter skip paths -------------------------------------------------


def test_adapter_auth_skip_paths() -> None:
    with pytest.raises(pytest.skip.Exception):
        AdapterTests().test_manifest_auth_methods_match(None)
    with pytest.raises(pytest.skip.Exception):
        AdapterTests().test_manifest_auth_methods_match({"no_auth": True})


def test_adapter_auth_skip_when_subclass_empty() -> None:
    with pytest.raises(pytest.skip.Exception):
        AdapterTests().test_manifest_auth_methods_match({"auth_methods": ["oauth2"]})


def test_adapter_capabilities_skip_paths() -> None:
    with pytest.raises(pytest.skip.Exception):
        AdapterTests().test_manifest_capabilities_match(None)
    with pytest.raises(pytest.skip.Exception):
        AdapterTests().test_manifest_capabilities_match({"nope": []})


def test_adapter_capabilities_skip_when_subclass_empty() -> None:
    with pytest.raises(pytest.skip.Exception):
        AdapterTests().test_manifest_capabilities_match({"capabilities": ["read"]})


def test_adapter_capabilities_agreement_match() -> None:
    class _Suite(AdapterTests):
        @property
        def capabilities(self) -> tuple[str, ...]:
            return ("read", "write")

    _Suite().test_manifest_capabilities_match({"capabilities": ["write", "read"]})


def test_adapter_capabilities_agreement_mismatch() -> None:
    class _Suite(AdapterTests):
        @property
        def capabilities(self) -> tuple[str, ...]:
            return ("read",)

    with pytest.raises(AssertionError, match="capabilities"):
        _Suite().test_manifest_capabilities_match({"capabilities": ["write"]})


def test_adapter_auth_agreement_match() -> None:
    class _Suite(AdapterTests):
        @property
        def auth_methods(self) -> tuple[str, ...]:
            return ("oauth2", "api_token")

    _Suite().test_manifest_auth_methods_match({"auth_methods": ["api_token", "oauth2"]})


# ---- Hook skip paths ----------------------------------------------------


def test_hook_manifest_skip_paths() -> None:
    with pytest.raises(pytest.skip.Exception):
        HookTests().test_manifest_fail_mode_matches(None)
    with pytest.raises(pytest.skip.Exception):
        HookTests().test_manifest_fail_mode_matches({"no_fail_mode": None})
    with pytest.raises(pytest.skip.Exception):
        HookTests().test_manifest_events_match_declared(None)
    with pytest.raises(pytest.skip.Exception):
        HookTests().test_manifest_events_match_declared({"no_events": None})


def test_hook_manifest_skip_when_subclass_empty() -> None:
    with pytest.raises(pytest.skip.Exception):
        HookTests().test_manifest_events_match_declared({"events": ["session.started"]})


def test_hook_manifest_events_agreement() -> None:
    class _Suite(HookTests):
        @property
        def declared_events(self) -> tuple[str, ...]:
            return ("session.started",)

    _Suite().test_manifest_events_match_declared({"events": ["session.started"]})


def test_hook_manifest_fail_mode_agreement() -> None:
    HookTests().test_manifest_fail_mode_matches({"fail_mode": "warn"})


def test_hook_module_as_entry() -> None:
    import types

    mod = types.ModuleType("fake_hooks")
    HookTests().test_hook_entry_is_callable_or_module(mod)


# ---- Skill remaining paths ----------------------------------------------


def test_skill_scripts_consistent(tmp_path: Path, skill_builder) -> None:  # type: ignore[no-untyped-def]
    skill_root = skill_builder(tmp_path / "sk")
    # Declare scripts but don't create the dir.
    frontmatter = {"name": "n", "description": "d", "scripts": ["go.py"]}
    with pytest.raises(AssertionError, match="scripts"):
        SkillTests().test_scripts_dir_consistent(skill_root, frontmatter)
    # Create it and re-check (happy path).
    (skill_root / "scripts").mkdir()
    SkillTests().test_scripts_dir_consistent(skill_root, frontmatter)


def test_skill_assets_consistent(tmp_path: Path, skill_builder) -> None:  # type: ignore[no-untyped-def]
    skill_root = skill_builder(tmp_path / "sk")
    frontmatter = {"name": "n", "description": "d", "assets": ["logo.png"]}
    with pytest.raises(AssertionError, match="assets"):
        SkillTests().test_assets_dir_consistent(skill_root, frontmatter)
    (skill_root / "assets").mkdir()
    SkillTests().test_assets_dir_consistent(skill_root, frontmatter)


def test_skill_references_happy(tmp_path: Path, skill_builder) -> None:  # type: ignore[no-untyped-def]
    skill_root = skill_builder(tmp_path / "sk")
    (skill_root / "references").mkdir()
    SkillTests().test_references_dir_consistent(skill_root, {"references": ["a.md"]})


def test_skill_scripts_skip_when_unreferenced() -> None:
    with pytest.raises(pytest.skip.Exception):
        SkillTests().test_scripts_dir_consistent(Path("/tmp"), {"name": "x"})
    with pytest.raises(pytest.skip.Exception):
        SkillTests().test_assets_dir_consistent(Path("/tmp"), {"name": "x"})
    with pytest.raises(pytest.skip.Exception):
        SkillTests().test_references_dir_consistent(Path("/tmp"), {"name": "x"})


def test_skill_frontmatter_missing_yaml_fails(tmp_path: Path) -> None:
    skill = tmp_path / "bad_skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# No frontmatter here\n", encoding="utf-8")
    # The fixture lookup would call pytest.fail; we directly check by
    # driving the regex.
    from axiom_tests.unit_tests.skill import SKILL_FRONTMATTER_RE

    assert SKILL_FRONTMATTER_RE.match((skill / "SKILL.md").read_text()) is None


def test_skill_yaml_parser_plain_value() -> None:
    from axiom_tests.unit_tests.skill import _parse_simple_yaml

    parsed = _parse_simple_yaml('name: demo\n# comment line\ndescription: "quoted value"\n')
    assert parsed == {"name": "demo", "description": "quoted value"}


def test_skill_yaml_parser_empty_value() -> None:
    from axiom_tests.unit_tests.skill import _parse_simple_yaml

    parsed = _parse_simple_yaml("name:\n")
    assert parsed["name"] == ""


def test_skill_yaml_parser_empty_inline_list() -> None:
    from axiom_tests.unit_tests.skill import _parse_simple_yaml

    parsed = _parse_simple_yaml("tags: []\n")
    assert parsed["tags"] == []


# ---- Misc: _format_error branches --------------------------------------


def test_format_error_root_path() -> None:
    """Validation error with empty path is rendered at ``$``."""
    from axiom_tests._manifest import validate_manifest

    errors = validate_manifest({})
    assert errors
    # Root-level error should be rendered at the root.
    assert any(e.startswith("$:") for e in errors)
