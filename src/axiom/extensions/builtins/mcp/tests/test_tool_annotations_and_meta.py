# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""AEOS 0.2 chain-readiness on the MCP wire: annotations and namespaced ``_meta``.

A real in-process MCP client (``mcp.Client`` against ``build_server``) lists the
tools and reads exactly what a non-Axiom harness would read:

* ``risk`` / ``idempotent`` project onto the MCP ``ToolAnnotations`` hints
  ``readOnlyHint``, ``destructiveHint`` and ``idempotentHint`` (wire names, from
  the installed library's ``ToolAnnotations`` aliases);
* ``requires`` / ``produces`` / ``next_steps`` / ``recipes`` ride in ``_meta``
  under the single reverse-DNS key ``ai.axiom/chain``, so a client that does not
  know it ignores it, and nothing is added at the top level of the tool.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.builtins.mcp.server import build_server
from axiom.extensions.builtins.mcp.skill_tools import META_KEY
from axiom.infra.skill_declarations import NextStep, Precondition, Ref
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult, SkillSpec

MCP = ("cli", "mcp", "agent_tool")


def _fn(p, c):
    return SkillResult(value={})


def _make(p, c):
    return SkillResult(value={"thing_id": "t1"})


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)


def _registry() -> SkillRegistry:
    reg = SkillRegistry()
    reg.register_skill(
        SkillSpec(  # undeclared: says nothing about risk
            name="t.old", fn=_fn, description="old", surfaces=MCP, inputs={}
        )
    )
    reg.register_skill(
        SkillSpec(  # read, idempotent
            name="t.look",
            fn=_fn,
            description="look",
            surfaces=MCP,
            side_effects=False,
            idempotent=True,
            risk="read",
            requires=(),
            inputs={},
        )
    )
    reg.register_skill(
        SkillSpec(
            name="t.make",
            fn=_make,
            description="make",
            surfaces=MCP,
            side_effects=True,
            idempotent=False,
            open_world=False,
            risk="reversible-write",
            inputs={"q": "str"},
            requires=(
                Precondition("go", "needs go", check=lambda p, c: bool(p.get("q")), fix="pass q"),
            ),
            produces=(Ref("thing_id", "pass as thing to t.drop"),),
            next_steps=(
                NextStep(
                    "t.drop", "produced:thing_id", "remove it again", feeds={"thing": "thing_id"}
                ),
            ),
            recipes=("tidy",),
        )
    )
    reg.register_skill(
        SkillSpec(
            name="t.drop",
            fn=_fn,
            description="drop",
            surfaces=MCP,
            side_effects=True,
            risk="destructive",
            inputs={"thing": "str"},
            requires=(),
        )
    )
    return reg


def _client_run(tmp_path, fn):
    reg = _registry()
    ctx = SkillContext(
        registry=reg, state_dir=tmp_path, logger=logging.getLogger("t"), user_prompt=None
    )
    surface = AggregationRegistry(extensions=[], registry=reg, ctx_factory=lambda: ctx).build()
    server = build_server(surface)

    async def go():
        from mcp import Client

        async with Client(server) as client:
            return await fn(client)

    return asyncio.run(go())


def _tools(tmp_path) -> dict:
    async def fn(client):
        listed = await client.list_tools()
        return {t.name: t.model_dump(by_alias=True, exclude_none=True) for t in listed.tools}

    return _client_run(tmp_path, fn)


def test_the_wire_names_are_the_ones_the_library_defines():
    from mcp.types import ToolAnnotations

    aliases = {f.alias for f in ToolAnnotations.model_fields.values()}
    assert {"readOnlyHint", "destructiveHint", "idempotentHint"} <= aliases


