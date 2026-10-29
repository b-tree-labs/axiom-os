# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLERK — the scheduled coordinator agent (ADR-161, prd-program R3).

The agent is declared the way every daemon agent is, so the existing agent
service runner can schedule it with no new mechanism: a ``kind = "agent"``
provide-block with a persona, and an ``[agent]`` lifecycle block whose
``heartbeat_command`` the runner fires as ``axi program sync`` on a timer.
These tests pin that the declaration is correct and genuinely schedulable,
and that it does not force autonomy on — firing stays two operator decisions
away (the master ``autonomy.enabled`` toggle and per-agent consent).
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from axiom.extensions import contracts
from axiom.extensions.builtins.program import cli

_MANIFEST = Path(__file__).parent.parent / "axiom-extension.toml"
_EXT_ROOT = _MANIFEST.parent


def _raw() -> dict:
    return tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))


class TestTheAgentProvideBlock:
    def test_an_agent_named_clerk_is_declared(self):
        agents = [p for p in _raw()["extension"]["provides"] if p["kind"] == "agent"]
        assert len(agents) == 1
        clerk = agents[0]
        assert clerk["name"] == "CLERK"
        assert clerk["entry"] == "axiom.extensions.builtins.program.cli:main"

    def test_the_declared_persona_file_exists(self):
        """A declared persona that does not resolve fails ``axi ext lint``
        (AEOS060); keep it from regressing."""
        (clerk,) = [p for p in _raw()["extension"]["provides"] if p["kind"] == "agent"]
        persona = clerk.get("persona")
        assert persona, "CLERK must declare a persona"
        assert (_EXT_ROOT / persona).exists(), persona

    def test_uses_skills_resolve_to_declared_skills(self):
        raw = _raw()
        (clerk,) = [p for p in raw["extension"]["provides"] if p["kind"] == "agent"]
        declared = {p.get("name") for p in raw["extension"]["provides"] if p["kind"] == "skill"}
        for used in clerk.get("uses_skills", []):
            assert used in declared, used


class TestTheDaemonLifecycle:
    def test_the_agent_is_genuinely_schedulable(self):
        ext = contracts.parse_manifest(_MANIFEST)
        agent = ext.agent
        assert agent is not None
        # startup=daemon + a non-empty heartbeat_command is what the runner
        # needs to register and fire it.
        assert agent.startup == "daemon"
        assert agent.is_always_on is True
        assert agent.is_registrable is True
        assert agent.heartbeat_command == "program sync"

    def test_the_heartbeat_interval_is_sane(self):
        # The repo-wide heartbeat guard requires >= 60s for a daemon agent.
        assert contracts.parse_manifest(_MANIFEST).agent.heartbeat_interval >= 60

    def test_the_heartbeat_command_is_a_real_program_subcommand(self):
        # `axi <heartbeat_command>` must parse — the runner invokes exactly it.
        noun, _, verb = contracts.parse_manifest(_MANIFEST).agent.heartbeat_command.partition(" ")
        assert noun == "program"
        args = cli._parser().parse_args([verb])
        assert args.verb == "sync"


class TestAutonomyIsNotForcedOn:
    def test_default_consent_is_opt_in(self):
        """Opt-in keeps CLERK out of the auto-enabled core set, so declaring
        it does not make it fire — an operator must select it."""
        assert _raw()["agent"]["default_consent"] == "opt-in"

    def test_the_sender_nameplate_is_declared(self):
        assert _raw()["sender"]["display_name"] == "CLERK"
