# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""C1 — address any agent/subagent by name over a channel."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.connect.agent_router import (
    discover_agents,
    parse_addressee,
    resolve_agent,
    suggest,
)


def test_discovers_builtin_agents_by_persona_convention():
    agents = discover_agents()
    # the personas that exist in this repo
    for name in ("axi", "tidy", "triage"):
        assert name in agents, f"missing {name}"
    assert agents["axi"].display == "Axi"
    assert agents["tidy"].display == "TIDY"
    assert agents["tidy"].namespace == "tidy"


_KNOWN = {"axi", "tidy", "triage"}


@pytest.mark.parametrize("text,name,rest", [
    ("TIDY: prune the journal", "tidy", "prune the journal"),
    ("tidy prune the journal", "tidy", "prune the journal"),
    ("Axi, how are you?", "axi", "how are you?"),
    ("@TRIAGE what's degraded?", "triage", "what's degraded?"),
    ("what is the status of things", None, "what is the status of things"),
    ("ZORP: do a thing", None, "ZORP: do a thing"),  # unknown → default
])
def test_parse_addressee(text, name, rest):
    got_name, got_rest = parse_addressee(text, _KNOWN)
    assert got_name == name
    assert got_rest == rest


def test_resolve_default_is_axi_with_persona():
    r = resolve_agent(None)
    assert r.spec.name == "axi"
    assert r.persona  # axi/persona.md is non-empty
    assert r.tools == []  # no registry → no tools at C1


def test_resolve_named_agent_loads_its_persona():
    r = resolve_agent("tidy")
    assert r.spec.display == "TIDY" and r.persona


def test_unknown_agent_raises_and_suggests():
    with pytest.raises(KeyError):
        resolve_agent("tidey")
    assert "tidy" in suggest("tidey", _KNOWN)


def test_resolve_scopes_tools_to_namespace_when_registry_given():
    calls = {}

    class _FakeRegistry:
        pass

    # patch the projection to capture the namespace it's scoped to
    import axiom.extensions.builtins.chat.skill_tools as st
    orig = st.skills_to_tool_definitions
    st.skills_to_tool_definitions = lambda reg, *, namespace=None: (calls.__setitem__("ns", namespace), ["t"])[1]
    try:
        r = resolve_agent("tidy", registry=_FakeRegistry())
    finally:
        st.skills_to_tool_definitions = orig
    assert calls["ns"] == "tidy" and r.tools == ["t"]
