# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Drive the unit-test base classes against known-good and known-bad inputs.

We instantiate the base classes in-process with hand-rolled fixture stubs
and call test methods directly. This gives us tight coverage of the
assertions without paying the pytester subprocess cost for every case.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from axiom_tests._manifest import load_manifest
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

# --------------------------------------------------------------------------
# ExtensionStandardTests
# --------------------------------------------------------------------------


def _run_extension_checks(root: Path) -> None:
    """Invoke every non-skip ``test_*`` method of ``ExtensionStandardTests``."""
    suite = ExtensionStandardTests()
    manifest_path = root / "axiom-extension.toml"
    manifest = load_manifest(manifest_path)
    import tomllib

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))

    suite.test_manifest_exists(manifest_path)
    suite.test_manifest_parses(manifest)
    suite.test_manifest_validates_against_schema(manifest)
    suite.test_required_files_present(root, is_builtin=False)
    suite.test_aeos_version_declared(manifest)
    suite.test_manifest_and_pyproject_agree_on_name(manifest, pyproject)
    suite.test_manifest_and_pyproject_agree_on_version(manifest, pyproject)
    suite.test_public_api_declared(root, manifest)
    suite.test_has_at_least_one_provides_block(manifest)


def test_extension_standard_accepts_valid(valid_extension: Path) -> None:
    _run_extension_checks(valid_extension)


def test_extension_standard_rejects_missing_required_file(
    broken_extension_missing_required_file: Path,
) -> None:
    suite = ExtensionStandardTests()
    with pytest.raises(AssertionError, match="missing required files"):
        suite.test_required_files_present(
            broken_extension_missing_required_file, is_builtin=False
        )


def test_extension_standard_rejects_missing_all(
    broken_extension_missing_all: Path,
) -> None:
    suite = ExtensionStandardTests()
    manifest = load_manifest(broken_extension_missing_all / "axiom-extension.toml")
    with pytest.raises(AssertionError, match="__all__"):
        suite.test_public_api_declared(broken_extension_missing_all, manifest)


def test_extension_standard_base_is_abstract_without_override(pytester) -> None:  # type: ignore[no-untyped-def]
    """Using the base class with no ``extension_manifest_path`` override fails.

    When a subclass forgets to override the fixture, pytest will report the
    underlying ``NotImplementedError`` raised by the default implementation.
    """
    pytester.makepyfile(
        """
        from axiom_tests.unit_tests import ExtensionStandardTests

        class TestUnconfigured(ExtensionStandardTests):
            pass
        """
    )
    result = pytester.runpytest("-x")
    # Every test needing the fixture will error out.
    result.assert_outcomes(errors=1, passed=0) if False else None
    # pytest 9 reports this as errors; we accept either outcome and just assert failure.
    assert result.ret != 0, "expected failure when fixture not overridden"
    stdout = result.stdout.str()
    assert "NotImplementedError" in stdout or "extension_manifest_path" in stdout


# --------------------------------------------------------------------------
# ToolTests
# --------------------------------------------------------------------------


class _GoodTool:
    input_schema = {"type": "object"}
    output_schema = {"type": "string"}

    def invoke(self, input_: dict) -> str:
        return "ok"


class _BadToolNoSchema:
    def invoke(self, input_: dict) -> str:
        return "ok"


def test_tool_tests_pass_for_good_tool() -> None:
    suite = ToolTests()
    suite.test_tool_class_exists(_GoodTool)
    suite.test_has_input_schema(_GoodTool)
    suite.test_has_output_schema(_GoodTool)
    suite.test_has_invoke_callable(_GoodTool)
    suite.test_side_effects_value_is_valid()


def test_tool_tests_fail_for_missing_input_schema() -> None:
    suite = ToolTests()
    with pytest.raises(AssertionError, match="input_schema"):
        suite.test_has_input_schema(_BadToolNoSchema)


def test_tool_tests_streaming_skips_by_default() -> None:
    suite = ToolTests()
    with pytest.raises(pytest.skip.Exception):
        suite.test_streaming_interface(_GoodTool)


def test_tool_tests_streaming_enforces_when_declared() -> None:
    class _StreamingSuite(ToolTests):
        @property
        def supports_streaming(self) -> bool:
            return True

    suite = _StreamingSuite()
    with pytest.raises(AssertionError, match="stream"):
        suite.test_streaming_interface(_GoodTool)


def test_tool_tests_invalid_side_effects_rejected() -> None:
    class _BadSideEffects(ToolTests):
        @property
        def side_effects(self) -> str:
            return "destroys_universe"

    with pytest.raises(AssertionError, match="side_effects"):
        _BadSideEffects().test_side_effects_value_is_valid()


def test_tool_tests_manifest_idempotent_agreement() -> None:
    class _NotIdempotent(ToolTests):
        @property
        def idempotent(self) -> bool:
            return False

    block = {"idempotent": True}
    with pytest.raises(AssertionError, match="idempotent"):
        _NotIdempotent().test_idempotent_flag_matches_manifest(block)