def test_risk_and_idempotent_project_onto_the_three_hints(tmp_path):
    tools = _tools(tmp_path)
    assert tools["axiom_t__look"]["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    }
    assert tools["axiom_t__make"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert tools["axiom_t__drop"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True}


def test_an_undeclared_risk_never_claims_it_is_harmless(tmp_path):
    ann = _tools(tmp_path)["axiom_t__old"]["annotations"]
    assert ann["readOnlyHint"] is False
    assert "destructiveHint" not in ann  # unset: the protocol default (conservative) applies
    assert "idempotentHint" not in ann


def test_chain_hints_ride_under_one_namespaced_meta_key(tmp_path):
    tools = _tools(tmp_path)
    assert META_KEY == "ai.axiom/chain"
    meta = tools["axiom_t__make"]["_meta"]
    assert set(meta) == {"ai.axiom/chain"}
    chain = meta["ai.axiom/chain"]
    assert chain["v"] == 1 and chain["risk"] == "reversible-write"
    assert chain["requires"] == [{"id": "go", "fix": "pass q"}]
    assert chain["produces"] == [{"kind": "thing_id", "how_to_use": "pass as thing to t.drop"}]
    assert chain["next"] == [
        {
            "operationId": "axiom_t__drop",
            "when": "produced:thing_id",
            "description": "remove it again",
            "parameters": {"thing": "$response.body#/value/thing_id"},
        }
    ]
    assert chain["recipes"] == ["tidy"]
    # nothing Axiom-specific leaks to the tool's top level
    assert set(tools["axiom_t__make"]) <= {
        "name",
        "description",
        "inputSchema",
        "outputSchema",
        "annotations",
        "_meta",
    }


def test_a_skill_declaring_nothing_has_no_meta_and_a_read_with_nothing_to_say_has_only_risk(
    tmp_path,
):
    tools = _tools(tmp_path)
    assert "_meta" not in tools["axiom_t__old"]
    assert tools["axiom_t__look"]["_meta"] == {"ai.axiom/chain": {"v": 1, "risk": "read"}}


def test_a_tool_that_produces_refs_declares_an_output_schema_with_typed_refs(tmp_path):
    tools = _tools(tmp_path)
    schema = tools["axiom_t__make"]["outputSchema"]
    ref = schema["properties"]["value"]["properties"]["thing_id"]
    assert ref["x-aeos-ref"] == "thing_id" and "t.drop" in ref["description"]
    assert "outputSchema" not in tools["axiom_t__look"]  # nothing produced, nothing declared


def test_success_returns_structured_content_and_mirrors_the_chain_id_in_result_meta(tmp_path):
    async def fn(client):
        return await client.call_tool("axiom_t__make", {"q": "a", "_chain": {"chain_id": "c-7"}})

    res = _client_run(tmp_path, fn)
    assert not res.is_error
    assert res.structured_content["value"] == {"thing_id": "t1"}
    assert res.structured_content["chain"]["produced"][0]["kind"] == "thing_id"
    assert res.meta["ai.axiom/chain"]["chain_id"] == "c-7" and res.meta["ai.axiom/chain"]["step_id"]
    assert "traceparent" not in res.meta  # a chain id is not a trace id


def test_an_unmet_requirement_is_a_tool_error_not_a_protocol_error_and_carries_the_fix(tmp_path):
    async def fn(client):
        return await client.call_tool("axiom_t__make", {})

    res = _client_run(tmp_path, fn)
    assert res.is_error is True
    assert res.structured_content["chain"]["unmet"] == [
        {"id": "go", "why": "needs go", "fix": "pass q"}
    ]


def test_a_link_in_the_live_result_carries_the_prefilled_pointer_and_the_value(tmp_path):
    async def fn(client):
        return await client.call_tool("axiom_t__make", {"q": "a"})

    res = _client_run(tmp_path, fn)
    nxt = res.structured_content["chain"]["next"][0]
    assert nxt["operationId"] == "axiom_t__drop"
    assert nxt["parameters"] == {"thing": "$response.body#/value/thing_id"}
    assert nxt["with"] == {"thing": "t1"}
