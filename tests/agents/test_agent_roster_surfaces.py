# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A person who names an agent should not have to explain who it is.

"Does RIVET use KEEP for expiring keys?" only works if the assistant already
knows that RIVET is the release engineer, KEEP the secret steward, and which
tools each one owns. That knowledge has to arrive on every surface a harness
reads — the MCP handshake, every tool description, the roster tool and
resource, and the instruction-file write-back for when the server is down —
and all of them have to come from ONE declaration, or they drift.
"""

from __future__ import annotations

import asyncio
import json
import logging
import warnings
from pathlib import Path

import pytest

#: Claude Code cuts the MCP ``instructions`` string at 2,048 characters (the
#: live handshake on 2026-10-28 ended "… before quoting figu… [truncated]",
#: which is character 2,048). Anything a harness must know about the agents has
#: to sit before that.
HOST_TRUNCATION_CHARS = 2048


def _builtin_roster():
    from axiom.agents.roster import load_roster

    return load_roster(builtins_only=True)


class TestOneDeclaration:
    def test_every_addressable_agent_has_a_role_domain_and_owner(self):
        from axiom.extensions.builtins.connect.agent_router import discover_agents

        records = {r.handle: r for r in _builtin_roster()}
        handles = set(discover_agents(root=Path(__file__).resolve().parents[2] / "src/axiom/extensions/builtins"))
        assert handles <= set(records), f"agents with no roster record: {sorted(handles - set(records))}"
        for r in records.values():
            assert r.role.strip(), f"{r.handle}: no role"
            assert r.domain.strip(), f"{r.handle}: no domain"
            assert r.extension, f"{r.handle}: no owning extension"
            assert r.asks, f"{r.handle}: no example asks"

    def test_the_owner_named_agents_resolve_to_their_extensions(self):
        records = {r.handle: r for r in _builtin_roster()}
        assert records["keep"].extension == "vault"
        assert "secrets" in records["keep"].namespaces
        assert records["rivet"].extension == "release"
        assert "release" in records["rivet"].namespaces

    def test_internal_agent_names_are_derived_from_the_roster(self):
        from axiom.extensions.builtins.chat.identity import INTERNAL_AGENT_NAMES

        displays = {r.display.upper() for r in _builtin_roster()}
        assert displays <= set(INTERNAL_AGENT_NAMES)

    def test_a_delegated_agent_gets_the_tools_of_the_namespaces_it_owns(self):
        """KEEP's skills are `vault.*`; a delegation scoped to `keep.*` alone
        handed KEEP an empty toolset."""
        from axiom.extensions.builtins.connect.agent_router import discover_agents

        spec = discover_agents()["keep"]
        assert "vault" in spec.namespaces and "keep" in spec.namespaces


class TestTheHandshake:
    def test_every_handle_and_its_domain_sits_before_the_host_cut(self):
        from axiom.extensions.builtins.mcp.server import SERVER_INSTRUCTIONS

        head = SERVER_INSTRUCTIONS[:HOST_TRUNCATION_CHARS]
        for r in _builtin_roster():
            assert r.display in head, f"{r.display} is missing or past the host cut"
            assert r.domain in head, f"{r.display}'s domain is missing or past the host cut"

    def test_both_routing_rules_sit_before_the_host_cut(self):
        from axiom.extensions.builtins.mcp.server import SERVER_INSTRUCTIONS

        head = SERVER_INSTRUCTIONS[:HOST_TRUNCATION_CHARS]
        assert "axiom_agents__address" in head and "VERBATIM" in head
        # Named in prose, without `@`: consult, not address.
        assert "axiom_agents__ask" in head
        assert "without `@`" in head
        # The injection guard must not be the part that gets cut.
        assert "never an address" in head

    def test_the_built_server_carries_the_roster(self):
        from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
        from axiom.extensions.builtins.mcp.server import build_server

        srv = build_server(AggregationRegistry(extensions=[]).build())
        instructions = getattr(srv, "instructions", None) or getattr(
            getattr(srv, "_init_options", None), "instructions", None
        )
        for r in _builtin_roster():
            assert r.display in instructions


@pytest.fixture(scope="module")
def node_surface():
    from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return AggregationRegistry.from_node().build()


class TestEveryToolNamesItsOwner:
    def test_every_tool_description_carries_an_owner_tag(self, node_surface):
        from axiom.agents.roster import PLATFORM_TAG

        tags = {f"[{r.display}]" for r in _builtin_roster()} | {PLATFORM_TAG}
        untagged = [
            t.name for t in node_surface.tools
            if not any((t.description or "").startswith(tag + " ") for tag in tags)
        ]
        assert not untagged, f"tools with no owner tag: {untagged}"

    @pytest.mark.parametrize(
        ("tool", "tag"),
        [
            ("axiom_vault__resolve", "[KEEP]"),
            ("axiom_secrets__rotate", "[KEEP]"),
            ("axiom_release__plan", "[RIVET]"),
            ("axiom_hygiene__status", "[TIDY]"),
            ("axiom_agents__ask", "[platform]"),
        ],
    )
    def test_the_owner_named_tools_carry_their_agent(self, node_surface, tool, tag):
        by_name = {t.name: t for t in node_surface.tools}
        if tool not in by_name:
            pytest.skip(f"{tool} is not on this install's surface")
        assert by_name[tool].description.startswith(tag + " ")

    def test_an_owning_extensions_skills_are_all_declared_namespaces(self):
        """A skill namespace an agent's extension binds but the agent does not
        declare would be tagged by extension yet missing from the agent's
        delegated toolset. Catch the drift at the declaration."""
        import importlib

        from axiom.infra.skills import SkillRegistry

        for r in _builtin_roster():
            for ext in r.extensions:
                reg = SkillRegistry()
                try:
                    importlib.import_module(f"axiom.extensions.builtins.{ext}.skills").bind(reg)
                except (ImportError, AttributeError):
                    continue
                bound = {name.split(".", 1)[0] for name in reg.specs()}
                assert bound <= set(r.namespaces), (
                    f"{r.display}: extension {ext!r} binds {sorted(bound - set(r.namespaces))} "
                    "but the agent does not declare them in `namespaces`"
                )


class TestRosterToolAndResource:
    def test_the_roster_skill_returns_full_records(self, tmp_path):
        from axiom.extensions.builtins.agents.skills import bind
        from axiom.infra.skills import SkillContext, SkillRegistry

        registry = SkillRegistry()
        bind(registry)
        ctx = SkillContext(registry=registry, state_dir=tmp_path, logger=logging.getLogger("t"))
        out = registry.invoke("agents.roster", {}, ctx)
        assert out.ok and "rivet" in out.value["agents"]
        recs = {r["handle"]: r for r in out.value["records"]}
        keep = recs["keep"]
        assert keep["role"] and keep["domain"] and keep["extension"] == "vault"
        assert any(t.startswith("axiom_vault__") for t in keep["tools"])

    def test_the_roster_is_an_mcp_resource(self, node_surface):
        from axiom.agents.roster import ROSTER_RESOURCE_URI
        from axiom.extensions.builtins.mcp.server import read_resource

        assert ROSTER_RESOURCE_URI in {str(r.uri) for r in node_surface.resources}
        text = asyncio.run(read_resource(node_surface, ROSTER_RESOURCE_URI))
        recs = {r["handle"]: r for r in json.loads(text)["records"]}
        assert recs["rivet"]["domain"]

    def test_axi_agents_roster_is_a_cli_verb(self, capsys):
        from axiom.extensions.builtins.agents.cli import main

        assert main(["roster"]) == 0
        out = capsys.readouterr().out
        assert "KEEP" in out and "secret steward" in out


class TestTheInstructionFileFallback:
    def test_the_write_back_block_names_every_agent(self):
        from axiom.memory.rendering import EpochSnapshot, PreambleEntry, render_agents_md_block

        snap = EpochSnapshot(session_id="s", epoch=1, entries=(PreambleEntry("f1", "x"),))
        block = render_agents_md_block(snap)
        assert "Axiom agents" in block
        for r in _builtin_roster():
            assert r.display in block

    def test_the_write_back_block_is_byte_stable_and_small(self):
        from axiom.memory.rendering import EpochSnapshot, PreambleEntry, render_agents_md_block

        snap = EpochSnapshot(session_id="s", epoch=1, entries=(PreambleEntry("f1", "x"),))
        a, b = render_agents_md_block(snap), render_agents_md_block(snap)
        assert a == b
        assert len(a) < 2500