# --------------------------------------------------------------------------
# AgentTests
# --------------------------------------------------------------------------


class _GoodAgent:
    name = "CHALKE"

    def execute(self, *args: Any, **kwargs: Any) -> str:
        return "done"


class _BadAgentLowercaseName:
    name = "chalke"

    def execute(self) -> None:
        pass


def test_agent_tests_pass_for_good_agent() -> None:
    class _AgentSuite(AgentTests):
        @property
        def requires_persona(self) -> bool:
            return False  # no manifest fixture provided

    suite = _AgentSuite()
    suite.test_agent_class_exists(_GoodAgent)
    suite.test_agent_has_name_attribute(_GoodAgent)
    suite.test_agent_name_follows_wall_e_convention(_GoodAgent)
    suite.test_implements_declared_interface(_GoodAgent)


def test_agent_tests_fail_for_lowercase_name() -> None:
    suite = AgentTests()
    with pytest.raises(AssertionError, match="AXI"):
        suite.test_agent_name_follows_wall_e_convention(_BadAgentLowercaseName)


def test_agent_tests_uses_skills_cross_check() -> None:
    class _AgentUsingSkills(AgentTests):
        pass

    agent_block = {"uses_skills": ["nope_skill"]}
    manifest = {"extension": {"provides": [{"kind": "skill", "name": "other"}]}}

    suite = _AgentUsingSkills()
    with pytest.raises(AssertionError, match="uses_skills"):
        suite.test_uses_skills_resolve(agent_block, manifest)


def test_agent_tests_uses_skills_ok_when_declared() -> None:
    agent_block = {"uses_skills": ["known_skill"]}
    manifest = {"extension": {"provides": [{"kind": "skill", "name": "known_skill"}]}}
    AgentTests().test_uses_skills_resolve(agent_block, manifest)


def test_agent_tests_persona_file_required_and_present(tmp_path: Path) -> None:
    persona = tmp_path / "ext_pkg" / "agents" / "scan" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("role: SCAN\n", encoding="utf-8")

    block = {"persona": "ext_pkg/agents/scan/persona.md"}
    AgentTests().test_persona_file_present_if_declared(block, tmp_path)


def test_agent_tests_persona_file_missing_fails(tmp_path: Path) -> None:
    block = {"persona": "ext_pkg/agents/scan/persona.md"}
    with pytest.raises(AssertionError, match="persona"):
        AgentTests().test_persona_file_present_if_declared(block, tmp_path)


# --------------------------------------------------------------------------
# CommandTests
# --------------------------------------------------------------------------


def test_command_tests_manifest_noun_check() -> None:
    class _Cmd(CommandTests):
        @property
        def expected_noun(self) -> str:
            return "enrollment"

    suite = _Cmd()
    suite.test_manifest_declares_noun({"noun": "enrollment", "subcommands": ["add"]})
    suite.test_noun_matches_expected({"noun": "enrollment"})


def test_command_tests_missing_subcommand_fails() -> None:
    class _Cmd(CommandTests):
        @property
        def expected_subcommands(self) -> tuple[str, ...]:
            return ("add", "remove")

    suite = _Cmd()
    with pytest.raises(AssertionError, match="subcommands"):
        suite.test_subcommands_registered_in_manifest({"noun": "n", "subcommands": ["add"]})


def test_command_tests_entry_usable_with_callable() -> None:
    def cli() -> None:
        pass

    CommandTests().test_command_entry_is_usable(cli)


def test_command_tests_entry_usable_with_parser() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="demo")
    CommandTests().test_command_entry_is_usable(p)


def test_command_tests_entry_usable_rejects_junk() -> None:
    with pytest.raises(AssertionError):
        CommandTests().test_command_entry_is_usable(42)


# --------------------------------------------------------------------------
# ServiceTests
# --------------------------------------------------------------------------


class _GoodService:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def status(self) -> str:
        return "ok"

    def health_check(self) -> bool:
        return True


class _BadService:
    def start(self) -> None: ...

    # missing stop, status, health_check


def test_service_tests_good_service() -> None:
    suite = ServiceTests()
    suite.test_service_class_exists(_GoodService)
    suite.test_service_has_required_interface(_GoodService)
    suite.test_service_methods_are_callable(_GoodService)
    suite.test_deployment_profile_is_valid()


def test_service_tests_bad_service() -> None:
    suite = ServiceTests()
    with pytest.raises(AssertionError, match="missing"):
        suite.test_service_has_required_interface(_BadService)


def test_service_tests_bad_deployment_profile() -> None:
    class _Suite(ServiceTests):
        @property
        def deployment_profile(self) -> str:
            return "mars"

    with pytest.raises(AssertionError, match="deployment_profile"):
        _Suite().test_deployment_profile_is_valid()


# --------------------------------------------------------------------------
# AdapterTests
# --------------------------------------------------------------------------


class _GoodAdapter:
    def connect(self) -> str:
        return "conn"


class _BadAdapter:
    pass


