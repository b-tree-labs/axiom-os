# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A capability is offered to a model only where it declared that surface.

``SkillSpec.surfaces`` is the bounded-exposure guard of AEOS §4.9.4, and
until now nothing consulted it anywhere — not chat, not MCP, despite the
field's own docstring describing the MCP behaviour. It was documentation.

What that cost is concrete. ``chat.tool_namespaces`` is namespace-coarse, so
naming ``data`` handed the model all 39 of the data platform's capabilities.
Twelve of them had declared ``surfaces=("cli", "mcp", "agent_tool")`` and
``side_effects=False`` — the introspection and query verbs, the ones that
answer a question with a table. The other 27 had declared nothing, and were
offered anyway. An operator turning on the namespace they needed for tables
got twenty-seven verbs nobody meant to offer, and a tool list long enough to
degrade every answer in the conversation.

Undeclared is withheld, not exposed. "Has not said" is not "yes", which is
the reading ``is_read_only`` already gives an undeclared ``side_effects``.
The ratchet runs the right way: a capability that wants to be reachable from
a conversation says so.
"""

from __future__ import annotations

from axiom.extensions.builtins.chat.skill_tools import (
    AGENT_SURFACE,
    skills_to_tool_definitions,
)
from axiom.infra.capability_projection import exposed_on
from axiom.infra.skills import SkillRegistry, SkillResult, SkillSpec


def _ok(params, ctx):
    return SkillResult(ok=True)


def _spec(name, surfaces=None, **over):
    return SkillSpec(name=name, fn=_ok, description=f"the {name} capability",
                     surfaces=surfaces, **over)


def _registry(*specs):
    r = SkillRegistry()
    for s in specs:
        r.register_skill(s)
    return r


# --- the predicate ---------------------------------------------------------


def test_a_capability_that_declared_the_surface_is_exposed():
    assert exposed_on(_spec("a.b", ("cli", "mcp", "agent_tool")), AGENT_SURFACE) is True


def test_a_capability_that_declared_other_surfaces_is_not():
    assert exposed_on(_spec("a.b", ("cli", "mcp")), AGENT_SURFACE) is False


def test_a_capability_that_declared_nothing_is_not():
    """Has not said is not yes — the reading is_read_only already gives."""
    assert exposed_on(_spec("a.b", None), AGENT_SURFACE) is False


def test_an_empty_declaration_is_not_a_declaration():
    assert exposed_on(_spec("a.b", ()), AGENT_SURFACE) is False


# --- the projection --------------------------------------------------------


def test_only_the_declared_capabilities_become_tools():
    r = _registry(
        _spec("data.catalog", ("cli", "mcp", "agent_tool"), side_effects=False),
        _spec("data.series", ("cli", "mcp", "agent_tool"), side_effects=False),
        _spec("data.install"),          # declared nothing
        _spec("data.unregister"),       # declared nothing
    )
    names = {t.name for t in skills_to_tool_definitions(r, namespace="data")}

    assert names == {"data__catalog", "data__series"}


def test_the_shape_of_the_real_gap():
    """Twelve declared out of thirty-nine is what `data` actually looks like."""
    declared = [_spec(f"data.read{i}", ("cli", "mcp", "agent_tool"), side_effects=False)
                for i in range(12)]
    silent = [_spec(f"data.other{i}") for i in range(27)]
    r = _registry(*declared, *silent)

    assert len(skills_to_tool_definitions(r, namespace="data")) == 12


def test_a_namespace_where_nothing_declared_offers_nothing():
    """Better than offering everything: configuring a namespace whose verbs
    have not opted in is a no-op, not a surprise."""
    r = _registry(_spec("legacy.one"), _spec("legacy.two"))

    assert skills_to_tool_definitions(r, namespace="legacy") == []


def test_withholding_is_logged_so_an_empty_tool_list_is_explainable(caplog):
    r = _registry(_spec("legacy.one"))
    with caplog.at_level("DEBUG", logger="axiom.extensions.builtins.chat.skill_tools"):
        skills_to_tool_definitions(r, namespace="legacy")

    assert "legacy.one" in caplog.text
    assert "did not declare" in caplog.text


def test_the_filter_applies_without_a_namespace_too():
    r = _registry(
        _spec("press.draft", ("agent_tool",)),
        _spec("press.publish"),
    )
    assert {t.name for t in skills_to_tool_definitions(r)} == {"press__draft"}
