# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Agentic drive: the read forms of chat commands as skill functions, projected as ``axiom_chat__<verb>``.

The registry declares what is exposed (``Command.expose`` / ``risk`` / ``mcp``); the skills package
registers it; the composed MCP server projects it. Destructive forms are never exposed. The MCP name-collision
rules (spec-builtin-mcp-server 9.1) live on branch ``test/mcp-harness-collisions``, which is not importable
from here, so the rules that apply to these names are replicated below from that branch's
``harness_collisions.py`` and ``harness_profiles.toml`` (commit 12395181) and say so.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import skills as chat_skills
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import (
    DEFAULT_EXPOSE,
    REGISTRY,
    Command,
    CommandContext,
    McpForm,
)
from axiom.extensions.builtins.chat.skills import read
from axiom.extensions.builtins.mcp.identity_gate import DEFAULT_PATTERNS, refusal_reason
from axiom.extensions.builtins.mcp.skill_tools import mcp_tool_name, skill_tool_contribution
from axiom.infra.skills import SkillContext, SkillRegistry

from ._tools_cmd_helpers import make_agent


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from axiom.extensions import discovery
    from axiom.extensions.builtins.chat import tools

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setattr(tools, "_manifest_roots", lambda: [])
    monkeypatch.setattr(discovery, "discover_and_load_chat_tools", lambda *a, **k: [])


def _exposed_commands():
    ensure_loaded()
    return [c for c in REGISTRY.commands(include_hidden=True) if "mcp" in c.expose]


# ---- the declaration ----------------------------------------------------------------------------------


def test_default_exposure_is_chat_and_cli_only():
    c = Command("/x", "x", "Inspect", lambda ctx, a: "")
    assert c.expose == DEFAULT_EXPOSE == frozenset({"chat", "cli"})


def test_risk_defaults_from_whether_the_command_mutates():
    assert Command("/x", "x", "g", lambda c, a: "").risk == "read"
    assert Command("/y", "y", "g", lambda c, a: "", mutates=True).risk == "reversible-write"
    assert Command("/z", "z", "g", lambda c, a: "", mutates=True, risk="destructive").risk == "destructive"


def test_a_destructive_form_can_never_be_declared_as_an_mcp_form():
    with pytest.raises(ValueError, match="destructive"):
        Command(
            "/x", "x", "g", lambda c, a: "", expose=frozenset({"mcp"}),
            mcp=(McpForm("chat.wipe", "/x --force", risk="destructive"),),
        )


def test_mcp_exposure_without_named_forms_is_refused():
    with pytest.raises(ValueError, match="names the forms"):
        Command("/x", "x", "g", lambda c, a: "", expose=frozenset({"mcp"}))


def test_unknown_exposure_and_unknown_risk_are_refused():
    with pytest.raises(ValueError, match="unknown exposure"):
        Command("/x", "x", "g", lambda c, a: "", expose=frozenset({"telepathy"}))
    with pytest.raises(ValueError, match="risk"):
        Command("/x", "x", "g", lambda c, a: "", risk="spicy")


def test_the_destructive_commands_have_no_mcp_form():
    ensure_loaded()
    for name in ("/undo", "/memory"):
        c = REGISTRY.get(name)
        assert c.risk == "destructive"
    assert "mcp" not in REGISTRY.get("/undo").expose and not REGISTRY.get("/undo").mcp
    # /memory is destructive only through `forget`; its MCP forms are the three reads
    forms = {f.form for f in REGISTRY.get("/memory").mcp}
    assert forms == {"/memory search", "/memory show", "/memory recent"}
    assert all(f.risk in ("read", "reversible-write") for c in _exposed_commands() for f in c.mcp)


def test_nothing_that_writes_or_forgets_is_registered_as_a_skill():
    names = {s.name for s in chat_skills.SPECS}
    assert not {n for n in names if re.search(r"forget|undo|restore|prune|stop|panic|add|apply", n)}
    assert all(s.side_effects is False and s.surfaces == ("mcp",) for s in chat_skills.SPECS)


# ---- registry <-> skills parity -------------------------------------------------------------------------


def test_every_declared_mcp_form_has_a_skill_and_every_skill_has_a_declaration():
    declared = {f.skill for c in _exposed_commands() for f in c.mcp}
    assert declared == set(read.FUNCTIONS) == {s.name for s in chat_skills.SPECS}


def test_the_admin_set_matches_the_commands_that_require_admin():
    ensure_loaded()
    admin_skills = {f.skill for c in _exposed_commands() for f in c.mcp if f.admin or c.requires == "admin"}
    assert admin_skills == set(chat_skills.ADMIN_ONLY)


