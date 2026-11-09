# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi chat`` flags come from the slash registry: one declaration gives the flag, the command and the help.

A flag must drive the *same handler* as its slash command, so the tests call through
``REGISTRY.dispatch`` and check the state the handler leaves behind, not a parallel implementation.
"""

from __future__ import annotations

import argparse
import io
import json

import pytest

from axiom.extensions.builtins.chat import cli_flags, tools
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.cli import get_parser
from axiom.extensions.builtins.chat.command_registry import (
    REGISTRY,
    CliSpec,
    Command,
    CommandRegistry,
)

from ._tools_cmd_helpers import make_agent


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    from axiom.extensions import discovery

    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setattr(tools, "_manifest_roots", lambda: [])
    monkeypatch.setattr(discovery, "discover_and_load_chat_tools", lambda *a, **k: [])


@pytest.fixture(scope="module")
def parser():
    return get_parser()


def parse(parser, *argv):
    return parser.parse_args(list(argv))


# ---- the registry drives the help ---------------------------------------------------------------------


def test_every_registry_command_with_a_cli_spec_appears_in_help_with_the_command_it_mirrors(parser):
    ensure_loaded()
    help_text = parser.format_help()
    specs = cli_flags.cli_commands()
    assert len(specs) >= 14
    for cmd, spec in specs:
        for flag in spec.flags:
            assert flag in help_text, f"{flag} ({cmd.name}) missing from --help"
        epilog = help_text.split("slash command mirrors:\n")[-1]
        line = next(ln for ln in epilog.splitlines() if ln.strip().startswith(spec.flags[0]))
        assert f"mirrors {cmd.name}" in line, f"{spec.flags[0]} does not say it mirrors {cmd.name}"


def test_the_flags_the_task_names_all_exist(parser):
    help_text = parser.format_help()
    for flag in (
        "--rag", "--trace", "--plan", "--agent", "--no-tools", "--provider", "--model", "--renew", "--harness",
        "--compare", "--no-memory", "--where", "--status", "--stats", "--export", "--format", "--tools",
        "--skills", "--agents",
    ):
        assert flag in help_text, flag


def test_existing_flags_still_parse_and_keep_their_meaning(parser):
    ns = parse(parser, "--resume", "abc", "--model", "m", "--provider", "p", "--mode", "public", "--no-tui")
    assert (ns.resume, ns.model, ns.provider, ns.mode, ns.no_tui) == ("abc", "m", "p", "public", True)
    ns = parse(parser, "--ask", "what is 2+2", "--json")
    assert ns.ask == "what is 2+2" and ns.json is True
    ns = parse(parser, "--renew", "--harness", "codex", "--json")
    assert ns.renew == "__pick__" and ns.harness == "codex"
    assert parse(parser, "--ask", "q").plan is False  # the new mode flags default off


def test_a_declared_existing_flag_the_parser_lacks_is_an_error():
    reg = CommandRegistry()
    reg.register(Command("/x", "x", "g", lambda c, a: "", cli=CliSpec(("--ghost",), existing=True)))
    with pytest.raises(ValueError, match="does not have"):
        cli_flags.add_registry_flags(argparse.ArgumentParser(), reg)


def test_a_new_flag_that_clashes_with_an_existing_one_is_an_error():
    reg = CommandRegistry()
    reg.register(Command("/x", "x", "g", lambda c, a: "", cli=CliSpec(("--model",), line="/x")))
    p = argparse.ArgumentParser()
    p.add_argument("--model")
    with pytest.raises(ValueError, match="already a flag"):
        cli_flags.add_registry_flags(p, reg)


# ---- startup flags run the same handler ---------------------------------------------------------------


def test_rag_off_drives_the_rag_handler(parser, monkeypatch):
    seen = []
    real = REGISTRY.dispatch
    monkeypatch.setattr(REGISTRY, "dispatch", lambda line, ctx: (seen.append(line), real(line, ctx))[1])
    agent = make_agent()
    printed = cli_flags.apply_startup(parse(parser, "--rag", "off"), agent, None)
    assert seen == ["/rag off"], "the flag must go through the registry's dispatch"
    assert agent.session.context["rag"] == "off" and "OFF" in printed[0]
    # and it is the same state the slash command leaves
    other = make_agent()
    REGISTRY.dispatch("/rag off", __import__("axiom.extensions.builtins.chat.command_registry", fromlist=["x"]).CommandContext(agent=other, store=None))
    assert other.session.context == agent.session.context


@pytest.mark.parametrize(
    "flag,mode", [("--plan", "plan"), ("--agent", "agent"), ("--no-tools", "ask")]
)
def test_mode_flags_set_the_interaction_mode(parser, flag, mode):
    agent = make_agent()
    cli_flags.apply_startup(parse(parser, flag), agent, None)
    assert agent._interaction_mode == mode


def test_two_mode_flags_are_refused_not_silently_resolved(parser):
    assert "choose one of" in cli_flags.conflicts(parse(parser, "--plan", "--agent"))
    assert cli_flags.conflicts(parse(parser, "--plan")) == ""


def test_trace_flag_turns_the_trace_on_for_the_person_at_their_terminal(parser):
    agent = make_agent()
    cli_flags.apply_startup(parse(parser, "--trace"), agent, None)
    assert agent.session.context.get("trace") is True


def test_no_memory_is_a_flag_with_no_slash_line_and_dispatches_nothing(parser, monkeypatch):
    ns = parse(parser, "--no-memory")
    assert ns.no_memory is True
    monkeypatch.setattr(REGISTRY, "dispatch", lambda *a, **k: pytest.fail("--no-memory must not run a slash line"))
    assert cli_flags.apply_startup(ns, make_agent(), None) == []


def test_no_flags_means_nothing_runs(parser):
    assert cli_flags.apply_startup(parse(parser), make_agent(), None) == []
    assert cli_flags.headless_request(parse(parser)) is None


def test_compare_builds_the_slash_line_with_the_prompt_intact(parser):
    ns = parse(parser, "--compare", "fast", "careful", "summarize this file")
    cmd, spec, sid, line = cli_flags.headless_request(ns)
    assert (cmd.name, sid, line) == ("/compare", "", "/compare fast careful summarize this file")


# ---- headless flags print and exit -----------------------------------------------------------------------


def real_agent(tmp_path):
    """A real ChatAgent over a real Gateway that has no providers configured (an empty config dir)."""
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import Gateway

    return ChatAgent(gateway=Gateway(config_dir=tmp_path), bus=EventBus())


def _run(parser, *argv, factory=None, tmp_path=None):
    out = io.StringIO()
    if factory is None:
        agent = real_agent(tmp_path)
        factory = lambda sid: (agent, None)  # noqa: E731
    code = cli_flags.run_headless(parse(parser, *argv), agent_factory=factory, out=out)
    return code, out.getvalue()


def test_where_json_prints_and_exits_with_follow_ups(parser, tmp_path):
    code, text = _run(parser, "--where", "--json", tmp_path=tmp_path)
    data = json.loads(text)
    assert code == 0 and "follow_ups" in data and data["follow_ups"][0]["command"] == "/model"


def test_tools_json_prints_the_tool_table(parser, tmp_path):
    code, text = _run(parser, "--tools", "--json", tmp_path=tmp_path)
    data = json.loads(text)
    assert code == 0 and data["count"] == len(data["tools"]) > 0 and data["tools"][0]["number"] == 1


def test_status_json_prints_the_session_and_where(parser, tmp_path):
    code, text = _run(parser, "--status", "--json", tmp_path=tmp_path)
    data = json.loads(text)
    assert code == 0 and {"session", "where", "mode", "usage"} <= set(data)


def test_skills_and_agents_json(parser, tmp_path):
    for flag in ("--skills", "--agents"):
        code, text = _run(parser, flag, "--json", tmp_path=tmp_path)
        assert code == 0 and "rows" in json.loads(text)


def test_stats_for_a_stored_session_reads_the_usage_that_session_recorded(parser):
    from axiom.extensions.builtins.chat.agent import USAGE_CONTEXT_KEY
    from axiom.extensions.builtins.chat.skills.read import agent_for
    from axiom.extensions.builtins.chat.usage import TurnUsage, UsageTracker
    from axiom.infra.orchestrator.session import Session, SessionStore

    tracker = UsageTracker()
    tracker.record_turn(TurnUsage(input_tokens=11, output_tokens=7, model="m", cost=0.0, latency_ms=5.0, tool_calls=0, turn=1))
    session = Session()
    session.add_message("user", "hi")
    session.context[USAGE_CONTEXT_KEY] = tracker.to_dict()
    SessionStore().save(session)
    code, text = _run(parser, "--stats", session.session_id, "--json", factory=lambda sid: agent_for({"session": sid}))
    data = json.loads(text)
    assert code == 0 and data["totals"]["input_tokens"] == 11 and data["turns"][0]["output_tokens"] == 7


def test_stats_for_a_session_that_does_not_exist_fails_with_a_sentence(parser, capsys):
    from axiom.extensions.builtins.chat.skills.read import agent_for

    code, text = _run(parser, "--stats", "nope", factory=lambda sid: agent_for({"session": sid}))
    assert code == 1 and text == "" and "no stored session" in capsys.readouterr().err


def test_export_runs_the_export_handler_for_a_stored_session(parser, tmp_path):
    from axiom.extensions.builtins.chat.skills.read import agent_for
    from axiom.infra.orchestrator.session import Session, SessionStore

    session = Session()
    session.add_message("user", "hello there")
    session.add_message("assistant", "hi")
    SessionStore().save(session)
    code, text = _run(parser, "--export", session.session_id, "--format", "json", factory=lambda sid: agent_for({"session": sid}))
    assert code == 0 and "Exported 2 messages" in text and ".json" in text


def test_a_usage_total_survives_a_resume():
    """The per-turn usage a session recorded comes back when the session is resumed (so --stats can read it)."""
    from axiom.extensions.builtins.chat.agent import USAGE_CONTEXT_KEY
    from axiom.extensions.builtins.chat.usage import TurnUsage

    agent = make_agent()
    agent.usage.record_turn(TurnUsage(input_tokens=3, output_tokens=2, model="m", cost=0.0, turn=1))
    agent.session.context[USAGE_CONTEXT_KEY] = agent.usage.to_dict()
    from axiom.extensions.builtins.chat.agent import ChatAgent

    again = ChatAgent(gateway=agent.gateway, bus=agent.bus, session=agent.session)
    assert again.usage.total_input_tokens == 3
