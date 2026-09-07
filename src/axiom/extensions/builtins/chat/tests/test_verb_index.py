# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The chat loop can discover verbs that are NOT in its loaded tool list.

The tool list is a bounded projection (AEOS §4.9.4): only the namespaces in
``chat.tool_namespaces`` become tools. ``discover_verbs`` is the read-only
companion: an index over every registered capability and every manifest
``[[extension.provides]]`` entry, ranked cheaply, reporting for each hit
whether it is loaded, available (and how to load it), or manifest-only.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import tools, verb_index
from axiom.extensions.builtins.chat.tools import execute_tool, get_all_tools, get_tool_definitions
from axiom.extensions.builtins.chat.verb_index import (
    VerbEntry,
    build_verb_index,
    describe,
    exposure,
    how_to_load,
    search_verbs,
)
from axiom.extensions.contracts import parse_manifest
from axiom.infra.orchestrator.actions import ActionCategory, create_action
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _ok(params, ctx):
    return SkillResult(ok=True, value=None)


@pytest.fixture
def registry():
    r = SkillRegistry()
    r.register_skill(
        SkillSpec(
            name="press.draft",
            fn=_ok,
            description="Draft a release before you publish.",
            inputs={"source": "Path"},
        )
    )
    r.register_skill(
        SkillSpec(name="press.publish", fn=_ok, description="Publish.", inputs={"source": "Path"})
    )
    r.register_skill(
        SkillSpec(
            name="scan.status",
            fn=_ok,
            description="Status.",
            side_effects=False,
            surfaces=("mcp",),
        )
    )
    return r


_MANIFEST = """
[extension]
name = "triage"
version = "0.1.0"
description = "Health sweeps."

[[extension.provides]]
kind = "cmd"
noun = "triage"
entry = "axiom.extensions.builtins.triage.cli:main"
description = "Run a health sweep."

[[extension.provides]]
kind = "tool"
name = "triage_sweep"
entry = "axiom.extensions.builtins.triage.sweep:run"
description = "Sweep every registered safety check."
side_effects = "writes_receipt"

[[extension.provides]]
kind = "skill"
name = "press.draft"
entry = "axiom.extensions.builtins.press.skills:draft"
description = "Manifest copy of a registry capability."

[[extension.provides]]
kind = "hook"
events = ["pre_publish"]
entry = "axiom.extensions.builtins.triage.hooks:on_publish"
"""


@pytest.fixture
def extension(tmp_path):
    """A real ``Extension`` parsed from a real manifest, the way discovery does."""
    root = tmp_path / "triage"
    root.mkdir()
    (root / "axiom-extension.toml").write_text(_MANIFEST, encoding="utf-8")
    return parse_manifest(root / "axiom-extension.toml")


@pytest.fixture
def index(registry, extension):
    return build_verb_index(registry=registry, extensions=[extension])


def _by_name(index):
    return {e.name: e for e in index}


# ---------------------------------------------------------------------------
# Index construction
# ---------------------------------------------------------------------------