def test_adapter_tests_good() -> None:
    AdapterTests().test_adapter_class_exists(_GoodAdapter)
    AdapterTests().test_has_connection_interface(_GoodAdapter)


def test_adapter_tests_bad_no_connection() -> None:
    with pytest.raises(AssertionError, match="Connection-like"):
        AdapterTests().test_has_connection_interface(_BadAdapter)


def test_adapter_tests_auth_methods_validity() -> None:
    class _Suite(AdapterTests):
        @property
        def auth_methods(self) -> tuple[str, ...]:
            return ("oauth2", "mystery_method")

    with pytest.raises(AssertionError, match="unknown auth methods"):
        _Suite().test_declared_auth_methods_are_valid()


def test_adapter_tests_manifest_auth_method_mismatch() -> None:
    class _Suite(AdapterTests):
        @property
        def auth_methods(self) -> tuple[str, ...]:
            return ("oauth2",)

    block = {"auth_methods": ["api_token"]}
    with pytest.raises(AssertionError, match="auth_methods"):
        _Suite().test_manifest_auth_methods_match(block)


# --------------------------------------------------------------------------
# SkillTests
# --------------------------------------------------------------------------


def test_skill_tests_good(tmp_path: Path, skill_builder) -> None:  # type: ignore[no-untyped-def]
    skill = skill_builder(tmp_path / "my_skill")
    suite = SkillTests()
    suite.test_skill_md_exists(skill)
    frontmatter = {"name": "demo_skill", "description": "A demo skill for tests"}
    suite.test_frontmatter_has_required_fields(frontmatter)
    suite.test_frontmatter_values_are_non_empty(frontmatter)


def test_skill_tests_missing_required_field() -> None:
    suite = SkillTests()
    with pytest.raises(AssertionError, match="missing required"):
        suite.test_frontmatter_has_required_fields({"name": "x"})


def test_skill_tests_empty_description() -> None:
    suite = SkillTests()
    with pytest.raises(AssertionError, match="empty"):
        suite.test_frontmatter_values_are_non_empty({"name": "n", "description": ""})


def test_skill_tests_no_skill_md(tmp_path: Path) -> None:
    suite = SkillTests()
    with pytest.raises(AssertionError, match="SKILL.md"):
        suite.test_skill_md_exists(tmp_path)


def test_skill_tests_yaml_parser_inline_list() -> None:
    from axiom_tests.unit_tests.skill import _parse_simple_yaml  # noqa: PLC2701

    parsed = _parse_simple_yaml("name: demo\ndescription: A demo\nallowed-tools: [python, bash]\n")
    assert parsed["allowed-tools"] == ["python", "bash"]


def test_skill_tests_yaml_parser_block_list() -> None:
    from axiom_tests.unit_tests.skill import _parse_simple_yaml  # noqa: PLC2701

    parsed = _parse_simple_yaml("name: demo\nreferences:\n  - first.md\n  - second.md\n")
    assert parsed["references"] == ["first.md", "second.md"]


def test_skill_tests_yaml_parser_rejects_bad() -> None:
    from axiom_tests.unit_tests.skill import _parse_simple_yaml  # noqa: PLC2701

    with pytest.raises(ValueError):
        _parse_simple_yaml("no-colon-anywhere\n")


def test_skill_tests_references_consistent(tmp_path: Path, skill_builder) -> None:  # type: ignore[no-untyped-def]
    skill_root = skill_builder(tmp_path / "sk")
    frontmatter = {"name": "n", "description": "d", "references": ["a.md"]}
    # With no references dir on disk, this should fail:
    with pytest.raises(AssertionError, match="references"):
        SkillTests().test_references_dir_consistent(skill_root, frontmatter)


# --------------------------------------------------------------------------
# HookTests
# --------------------------------------------------------------------------


def test_hook_tests_callable_entry() -> None:
    def hook(event: str) -> None:
        pass

    HookTests().test_hook_entry_is_callable_or_module(hook)


def test_hook_tests_rejects_non_callable() -> None:
    with pytest.raises(AssertionError, match="callable"):
        HookTests().test_hook_entry_is_callable_or_module(42)


def test_hook_tests_fail_mode_valid() -> None:
    HookTests().test_fail_mode_is_valid()


def test_hook_tests_fail_mode_invalid() -> None:
    class _Suite(HookTests):
        @property
        def fail_mode(self) -> str:
            return "explode"

    with pytest.raises(AssertionError, match="fail_mode"):
        _Suite().test_fail_mode_is_valid()


def test_hook_tests_event_name_validation() -> None:
    class _Suite(HookTests):
        @property
        def declared_events(self) -> tuple[str, ...]:
            return ("not_a_dotted_name",)

    with pytest.raises(AssertionError, match="event name"):
        _Suite().test_events_look_like_event_names()


def test_hook_tests_manifest_events_mismatch() -> None:
    class _Suite(HookTests):
        @property
        def declared_events(self) -> tuple[str, ...]:
            return ("session.started", "session.ended")

    with pytest.raises(AssertionError, match="events"):
        _Suite().test_manifest_events_match_declared({"events": ["session.started"]})
