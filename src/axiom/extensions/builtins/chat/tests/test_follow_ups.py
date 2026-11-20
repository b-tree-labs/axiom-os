# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every command result leads to its follow-up: one shared layer, adopted by the commands with weak journeys.

Real registry, real ``ChatAgent`` (only the model gateway, the true external boundary, is a stub), real
checkpoints and approval queue in per-test temp state. A follow-up is checked two ways: that it appears where
the journey earns it, and that it names something that exists.
"""

from __future__ import annotations

import json
import re

import pytest

from axiom.extensions.builtins.chat import completion, tools
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import (
    REGISTRY,
    Command,
    CommandContext,
    CommandRegistry,
)
from axiom.extensions.builtins.chat.follow_ups import (
    CommandResult,
    FollowUp,
    check_follow_up,
    pick,
    remember_list,
    resolve_ref,
    result,
    validate_registry,
)
from axiom.extensions.builtins.chat.usage import TurnUsage
from axiom.infra.skills import SkillRegistry, SkillSpec

from ._cmd_helpers import _jsonl_ledger, agent_turn  # noqa: F401
from ._tools_cmd_helpers import make_agent

_AGENT = ("cli", "mcp", "agent_tool")


@pytest.fixture(autouse=True)
def _quiet_discovery(monkeypatch):
    from axiom.extensions import discovery

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setattr(tools, "_manifest_roots", lambda: [])
    monkeypatch.setattr(discovery, "discover_and_load_chat_tools", lambda *a, **k: [])
    r = SkillRegistry()
    r.register_skill(SkillSpec(name="press.draft", fn=lambda p, c: None, description="Draft.", surfaces=_AGENT))
    monkeypatch.setattr(tools, "_skill_registry", lambda: r)
    monkeypatch.setattr(tools, "_skill_namespaces", lambda: [])


def run(line, agent=None, surface="repl"):
    ensure_loaded()
    return REGISTRY.dispatch(line, CommandContext(agent=agent or make_agent(), store=None, surface=surface))


def commands_of(out):
    return [f.command for f in out.follow_ups]


# ---- the layer itself -------------------------------------------------------------------------------


def test_a_result_prints_like_the_string_it_wraps_and_carries_its_follow_ups():
    out = CommandResult("hello", [FollowUp("/where", "see where")])
    assert isinstance(out, str)
    assert out.startswith("hello") and "Next:" in out and "/where  (see where)" in out
    assert out.follow_ups == (FollowUp("/where", "see where"),)
    assert CommandResult("plain") == "plain"  # no follow-ups, no footer: backward compatible


def test_a_handler_cannot_emit_a_follow_up_its_command_never_declared():
    ensure_loaded()
    with pytest.raises(KeyError, match="did not declare"):
        result("/where", "x", "invented")
    with pytest.raises(KeyError, match="not a registered"):
        result("/nope", "x", "k")


def test_a_template_without_its_values_is_an_error_not_a_literal():
    ensure_loaded()
    with pytest.raises(KeyError):
        pick("/tools", "refused")  # needs {tool}


def test_plain_string_handlers_keep_working_and_clear_stale_follow_ups():
    ensure_loaded()
    reg = CommandRegistry()
    reg.register(Command("/echo", "echo", "Inspect", lambda ctx, a: " ".join(a)))
    agent = make_agent()
    agent._last_follow_ups = (FollowUp("/where", "old"),)
    assert reg.dispatch("/echo hi", CommandContext(agent=agent, store=None)) == "hi"
    assert agent._last_follow_ups == ()


def test_every_declared_follow_up_names_a_command_subcommand_or_flag_that_exists():
    ensure_loaded()
    assert validate_registry(REGISTRY) == []


def test_the_validator_can_fail():
    """Negative controls: each kind of dangling reference is caught (so the walk above means something)."""
    ensure_loaded()
    for bad, why in [
        ("/nosuchcommand", "not a registered command"),
        ("/permissions frobnicate x", "no subcommand"),
        ("/stats --nosuchflag", "no flag"),
        ("{cli} nosuchnoun do", "not a CLI command"),
        ("rm -rf /", "neither"),
    ]:
        msg = check_follow_up(REGISTRY, "/x", "k", FollowUp(bad, "w"))
        assert msg and why in msg, (bad, msg)
    assert check_follow_up(REGISTRY, "/x", "k", FollowUp("/permissions set {tool} ask", "w")) is None
    assert check_follow_up(REGISTRY, "/x", "k", FollowUp("{cli} settings set chat.tool_namespaces {ns}", "w")) is None


def test_the_cli_nouns_follow_ups_may_name_exist_in_the_real_cli_registry():
    from axiom.cli_registry import CLI_MODULES
    from axiom.extensions.builtins.chat.follow_ups import KNOWN_CLI_NOUNS

    assert set(CLI_MODULES) >= KNOWN_CLI_NOUNS, sorted(KNOWN_CLI_NOUNS - set(CLI_MODULES))


ADOPTERS = (
    "/tools", "/stats", "/usage", "/compact", "/sources", "/compare", "/memory", "/review", "/where",
    "/renew", "/fork", "/undo", "/skills", "/agents",
)


@pytest.mark.parametrize("name", ADOPTERS)
def test_each_weak_journey_command_declares_its_follow_ups_in_its_own_module(name):
    ensure_loaded()
    assert REGISTRY.get(name).follow_ups, f"{name} declares no follow-ups"


# ---- /tools -----------------------------------------------------------------------------------------


def test_tools_offers_the_exact_permission_command_for_a_refused_and_a_default_ask_tool():
    agent = make_agent()
    agent.permissions.set("write_file", "deny")
    out = run("/tools", agent)
    assert "/permissions set write_file ask" in commands_of(out)
    agent2 = make_agent()
    assert any(c.startswith("/permissions set ") and c.endswith(" allow") for c in commands_of(run("/tools", agent2)))


def test_tools_says_the_exact_setting_that_turns_the_skill_tools_on():
    out = run("/tools")
    assert any(c.endswith("settings set chat.tool_namespaces press") for c in commands_of(out))


def test_a_list_row_number_opens_the_same_row():
    agent = make_agent()
    run("/tools", agent)
    first = agent._last_lists["tools"][0]
    by_number = run("/tools 1", agent)
    by_name = run(f"/tools {first}", agent)
    assert str(by_number) == str(by_name) and first in by_number


def test_a_row_number_with_no_such_row_says_so():
    agent = make_agent()
    run("/tools", agent)
    assert "no row 9999" in run("/tools 9999", agent).lower()


def test_tools_json_carries_the_same_follow_ups_and_numbers():
    agent = make_agent()
    agent.permissions.set("write_file", "deny")
    data = json.loads(run("/tools --json", agent))
    assert data["tools"][0]["number"] == 1
    assert {"command": "/permissions set write_file ask"} .items() <= data["follow_ups"][0].items()


# ---- /stats and /usage ------------------------------------------------------------------------------


def _agent_with_usage(latency_ms=20_000.0):
    agent = make_agent()
    agent.usage.record_turn(
        TurnUsage(input_tokens=10, output_tokens=5, model="mystery-model-9", latency_ms=latency_ms, tool_calls=0, turn=1)
    )
    return agent


def test_stats_on_an_unpriced_model_says_how_to_price_it():
    out = run("/stats", _agent_with_usage())
    assert "cost_per_1k_input" in out and "llm-providers.toml" in out
    assert "/where" in commands_of(out)


def test_stats_slow_turn_points_an_admin_at_the_trace():
    out = run("/stats", _agent_with_usage(), surface="repl")  # own terminal: an administrator
    assert "/trace on" in commands_of(out)


def test_stats_slow_turn_tells_a_non_admin_plainly_instead_of_offering_what_they_cannot_run():
    out = run("/stats", _agent_with_usage(), surface="headless")
    assert "/trace on" not in commands_of(out)
    assert "administrators" in out and "nothing more can be shown" in out


def test_a_fast_priced_session_gets_no_trace_or_price_advice():
    agent = make_agent()
    agent.usage.record_turn(TurnUsage(input_tokens=1, output_tokens=1, model="m", cost=0.0, latency_ms=10.0, tool_calls=0, turn=1))
    out = run("/stats", agent)
    assert "/trace on" not in commands_of(out) and "cost_per_1k" not in out


def test_stats_json_includes_follow_ups_and_notes():
    data = json.loads(run("/stats --json", _agent_with_usage(), surface="headless"))
    assert any(f["command"] == "/where" for f in data["follow_ups"])
    assert any("cost_per_1k_input" in n for n in data["notes"])


def test_usage_on_an_unpriced_model_says_how_to_price_it():
    out = run("/usage", _agent_with_usage())
    assert "cost_per_1k_input" in out and "/stats" in commands_of(out)


# ---- /sources, /where -------------------------------------------------------------------------------


def test_sources_when_retrieval_is_off_names_the_command_that_turns_it_on():
    agent = make_agent()
    agent.session.context["rag"] = "off"
    assert "/rag on" in commands_of(run("/sources", agent))


def test_sources_when_nothing_was_retrieved_points_at_where():
    assert "/where" in commands_of(run("/sources", make_agent()))


def test_where_always_leads_somewhere():
    assert commands_of(run("/where", make_agent()))[:1] == ["/model"]


# ---- /undo, /fork -----------------------------------------------------------------------------------


def _undo_world(tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("a0")
    agent = make_agent()
    agent_turn(agent.session.session_id, [(a, "a1")])
    return agent, a


def test_undo_says_how_to_get_back(tmp_path):
    agent, a = _undo_world(tmp_path)
    out = run("/undo", agent)
    assert a.read_text() == "a0"
    assert commands_of(out) == ["/undo", "/diff"]
    assert "undo this undo" in out.follow_ups[0].why
    run("/undo", agent)
    assert a.read_text() == "a1"


def test_a_blocked_undo_offers_diff_and_the_planned_force(tmp_path):
    agent, a = _undo_world(tmp_path)
    a.write_text("mine")
    out = run("/undo", agent)
    assert a.read_text() == "mine" and commands_of(out) == ["/diff", "/undo --force"]


def test_undo_force_plans_first_and_changes_nothing(tmp_path):
    agent, a = _undo_world(tmp_path)
    a.write_text("mine")
    out = run("/undo --force", agent)
    assert a.read_text() == "mine" and "Planned, not run" in out
    assert any(c.startswith("/undo --force --apply ") for c in commands_of(out))


def test_undo_force_apply_runs_once_and_keeps_the_users_version(tmp_path):
    agent, a = _undo_world(tmp_path)
    a.write_text("mine")
    plan = run("/undo --force", agent)
    held = re.search(r"held as (\S+),", plan).group(1)
    done = run(f"/undo --force --apply {held}", agent)
    assert a.read_text() == "a0" and "forced over 1 file" in done
    again = run(f"/undo --force --apply {held}", agent)
    assert "Not run" in again and "already applied" in again
    run("/undo", agent)  # the undo of the undo brings the user's version back
    assert a.read_text() == "mine"


def test_undo_force_apply_refuses_when_the_file_moved_after_the_plan(tmp_path):
    agent, a = _undo_world(tmp_path)
    a.write_text("mine")
    plan = run("/undo --force", agent)
    held = re.search(r"held as (\S+),", plan).group(1)
    a.write_text("mine, edited again")
    out = run(f"/undo --force --apply {held}", agent)
    assert a.read_text() == "mine, edited again" and "changed since" in out


def test_undo_force_apply_from_a_served_surface_needs_a_real_approval(tmp_path):
    """Only a person's own terminal approves by typing --apply. A headless caller waits for `approve ok`."""
    agent, a = _undo_world(tmp_path)
    a.write_text("mine")
    plan = run("/undo --force", agent)
    held = re.search(r"held as (\S+),", plan).group(1)
    out = run(f"/undo --force --apply {held}", agent, surface="headless")
    assert a.read_text() == "mine" and "approve ok" in out
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import FileActionStore, default_approval_path

    ApprovalGate(FileActionStore(default_approval_path())).approve(held, decided_by="@ben:local")
    run(f"/undo --force --apply {held}", agent, surface="headless")
    assert a.read_text() == "a0"


