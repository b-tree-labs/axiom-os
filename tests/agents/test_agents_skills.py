# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The delegation spine is reachable by a consuming harness, not just by chat.

`axi agents` declared `# mcp: not-applicable — no agent-invocable
capabilities`. That was true while the verbs were start/stop/status. It stopped
being true the moment delegation worked, and a manifest that still said it
would have kept the whole feature invisible to every MCP consumer.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from axiom.extensions.builtins.agents.skills import ask, roster  # package re-export

_MANIFEST = (
    Path(__file__).parents[2]
    / "src/axiom/extensions/builtins/agents/axiom-extension.toml"
)


def _provides():
    return tomllib.loads(_MANIFEST.read_text(encoding="utf-8"))["extension"]["provides"]


class TestItIsDeclared:
    def test_ask_and_roster_are_declared_skills(self):
        names = {p["name"] for p in _provides() if p["kind"] == "skill"}
        assert {"ask", "roster"} <= names

    def test_every_declared_entry_point_imports_and_is_callable(self):
        """A declared entry that does not resolve is a capability the registry
        advertises and cannot run."""
        import importlib

        for p in _provides():
            if p["kind"] != "skill":
                continue
            module, _, func = p["entry"].partition(":")
            assert callable(getattr(importlib.import_module(module), func))

    def test_the_manifest_no_longer_claims_mcp_is_not_applicable(self):
        """The comment was a true statement that this feature falsified."""
        head = _MANIFEST.read_text(encoding="utf-8")[:600]
        assert "mcp: not-applicable" not in head


class TestItWorks:
    def test_roster_lists_addressable_agents(self):
        out = roster()
        assert "rivet" in out["agents"] and "tidy" in out["agents"]

    def test_ask_refuses_an_unknown_agent_without_running(self):
        out = ask(agent="rivett", request="why is CI red")
        assert out["ok"] is False
        assert "rivet" in out["did_you_mean"]

    def test_ask_refuses_an_empty_request(self):
        out = ask(agent="rivet", request="")
        assert out["ok"] is False
        assert "empty" in out["error"]


def test_the_skills_package_does_not_shadow_a_module():
    """`skills.py` beside a `skills/` directory resolves by an implementation
    detail — a regular module beats a namespace package until someone adds an
    `__init__.py`, and then the declared entry points silently move. The
    persona-less extensions (analytics, authz, attest) all use a `skills/`
    PACKAGE, so this one does too and the ambiguity cannot arise."""
    from pathlib import Path

    pkg = Path(__file__).parents[2] / "src/axiom/extensions/builtins/agents"
    assert (pkg / "skills" / "__init__.py").is_file()
    assert not (pkg / "skills.py").exists(), "a module beside the package reintroduces the shadow"
