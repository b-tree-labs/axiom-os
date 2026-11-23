# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An agent and a skill shipped by a NON-builtin extension reach the roster and MCP.

Regression: ``discover_agents`` only scanned the builtins tree, and the MCP
surface only bound skills of ``<pkg>.extensions.builtins.<name>`` packages, so
an extension scaffolded by ``axi ext init`` (flat ``<pkg>/skills``) was listed
by ``axi ext list`` yet absent from the roster, ``agents.ask`` and MCP.
"""

from __future__ import annotations

import sys
import textwrap

import pytest

from axiom.extensions.builtins.connect import agent_router
from axiom.extensions.builtins.mcp.aggregation import AggregationRegistry
from axiom.extensions.discovery import parse_manifest


@pytest.fixture
def flat_ext(tmp_path, monkeypatch):
    root = tmp_path / "ext" / "zedpal"
    (root / "zedpal" / "agents" / "zedpal").mkdir(parents=True)
    (root / "zedpal" / "agents" / "zedpal" / "persona.md").write_text("# ZEDPAL\n")
    (root / "zedpal" / "skills").mkdir()
    (root / "zedpal" / "__init__.py").write_text("")
    (root / "zedpal" / "skills" / "__init__.py").write_text(textwrap.dedent('''
        from axiom.infra.skills import SkillResult, SkillSpec
        def hello(params, ctx):
            return SkillResult(ok=True, value={"m": "hi"})
        def bind(registry):
            registry.register_skill(SkillSpec(name="zedpal.hello", fn=hello,
                surfaces=("cli", "mcp", "agent_tool"), side_effects=False, idempotent=True))
        '''))
    (root / "axiom-extension.toml").write_text(textwrap.dedent('''
        [extension]
        name = "zedpal"
        version = "0.1.0"
        description = "t"
        owner = "x"
        [[extension.provides]]
        kind = "agent"
        name = "zedpal"
        persona = "zedpal/agents/zedpal/persona.md"
        [[extension.provides]]
        kind = "skill"
        name = "zedpal.hello"
        entry = "zedpal.skills:hello"
        description = "hi"
        '''))
    monkeypatch.syspath_prepend(str(root))
    yield root
    for m in [m for m in sys.modules if m.split(".")[0] == "zedpal"]:
        del sys.modules[m]


def test_roster_includes_extension_agent(flat_ext, monkeypatch):
    ext = parse_manifest(flat_ext / "axiom-extension.toml")
    monkeypatch.setattr("axiom.extensions.discovery.discover_extensions", lambda *a: [ext])
    roster = agent_router.discover_agents()
    assert "zedpal" in roster
    assert roster["zedpal"].persona_dir == (flat_ext / "zedpal/agents/zedpal").resolve()
    assert "tidy" in roster  # builtins still present


def test_mcp_surface_lists_extension_skill(flat_ext):
    ext = parse_manifest(flat_ext / "axiom-extension.toml")
    surface = AggregationRegistry(extensions=[ext]).build()
    assert "axiom_zedpal__hello" in {t.name for t in surface.tools}