def test_fork_says_how_to_go_back():
    import tempfile
    from pathlib import Path

    from axiom.infra.orchestrator.session import SessionStore

    store = SessionStore(Path(tempfile.mkdtemp()))
    agent = make_agent()
    agent.session.add_message("user", "hello")
    original = agent.session.session_id
    out = REGISTRY.dispatch("/fork", CommandContext(agent=agent, store=store, surface="repl"))
    assert commands_of(out)[0] == f"/renew {original}"


# ---- /skills, /agents, /renew numbers -----------------------------------------------------------------


def test_skills_rows_are_numbered_and_a_number_opens_the_row():
    agent = make_agent()
    listing = run("/skills", agent)
    name = agent._last_lists["skills"][0]
    opened = run("/skills 1", agent)
    assert name in opened and "Parameters" in opened
    assert any(c.startswith("{cli}") or "settings set chat.tool_namespaces" in c for c in commands_of(listing))


def test_agents_rows_are_numbered_and_a_number_opens_the_row():
    agent = make_agent()
    listing = run("/agents", agent)
    names = agent._last_lists.get("agents") or []
    if not names:
        pytest.skip("no agents declared in this install")
    assert names[0] in run("/agents 1", agent)
    assert "/agents 1" in commands_of(listing)


def test_resolve_ref_reads_the_last_list_and_falls_back_deterministically():
    agent = make_agent()
    assert resolve_ref(agent, "x", "3")[0] is None
    assert resolve_ref(agent, "x", "3", fallback=lambda: ["a", "b", "c"]) == ("c", "")
    remember_list(agent, "x", ["p", "q"])
    assert resolve_ref(agent, "x", "2") == ("q", "")
    assert resolve_ref(agent, "x", "5")[0] is None
    assert resolve_ref(agent, "x", "name") == ("name", "")


# ---- autocomplete: the ghost text after a result --------------------------------------------------------


def test_ghost_text_after_a_result_suggests_its_likeliest_follow_up(tmp_path):
    agent, a = _undo_world(tmp_path)
    run("/undo", agent)  # offers /undo then /diff
    assert completion.best_remainder("/", agent) == "undo"
    assert completion.best_remainder("/di", agent) == "ff"


def test_ghost_text_never_offers_a_follow_up_that_still_needs_filling_in():
    agent = make_agent()
    agent._last_follow_ups = (FollowUp("/permissions set <tool> allow", "x"), FollowUp("/where", "w"))
    sugs = [s.value for s in completion.suggest("/", agent) if s.kind == "follow-up"]
    assert sugs == ["/where"]


def test_a_follow_up_result_does_not_outlive_the_next_plain_result():
    agent = make_agent()
    run("/tools", agent)
    assert agent._last_follow_ups
    run("/help", agent)
    assert agent._last_follow_ups == ()
    assert [s for s in completion.suggest("/", agent) if s.kind == "follow-up"] == []