class TestBuildIndex:
    def test_registry_capabilities_are_projected(self, index):
        entries = _by_name(index)
        draft = entries["press.draft"]
        assert isinstance(draft, VerbEntry)
        assert draft.namespace == "press"
        assert draft.kind == "capability"
        assert draft.source == "registry"
        assert draft.tool_name == "press__draft"
        assert draft.description == "Draft a release before you publish."
        assert draft.inputs["type"] == "object"
        assert draft.inputs["properties"]["source"]["type"] == "string"
        assert draft.side_effects is None  # undeclared stays undeclared

        status = entries["scan.status"]
        assert status.namespace == "scan"
        assert status.side_effects is False
        assert status.surfaces == ("mcp",)
        assert status.tool_name == "scan__status"

    def test_manifest_provides_are_indexed_with_ext_prefix(self, index):
        entries = _by_name(index)
        cmd = entries["ext:cmd:triage"]
        assert cmd.kind == "cmd"
        assert cmd.namespace == "triage"
        assert cmd.source == "triage"
        assert cmd.tool_name is None
        assert cmd.description == "Run a health sweep."

        tool = entries["ext:tool:triage_sweep"]
        assert tool.kind == "tool"
        assert tool.namespace == "triage"  # non-dotted name: the extension is the namespace
        assert tool.side_effects is True  # "writes_receipt" declares side effects

    def test_registry_record_wins_over_manifest_duplicate(self, index):
        names = [e.name for e in index]
        assert names.count("press.draft") == 1
        assert "ext:skill:press.draft" not in names
        assert _by_name(index)["press.draft"].source == "registry"

    def test_unnamed_provides_are_skipped(self, index):
        assert not [e for e in index if e.kind == "hook"]

    def test_capabilities_come_first_sorted_by_name(self, index):
        caps = [e.name for e in index if e.kind == "capability"]
        assert caps == sorted(caps)
        assert [e.kind for e in index][: len(caps)] == ["capability"] * len(caps)

    def test_bad_or_missing_manifest_is_skipped_not_raised(self, registry, tmp_path):
        broken = tmp_path / "broken.toml"
        broken.write_text("this is = not [valid", encoding="utf-8")
        exts = [
            SimpleNamespace(name="broken", manifest_path=broken),
            SimpleNamespace(name="missing", manifest_path=tmp_path / "nope.toml"),
            SimpleNamespace(name="no-attr"),
        ]
        index = build_verb_index(registry=registry, extensions=exts)
        assert {e.name for e in index} == {"press.draft", "press.publish", "scan.status"}

    def test_empty_registry_and_no_extensions_is_empty(self):
        assert build_verb_index(registry=SkillRegistry(), extensions=[]) == []

    def test_default_discovery_never_raises(self):
        # Real registry + real surfaced extensions: every shipped manifest parses.
        assert isinstance(build_verb_index(), list)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    def test_name_hit_outranks_description_hit(self, index):
        names = [e.name for e in search_verbs(index, "publish")]
        assert names[0] == "press.publish"
        assert "press.draft" in names  # description mentions publish, ranks lower

    def test_namespace_query_returns_its_verbs_first(self, index):
        names = [e.name for e in search_verbs(index, "press")]
        assert names[:2] == ["press.draft", "press.publish"]
        assert "scan.status" not in names[:2]

    def test_exact_name_outranks_everything(self, index):
        assert search_verbs(index, "scan.status")[0].name == "scan.status"
        assert search_verbs(index, "scan__status")[0].name == "scan.status"
        assert search_verbs(index, "triage")[0].name == "ext:cmd:triage"

    def test_unknown_query_is_empty(self, index):
        assert search_verbs(index, "zebra quantum") == []

    def test_blank_query_is_empty(self, index):
        assert search_verbs(index, "") == []
        assert search_verbs(index, "   ") == []

    def test_limit_is_respected(self, index):
        assert len(search_verbs(index, "press", limit=1)) == 1
        assert len(search_verbs(index, "press", limit=0)) == 0

    def test_case_insensitive(self, index):
        assert [e.name for e in search_verbs(index, "PUBLISH")] == [
            e.name for e in search_verbs(index, "publish")
        ]

    def test_stable_order_between_equal_scores(self, index):
        first = [e.name for e in search_verbs(index, "press")]
        second = [e.name for e in search_verbs(index, "press")]
        assert first == second


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


class TestExposure:
    def test_loaded_available_and_manifest_only(self, index):
        entries = _by_name(index)
        assert exposure(entries["press.draft"], ["press"]) == "loaded"
        assert exposure(entries["press.publish"], ["press"]) == "loaded"
        assert exposure(entries["scan.status"], ["press"]) == "available"
        assert exposure(entries["ext:cmd:triage"], ["press"]) == "manifest-only"
        assert exposure(entries["ext:cmd:triage"], ["triage"]) == "manifest-only"

    def test_nothing_loaded_by_default(self, index):
        assert exposure(_by_name(index)["press.draft"], []) == "available"

    def test_how_to_load_names_the_setting_and_namespace(self, index):
        entries = _by_name(index)
        hint = how_to_load(entries["scan.status"], ["press"])
        assert "chat.tool_namespaces" in hint
        assert "scan" in hint
        assert "scan__status" in hint
        assert how_to_load(entries["press.draft"], ["press"]) is None
        manifest_hint = how_to_load(entries["ext:cmd:triage"], ["press"])
        assert "triage" in manifest_hint and "chat.tool_namespaces" not in manifest_hint

    def test_describe_shape(self, index):
        row = describe(_by_name(index)["scan.status"], ["press"])
        assert set(row) == {
            "name",
            "namespace",
            "kind",
            "description",
            "tool_name",
            "exposure",
            "how_to_load",
        }
        assert row["exposure"] == "available"