def test_every_manifest_skill_entry_points_at_a_real_function():
    import tomllib
    from importlib import import_module
    from pathlib import Path

    manifest = Path(chat_skills.__file__).parent.parent / "axiom-extension.toml"
    provides = tomllib.loads(manifest.read_text())["extension"]["provides"]
    declared = {p["name"]: p["entry"] for p in provides if p["kind"] == "skill"}
    assert set(declared) == {s.name for s in chat_skills.SPECS}
    for name, entry in declared.items():
        mod, _, fn = entry.partition(":")
        assert getattr(import_module(mod), fn) is read.FUNCTIONS[name]


# ---- the slash handler and the MCP tool call the same function ---------------------------------------------


@pytest.mark.parametrize(
    "module,fn,line,skill,params",
    [
        ("tools_cmd", "tools_data", "/tools --json", "chat.tools", {}),
        ("stats", "stats_data", "/stats --json", "chat.stats", {}),
        ("where_cmd", "where_data", "/where --json", "chat.where", {}),
    ],
)
def test_slash_json_and_the_skill_end_in_the_same_data_function(monkeypatch, module, fn, line, skill, params):
    import importlib

    mod = importlib.import_module(f"axiom.extensions.builtins.chat.commands_ext.{module}")
    calls = []

    def spy(*args, **kw):
        calls.append(args)
        return {"spy": True, "follow_ups": []}

    monkeypatch.setattr(mod, fn, spy)
    ensure_loaded()
    slash = REGISTRY.dispatch(line, CommandContext(agent=make_agent(), store=None, surface="repl"))
    via_skill = read.FUNCTIONS[skill](params, _ctx())
    assert len(calls) == 2, "the slash handler and the skill must both call the one data function"
    assert json.loads(slash)["spy"] is True and via_skill.value["spy"] is True


def test_skills_and_agents_data_are_identical_through_both_doors():
    ensure_loaded()
    slash = json.loads(REGISTRY.dispatch("/skills --json", CommandContext(agent=make_agent(), store=None)))
    via = read.FUNCTIONS["chat.skills"]({}, _ctx()).value
    assert slash["rows"] == via["rows"] and slash["total"] == via["total"]


def _ctx():
    return SkillContext(registry=SkillRegistry(), state_dir=__import__("pathlib").Path("."), logger=__import__("logging").getLogger("t"))


# ---- the MCP projection -----------------------------------------------------------------------------------


def _contribution():
    registry = SkillRegistry()
    chat_skills.bind(registry)
    return skill_tool_contribution(registry, ctx_factory=_ctx)


def test_the_composed_server_projects_each_skill_as_axiom_chat_verb():
    tools = {t.name: t for t in _contribution().tools}
    assert set(tools) == {
        "axiom_chat__tools", "axiom_chat__stats", "axiom_chat__where", "axiom_chat__sources",
        "axiom_chat__skills", "axiom_chat__agents", "axiom_chat__mcp", "axiom_chat__memory_search",
        "axiom_chat__memory_show", "axiom_chat__memory_recent",
    }
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert mcp_tool_name("chat.tools") == "axiom_chat__tools"


# Reserved harness commands, copied from harness_profiles.toml (test/mcp-harness-collisions @ 12395181):
# the verified / from-binary / documented names of Claude Code, OpenCode, Codex, Gemini and Cursor.
_RESERVED = {
    "agents", "mcp", "usage", "context", "init", "model", "compact", "clear", "resume", "help", "cost",
    "status", "plan", "memory", "permissions", "review", "export", "hooks", "rewind", "undo", "redo", "fork",
    "share", "sessions", "models", "skills", "mcps", "tools", "stats", "restore", "settings", "diff",
    "approvals", "new", "exit", "quit", "chat", "extensions", "compress",
}
_SESSION_OWNING = {"new", "resume", "clear", "compact", "exit", "sessions", "model"}
_NAMESPACED = re.compile(r"^axiom_(?=[a-z0-9_]*[a-z0-9]_+[a-z0-9])[a-z0-9_]+$")


def _verbs(name: str) -> list[str]:
    core = name.split("__")[-1]
    return sorted({core, core.rsplit("_", 1)[-1]})


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _disambiguates(description: str, reserved: str) -> bool:
    for clause in re.split(r"[.;\n]", description.lower()):
        if re.search(r"\b(not|isn't|unlike|rather than|instead of)\b", clause) and (
            re.search(rf"(?<![a-z0-9_]){re.escape(reserved)}(?![a-z0-9_])", clause)
            or re.search(r"\b(harness|client|slash)", clause)
        ):
            return True
    return False


