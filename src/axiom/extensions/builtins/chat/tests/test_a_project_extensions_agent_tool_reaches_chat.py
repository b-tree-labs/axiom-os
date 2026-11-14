# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A project extension's ``agent_tool`` skill is offered in chat (#1672).

With ``chat.tool_namespaces`` naming it, a skill an extension declared for
``agent_tool`` did not reach chat: chat read the process registry and nothing
filled it from discovered extensions, while the MCP server binds each one
itself. Chat now binds discovered extensions the same way the MCP server does,
including the flat ``<pkg>/skills`` layout ``axi ext init`` scaffolds.
"""

from __future__ import annotations

import textwrap
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import tools as chat_tools
from axiom.infra import skills as infra_skills

PKG = "greeter_pkg_r1672"
NS = "greeter_r1672"


@pytest.fixture
def project_extension(tmp_path, monkeypatch):
    root = tmp_path / PKG
    (root / "skills").mkdir(parents=True)
    (root / "__init__.py").write_text("")
    (root / "skills" / "impl.py").write_text(textwrap.dedent('''
        from axiom.infra.skills import SkillResult

        def greet(params, ctx):
            return SkillResult(ok=True, value={"greeting": "hello"})

        def hidden(params, ctx):
            return SkillResult(ok=True, value={})
    '''))
    (root / "skills" / "__init__.py").write_text(textwrap.dedent(f'''
        from axiom.infra.skills import SkillSpec
        from .impl import greet, hidden

        def bind(registry):
            registry.register_skill(SkillSpec(name="{NS}.greet", fn=greet, description="say hello",
                                              side_effects=False,
                                              surfaces=("cli", "mcp", "agent_tool")), mutating=False)
            registry.register_skill(SkillSpec(name="{NS}.hidden", fn=hidden, description="cli only",
                                              side_effects=False, surfaces=("cli",)), mutating=False)
    '''))
    manifest = root / "axiom-extension.toml"
    manifest.write_text(textwrap.dedent(f'''
        [extension]
        name = "{NS}"
        version = "0.1.0"

        [[extension.provides]]
        kind = "skill"
        name = "{NS}.greet"
        entry = "{PKG}.skills.impl:greet"
    '''))
    monkeypatch.syspath_prepend(str(tmp_path))
    ext = SimpleNamespace(name=NS, enabled=True, root=root, manifest_path=manifest)
    monkeypatch.setattr("axiom.extensions.discovery.discover_extensions", lambda *a, **k: [ext])
    registry = infra_skills.SkillRegistry()
    monkeypatch.setattr(infra_skills, "default_registry", lambda: registry)
    monkeypatch.setattr(chat_tools, "_skill_namespaces", lambda: [NS])
    monkeypatch.setattr(chat_tools, "_BOUND_REGISTRIES", set(), raising=False)
    return registry


def test_a_project_extensions_agent_tool_is_offered_in_chat(project_extension):
    offered = set(chat_tools._scan_skill_tools())
    assert f"{NS}__greet" in offered


def test_a_skill_that_did_not_declare_agent_tool_is_still_withheld(project_extension):
    offered = set(chat_tools._scan_skill_tools())
    assert f"{NS}__hidden" not in offered


def test_binding_twice_is_harmless(project_extension):
    chat_tools._scan_skill_tools()
    assert f"{NS}__greet" in set(chat_tools._scan_skill_tools())
