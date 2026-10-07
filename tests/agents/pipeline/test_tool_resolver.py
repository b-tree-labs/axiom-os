# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for axiom.agents.pipeline.tool_resolver — ADR-034 §D4.

Plans reference tool IDs; at validation time every tool_id must resolve to
an installed AEOS extension capability. These tests cover:

- StaticToolResolver: in-memory resolver populated from a list of descriptors.
- discover_installed_tools: walks a directory of axiom-extension.toml manifests.
- validate_plan_tools: end-to-end plan validation against a resolver.
- ToolDescriptor immutability and classification predicate semantics.

The classification semantics tested here follow the prompt's Phase-1 rule:
"a tool whose classification_required.level == 'cui' requires inputs at >= CUI;
a tool with classification_required = None or unclassified accepts any input."
"""

from __future__ import annotations

import textwrap

import pytest

from axiom.agents.pipeline.plan import Plan, PlanRequest, PlanStep
from axiom.agents.pipeline.tool_resolver import (
    StaticToolResolver,
    ToolDescriptor,
    ToolResolutionError,
    discover_installed_tools,
    validate_plan_tools,
)
from axiom.vega.federation.policy import ClassificationStamp

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stamp(level: str) -> ClassificationStamp:
    return ClassificationStamp(level=level)


def _descriptor(
    tool_id: str,
    *,
    extension_name: str = "ext",
    extension_version: str = "0.1.0",
    kind: str = "tool",
    classification_required: ClassificationStamp | None = None,
    description: str = "",
) -> ToolDescriptor:
    return ToolDescriptor(
        tool_id=tool_id,
        extension_name=extension_name,
        extension_version=extension_version,
        kind=kind,  # type: ignore[arg-type]
        classification_required=classification_required,
        description=description,
    )


def _plan_with_steps(*steps: PlanStep) -> Plan:
    request = PlanRequest(
        goal="t",
        scope_id="s",
        principal_id="@a:t",
        accountable_human_id="@h:t",
    )
    return Plan(request=request, steps=tuple(steps))


# ---------------------------------------------------------------------------
# ToolDescriptor
# ---------------------------------------------------------------------------


class TestToolDescriptor:
    def test_descriptor_is_frozen(self):
        d = _descriptor("ext.classroom.ask")
        with pytest.raises((AttributeError, TypeError)):
            d.tool_id = "other"  # type: ignore[misc]

    def test_descriptor_required_fields(self):
        d = _descriptor(
            "ext.classroom.ask",
            extension_name="classroom",
            extension_version="0.2.0",
            kind="tool",
            description="ask a question",
        )
        assert d.tool_id == "ext.classroom.ask"
        assert d.extension_name == "classroom"
        assert d.extension_version == "0.2.0"
        assert d.kind == "tool"
        assert d.classification_required is None
        assert d.description == "ask a question"


# ---------------------------------------------------------------------------
# StaticToolResolver
# ---------------------------------------------------------------------------


class TestStaticToolResolver:
    def test_resolve_known_id_returns_descriptor(self):
        d = _descriptor("ext.classroom.ask")
        r = StaticToolResolver(tools_by_id={d.tool_id: d})
        assert r.resolve("ext.classroom.ask") is d

    def test_resolve_unknown_id_raises(self):
        r = StaticToolResolver(tools_by_id={})
        with pytest.raises(ToolResolutionError) as exc:
            r.resolve("ext.missing")
        assert "ext.missing" in str(exc.value)

    def test_list_tools_returns_sequence(self):
        d1 = _descriptor("a")
        d2 = _descriptor("b")
        r = StaticToolResolver(tools_by_id={"a": d1, "b": d2})
        tools = r.list_tools()
        assert set(tools) == {d1, d2}
        assert len(tools) == 2

    def test_from_descriptors_factory(self):
        d1 = _descriptor("a")
        d2 = _descriptor("b")
        r = StaticToolResolver.from_descriptors([d1, d2])
        assert r.resolve("a") is d1
        assert r.resolve("b") is d2

    def test_from_descriptors_rejects_duplicates(self):
        d1 = _descriptor("a")
        d2 = _descriptor("a")
        with pytest.raises(ValueError):
            StaticToolResolver.from_descriptors([d1, d2])


# ---------------------------------------------------------------------------
# Compatibility
# ---------------------------------------------------------------------------


class TestIsCompatible:
    def test_unclassified_tool_accepts_unclassified_input(self):
        d = _descriptor("a", classification_required=_stamp("unclassified"))
        r = StaticToolResolver.from_descriptors([d])
        assert r.is_compatible(d, _stamp("unclassified")) is True

    def test_none_classification_tool_accepts_anything(self):
        d = _descriptor("a", classification_required=None)
        r = StaticToolResolver.from_descriptors([d])
        assert r.is_compatible(d, _stamp("unclassified")) is True
        assert r.is_compatible(d, _stamp("cui")) is True
        assert r.is_compatible(d, _stamp("secret")) is True

    def test_cui_required_tool_accepts_cui_input(self):
        d = _descriptor("a", classification_required=_stamp("cui"))
        r = StaticToolResolver.from_descriptors([d])
        assert r.is_compatible(d, _stamp("cui")) is True

    def test_cui_required_tool_accepts_higher_input(self):
        # Phase-1 semantics: tool requires inputs at >= CUI.
        d = _descriptor("a", classification_required=_stamp("cui"))
        r = StaticToolResolver.from_descriptors([d])
        assert r.is_compatible(d, _stamp("secret")) is True
        assert r.is_compatible(d, _stamp("top_secret")) is True

    def test_cui_required_tool_rejects_unclassified_input(self):
        d = _descriptor("a", classification_required=_stamp("cui"))
        r = StaticToolResolver.from_descriptors([d])
        assert r.is_compatible(d, _stamp("unclassified")) is False

    def test_unclassified_tool_accepts_higher_classified_inputs(self):
        # An unclassified-floor tool runs on any input — the data can carry
        # higher classification but the tool imposes no minimum.
        d = _descriptor("a", classification_required=_stamp("unclassified"))
        r = StaticToolResolver.from_descriptors([d])
        assert r.is_compatible(d, _stamp("cui")) is True
        assert r.is_compatible(d, _stamp("secret")) is True


# ---------------------------------------------------------------------------
# validate_plan_tools
# ---------------------------------------------------------------------------


class TestValidatePlanTools:
    def test_empty_plan_returns_no_issues(self):
        plan = _plan_with_steps()
        r = StaticToolResolver.from_descriptors([])
        issues = validate_plan_tools(plan, r, _stamp("unclassified"))
        assert issues == ()

    def test_all_resolvable_tools_returns_no_issues(self):
        d = _descriptor("ext.x.do")
        r = StaticToolResolver.from_descriptors([d])
        plan = _plan_with_steps(
            PlanStep(intent="run", tool_id="ext.x.do"),
        )
        issues = validate_plan_tools(plan, r, _stamp("unclassified"))
        assert issues == ()

    def test_step_without_tool_id_is_skipped(self):
        r = StaticToolResolver.from_descriptors([])
        plan = _plan_with_steps(
            PlanStep(intent="think — no tool"),
        )
        issues = validate_plan_tools(plan, r, _stamp("unclassified"))
        assert issues == ()

    def test_unresolved_tool_id_yields_unresolved_issue(self):
        r = StaticToolResolver.from_descriptors([])
        step = PlanStep(intent="run", tool_id="ext.missing")
        plan = _plan_with_steps(step)
        issues = validate_plan_tools(plan, r, _stamp("unclassified"))
        assert len(issues) == 1
        issue = issues[0]
        assert issue.step_id == step.step_id
        assert issue.tool_id == "ext.missing"
        assert issue.issue == "unresolved"
        assert "ext.missing" in issue.message

    def test_classification_mismatch_yields_classification_incompatible(self):
        d = _descriptor("ext.cui_only", classification_required=_stamp("cui"))
        r = StaticToolResolver.from_descriptors([d])
        step = PlanStep(intent="run", tool_id="ext.cui_only")
        plan = _plan_with_steps(step)
        issues = validate_plan_tools(plan, r, _stamp("unclassified"))
        assert len(issues) == 1
        assert issues[0].issue == "classification_incompatible"
        assert issues[0].tool_id == "ext.cui_only"

    def test_multiple_issues_returned_in_order(self):
        d_ok = _descriptor("ext.ok")
        d_cui = _descriptor("ext.cui", classification_required=_stamp("cui"))
        r = StaticToolResolver.from_descriptors([d_ok, d_cui])
        plan = _plan_with_steps(
            PlanStep(intent="a", tool_id="ext.missing"),
            PlanStep(intent="b", tool_id="ext.ok"),
            PlanStep(intent="c", tool_id="ext.cui"),
        )
        issues = validate_plan_tools(plan, r, _stamp("unclassified"))
        # step a is unresolved; step b passes; step c is classification-incompatible.
        assert len(issues) == 2
        kinds = [i.issue for i in issues]
        assert kinds == ["unresolved", "classification_incompatible"]


# ---------------------------------------------------------------------------
# discover_installed_tools — walks axiom-extension.toml manifests.
# ---------------------------------------------------------------------------


_MANIFEST_BASIC = textwrap.dedent("""
    [extension]
    name = "demo"
    version = "1.2.3"

    [[extension.provides]]
    kind = "tool"
    name = "syllabus_extraction"
    description = "extract"

    [[extension.provides]]
    kind = "skill"
    name = "tutor"
    description = "tutoring skill"

    [[extension.provides]]
    kind = "cmd"
    noun = "demo"
    description = "demo cli"
