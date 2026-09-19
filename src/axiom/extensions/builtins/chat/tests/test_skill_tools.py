# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Capabilities → agent-facing LLM tools (AEOS §4.9 agent_tool projection)."""

from __future__ import annotations

from axiom.extensions.builtins.chat.skill_tools import (
    skill_to_tool_name,
    skills_to_tool_definitions,
    tool_to_skill_name,
)
from axiom.infra.orchestrator.actions import ActionCategory
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec


def _ok(params, ctx):
    return SkillResult(ok=True)


def _registry() -> SkillRegistry:
    r = SkillRegistry()
    r.register_skill(
        SkillSpec(
            name="press.draft",
            fn=_ok,
            description="Render a document draft locally.",
            inputs={"source": "Path", "copies": "int"},
        )
    )
    r.register_skill(
        SkillSpec(
            name="press.publish",
            fn=_ok,
            description="Publish a document end-to-end.",
            inputs={"source": "Path"},
        )
    )
    r.register_skill(
        SkillSpec(
            name="scan.status",
            fn=_ok,
            description="Show signal counts.",
            side_effects=False,  # declared read-only → READ projection
        )
    )
    return r


# --- name mangling round-trip --------------------------------------------- #
def test_name_mangle_roundtrip():
    assert skill_to_tool_name("press.draft") == "press__draft"
    assert tool_to_skill_name("press__draft") == "press.draft"
    assert "." not in skill_to_tool_name("a.b.c")  # LLM-safe


# --- conversion ----------------------------------------------------------- #
def test_each_skill_becomes_a_tool():
    tools = skills_to_tool_definitions(_registry())
    names = {t.name for t in tools}
    assert names == {"press__draft", "press__publish", "scan__status"}


def test_undeclared_side_effects_defaults_write():
    tools = {t.name: t for t in skills_to_tool_definitions(_registry())}
    assert tools["press__publish"].description == "Publish a document end-to-end."
    # Undeclared side_effects → conservative WRITE → confirm-gated.
    assert tools["press__publish"].category is ActionCategory.WRITE


def test_inputs_become_json_schema():
    tools = {t.name: t for t in skills_to_tool_definitions(_registry())}
    schema = tools["press__draft"].parameters
    assert schema["type"] == "object"
    assert schema["properties"]["source"]["type"] == "string"  # Path → string
    assert schema["properties"]["copies"]["type"] == "integer"  # int → integer


def test_no_inputs_gives_empty_object_schema():
    tools = {t.name: t for t in skills_to_tool_definitions(_registry())}
    assert tools["scan__status"].parameters == {"type": "object", "properties": {}}


def test_namespace_filter():
    tools = skills_to_tool_definitions(_registry(), namespace="press")
    assert {t.name for t in tools} == {"press__draft", "press__publish"}


def test_category_read_from_capability_side_effects():
    # Approval comes from the capability's declared side_effects, not a
    # per-surface override (AEOS §4.9.3).
    tools = {t.name: t for t in skills_to_tool_definitions(_registry())}
    assert tools["scan__status"].category is ActionCategory.READ  # side_effects=False
    assert tools["press__publish"].category is ActionCategory.WRITE  # undeclared