# ---------------------------------------------------------------------------
# The chat tool
# ---------------------------------------------------------------------------


@pytest.fixture
def wired(monkeypatch, registry):
    """Registry injected, no extensions; namespaces set via the returned setter."""
    monkeypatch.setattr(tools, "_skill_registry", lambda: registry)
    monkeypatch.setattr(verb_index, "_default_extensions", lambda: [])

    def _set(namespaces):
        monkeypatch.setattr(tools, "_skill_namespaces", lambda: list(namespaces))

    _set(["press"])
    return _set


class TestDiscoverVerbsTool:
    def test_registered_as_read_only_builtin(self):
        tool = get_all_tools()["discover_verbs"]
        assert tool.category == ActionCategory.READ
        assert tool.parameters["required"] == ["query"]
        assert set(tool.parameters["properties"]) == {"query", "limit"}
        # The approval gate classifies by name; discovery must never be confirm-gated.
        assert create_action("discover_verbs", {"query": "x"}).category == ActionCategory.READ

    def test_advertised_to_the_model(self):
        assert "discover_verbs" in {d["function"]["name"] for d in get_tool_definitions()}

    def test_finds_an_unloaded_capability_and_says_how_to_load_it(self, wired):
        result = execute_tool("discover_verbs", {"query": "status"})
        assert result["query"] == "status"
        assert result["loaded_namespaces"] == ["press"]
        match = result["matches"][0]
        assert match["name"] == "scan.status"
        assert match["tool_name"] == "scan__status"
        assert match["exposure"] == "available"
        assert "chat.tool_namespaces" in match["how_to_load"]

    def test_reports_loaded_when_namespace_is_configured(self, wired):
        wired(["scan"])
        match = execute_tool("discover_verbs", {"query": "status"})["matches"][0]
        assert match["exposure"] == "loaded"
        assert match["how_to_load"] is None

    def test_limit_parameter(self, wired):
        result = execute_tool("discover_verbs", {"query": "press", "limit": 1})
        assert len(result["matches"]) == 1
        # Garbage limit falls back to the default instead of failing.
        result = execute_tool("discover_verbs", {"query": "press", "limit": "lots"})
        assert len(result["matches"]) == 2

    def test_empty_registry_and_no_extensions_is_no_matches(self, monkeypatch):
        monkeypatch.setattr(tools, "_skill_registry", lambda: SkillRegistry())
        monkeypatch.setattr(tools, "_skill_namespaces", lambda: [])
        monkeypatch.setattr(verb_index, "_default_extensions", lambda: [])
        result = execute_tool("discover_verbs", {"query": "status"})
        assert result["matches"] == []
        assert "error" not in result

    def test_registry_failure_is_no_matches_not_an_exception(self, monkeypatch):
        def _boom():
            raise RuntimeError("registry down")

        monkeypatch.setattr(tools, "_skill_registry", _boom)
        monkeypatch.setattr(tools, "_skill_namespaces", lambda: [])
        monkeypatch.setattr(verb_index, "_default_extensions", lambda: [])
        monkeypatch.setattr(verb_index, "_default_registry", _boom)
        result = execute_tool("discover_verbs", {"query": "status"})
        assert result["matches"] == []

    def test_blank_query_is_an_error_result(self, wired):
        assert "error" in execute_tool("discover_verbs", {"query": "  "})
        assert "error" in execute_tool("discover_verbs", {})