""").lstrip()


_MANIFEST_AGENT_ONLY = textwrap.dedent("""
    [extension]
    name = "agentpkg"
    version = "0.1.0"

    [[extension.provides]]
    kind = "agent"
    name = "alpha"
    description = "agent — not a tool"
""").lstrip()


_MANIFEST_WITH_CLASSIFICATION = textwrap.dedent("""
    [extension]
    name = "secured"
    version = "0.0.1"

    [[extension.provides]]
    kind = "tool"
    name = "redact"
    classification = "cui"
    description = "redact CUI"
""").lstrip()


_MANIFEST_WITH_EXPLICIT_ID = textwrap.dedent("""
    [extension]
    name = "core"
    version = "0.0.1"

    [[extension.provides]]
    kind = "tool"
    id = "axiom.core.echo"
    name = "echo"
    description = "echo"
""").lstrip()


class TestDiscoverInstalledTools:
    def test_walks_directory_and_extracts_tool_descriptors(self, tmp_path):
        ext_dir = tmp_path / "demo"
        ext_dir.mkdir()
        (ext_dir / "axiom-extension.toml").write_text(_MANIFEST_BASIC)

        descriptors = discover_installed_tools(extensions_root=str(tmp_path))

        ids = {d.tool_id for d in descriptors}
        # tool, skill, cmd are all extracted.
        assert "demo.syllabus_extraction" in ids
        assert "demo.tutor" in ids
        assert "demo.demo" in ids  # cmd uses noun for the local name

    def test_extension_name_and_version_are_propagated(self, tmp_path):
        ext_dir = tmp_path / "demo"
        ext_dir.mkdir()
        (ext_dir / "axiom-extension.toml").write_text(_MANIFEST_BASIC)

        descriptors = discover_installed_tools(extensions_root=str(tmp_path))

        for d in descriptors:
            assert d.extension_name == "demo"
            assert d.extension_version == "1.2.3"

    def test_skips_directory_without_manifest(self, tmp_path):
        # Empty subdir with no manifest is silently skipped.
        (tmp_path / "no_manifest").mkdir()

        ext_dir = tmp_path / "demo"
        ext_dir.mkdir()
        (ext_dir / "axiom-extension.toml").write_text(_MANIFEST_BASIC)

        descriptors = discover_installed_tools(extensions_root=str(tmp_path))
        # Only demo's tools should appear.
        names = {d.extension_name for d in descriptors}
        assert names == {"demo"}

    def test_agent_only_manifests_yield_no_tool_descriptors(self, tmp_path):
        ext_dir = tmp_path / "agentpkg"
        ext_dir.mkdir()
        (ext_dir / "axiom-extension.toml").write_text(_MANIFEST_AGENT_ONLY)

        descriptors = discover_installed_tools(extensions_root=str(tmp_path))
        # agents are not tool/skill/cmd — they should not be in the resolver surface.
        assert descriptors == ()

    def test_classification_required_parsed_from_manifest(self, tmp_path):
        ext_dir = tmp_path / "secured"
        ext_dir.mkdir()
        (ext_dir / "axiom-extension.toml").write_text(_MANIFEST_WITH_CLASSIFICATION)

        descriptors = discover_installed_tools(extensions_root=str(tmp_path))
        assert len(descriptors) == 1
        d = descriptors[0]
        assert d.tool_id == "secured.redact"
        assert d.classification_required is not None
        assert d.classification_required.level == "cui"

    def test_explicit_id_overrides_dotted_default(self, tmp_path):
        ext_dir = tmp_path / "core"
        ext_dir.mkdir()
        (ext_dir / "axiom-extension.toml").write_text(_MANIFEST_WITH_EXPLICIT_ID)

        descriptors = discover_installed_tools(extensions_root=str(tmp_path))
        ids = {d.tool_id for d in descriptors}
        assert ids == {"axiom.core.echo"}

    def test_returns_empty_tuple_for_missing_root(self, tmp_path):
        nonexistent = tmp_path / "nope"
        descriptors = discover_installed_tools(extensions_root=str(nonexistent))
        assert descriptors == ()

    def test_malformed_manifest_is_skipped(self, tmp_path):
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "axiom-extension.toml").write_text("not valid = = toml [[")

        good = tmp_path / "good"
        good.mkdir()
        (good / "axiom-extension.toml").write_text(_MANIFEST_BASIC)

        descriptors = discover_installed_tools(extensions_root=str(tmp_path))
        # The bad one is skipped silently; the good one still loads.
        assert {d.extension_name for d in descriptors} == {"demo"}

    def test_axiom_home_env_var_used_when_root_omitted(self, tmp_path, monkeypatch):
        ext_dir = tmp_path / "demo"
        ext_dir.mkdir()
        (ext_dir / "axiom-extension.toml").write_text(_MANIFEST_BASIC)

        monkeypatch.setenv("AXIOM_HOME", str(tmp_path.parent))
        # Mirror the layout `$AXIOM_HOME/extensions/<ext>/manifest`.
        target = tmp_path.parent / "extensions"
        if not target.exists():
            target.symlink_to(tmp_path)

        descriptors = discover_installed_tools()
        ids = {d.tool_id for d in descriptors}
        assert "demo.syllabus_extraction" in ids