def collisions(tools) -> list[str]:
    found = []
    for t in tools:
        if not _NAMESPACED.match(t.name):
            found.append(f"bare-verb {t.name}")
        for v in _verbs(t.name):
            if v in _SESSION_OWNING:
                found.append(f"session-owning {t.name}")
            for r in _RESERVED:
                near = v == r or (len(v) >= 4 and len(r) >= 4 and _edit_distance(v, r) <= 1)
                if near and not _disambiguates(t.description or "", r):
                    found.append(f"verb-shadow {t.name} ~ /{r}")
    return found


def test_published_names_follow_the_harness_collision_rules():
    assert collisions(_contribution().tools) == []


def test_the_collision_check_can_fail():
    bare = SimpleNamespace(name="tools", description="x")
    owning = SimpleNamespace(name="axiom_chat__model", description="Switch. Not the harness's own /model.")
    shadow = SimpleNamespace(name="axiom_chat__tools", description="Lists tools.")
    assert any("bare-verb" in f for f in collisions([bare]))
    assert any("session-owning" in f for f in collisions([owning]))
    assert any("verb-shadow" in f for f in collisions([shadow]))


# ---- admin-only commands and the identity gate (ADR-114) ---------------------------------------------------


def test_an_admin_only_skill_is_unreachable_when_the_gate_resolves_no_admin(monkeypatch):
    from axiom.extensions.builtins.chat import roles

    monkeypatch.setattr(chat_skills, "ADMIN_ONLY", frozenset({"chat.tools"}))
    monkeypatch.setattr(roles, "_configured_admins", lambda: [])
    registry = SkillRegistry()
    chat_skills.bind(registry)
    contrib = skill_tool_contribution(registry, ctx_factory=_ctx)
    patterns = contrib.allowed_principals["axiom_chat__tools"]
    assert patterns == chat_skills.NO_ADMIN_PATTERN
    for who in ("@ben:local", "@root:local", "@anyone:anywhere"):
        assert refusal_reason("axiom_chat__tools", who, patterns), f"{who} must be refused"
    # a non-admin tool of the same server is untouched
    assert contrib.allowed_principals["axiom_chat__where"] == DEFAULT_PATTERNS


def test_an_admin_only_skill_admits_exactly_the_configured_admin(monkeypatch):
    from axiom.extensions.builtins.chat import roles

    monkeypatch.setattr(chat_skills, "ADMIN_ONLY", frozenset({"chat.tools"}))
    monkeypatch.setattr(roles, "_configured_admins", lambda: ["@root:local"])
    registry = SkillRegistry()
    chat_skills.bind(registry)
    patterns = skill_tool_contribution(registry, ctx_factory=_ctx).allowed_principals["axiom_chat__tools"]
    assert refusal_reason("axiom_chat__tools", "@root:local", patterns) is None
    assert refusal_reason("axiom_chat__tools", "@ben:local", patterns)


def test_the_chat_trace_command_is_admin_only_and_not_exposed_over_mcp():
    ensure_loaded()
    trace = REGISTRY.get("/trace")
    assert trace.requires == "admin" and "mcp" not in trace.expose


# ---- nothing secret leaves through these doors ----------------------------------------------------------------


def test_a_credential_shaped_memory_is_withheld_even_if_the_gate_let_it_through():
    from axiom.extensions.builtins.chat.commands_ext.memory import _data_item

    m = SimpleNamespace(comp=SimpleNamespace(artifact_registry=SimpleNamespace(find_by_name=lambda *a: [])))
    item = SimpleNamespace(
        fragment_id="f1", cognitive_type="semantic", text="my aws key is AKIAIOSFODNN7EXAMPLE ok"
    )
    out = _data_item(m, item)
    assert "AKIA" not in json.dumps(out) and "withheld" in out["text"]
    plain = _data_item(m, SimpleNamespace(fragment_id="f2", cognitive_type="semantic", text="lunch is at noon"))
    assert plain["text"] == "lunch is at noon"


_SECRET_KEYS = re.compile(r"(api[_-]?key|secret|token|password|credential|recovery)", re.I)


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_the_listing_skills_return_names_and_states_never_secret_fields():
    for name, params in (("chat.tools", {}), ("chat.skills", {}), ("chat.agents", {}), ("chat.where", {})):
        value = read.FUNCTIONS[name](params, _ctx()).value
        # a name ending in _ref points at where a secret lives (the vault); it is never the secret
        bad = [k for k in _keys(value) if _SECRET_KEYS.search(k) and not k.endswith("_ref")]
        assert not bad, f"{name} returns secret-looking fields: {bad}"


def test_a_stats_skill_without_a_session_is_an_error_not_a_guess():
    out = read.FUNCTIONS["chat.stats"]({"session": "no-such-session"}, _ctx())
    assert out.ok is False and "no stored session" in out.errors[0]
