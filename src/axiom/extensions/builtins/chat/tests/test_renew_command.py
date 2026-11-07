# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/renew and /resume: resolving a query, the REPL list-and-choose, what resuming says and restores."""

from __future__ import annotations

import re

from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.commands_ext import renew
from axiom.infra.orchestrator.session import Session

from ._renew_support import ME, Agent, FakeGateway, make_session, new_store, provider

ensure_loaded()


def _plain(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(s))


def _store(tmp_path):
    store = new_store(tmp_path)
    make_session(store, "aaaa11110001", "Fuel temperature report", hours_ago=2, messages=4,
                 context={"last_provider": "node-qwen"})
    make_session(store, "aaaa11110002", "Fuel loading plan", hours_ago=30, messages=2)
    make_session(store, "bbbb22220001", "Calibration of the detector", hours_ago=60, messages=6)
    return store


def _agent():
    return Agent(Session(principal_id=ME))


def run(agent, store, line, surface="headless"):
    return _plain(REGISTRY.dispatch(line, CommandContext(agent=agent, store=store, surface=surface, line=line)))


# ---- resolution ---------------------------------------------------------------------------------


def test_a_number_picks_from_the_displayed_list(tmp_path):
    store, agent = _store(tmp_path), _agent()
    rows = renew.session_rows(store, agent.session)
    assert renew.resolve(rows, "3").unique["id"] == "bbbb22220001"


def test_an_id_prefix_picks_the_one_session(tmp_path):
    store, agent = _store(tmp_path), _agent()
    rows = renew.session_rows(store, agent.session)
    assert renew.resolve(rows, "bbbb").unique["id"] == "bbbb22220001"


def test_text_matches_the_title_fuzzily(tmp_path):
    store, agent = _store(tmp_path), _agent()
    rows = renew.session_rows(store, agent.session)
    assert renew.resolve(rows, "calib detector").unique["id"] == "bbbb22220001"
    assert renew.resolve(rows, "clbrtn").unique["id"] == "bbbb22220001"  # letters in order


def test_ambiguous_text_shows_the_matches_and_resumes_nothing(tmp_path):
    store, agent = _store(tmp_path), _agent()
    before = agent.session
    out = run(agent, store, "/renew fuel")
    assert agent.session is before
    assert "Fuel temperature report" in out and "Fuel loading plan" in out
    assert "nothing was resumed" in out


def test_a_number_out_of_range_is_said_and_resumes_nothing(tmp_path):
    store, agent = _store(tmp_path), _agent()
    before = agent.session
    out = run(agent, store, "/renew 9")
    assert agent.session is before and "no session number 9" in out


# ---- resuming -----------------------------------------------------------------------------------


def test_resume_loads_messages_and_says_what_was_renewed(tmp_path):
    store, agent = _store(tmp_path), _agent()
    out = run(agent, store, "/renew 1")
    assert agent.session.session_id == "aaaa11110001"
    assert len(agent.session.messages) == 4
    assert "renewed Fuel temperature report (4 messages, last used 2h ago on node-qwen)" in out


def test_resume_is_the_same_handler_and_a_bare_resume_gives_the_same_list(tmp_path):
    store, agent = _store(tmp_path), _agent()
    assert run(agent, store, "/resume") == run(agent, store, "/renew")
    run(agent, store, "/resume calibration")
    assert agent.session.session_id == "bbbb22220001"


def test_the_old_session_is_saved_before_switching(tmp_path):
    store, agent = _store(tmp_path), _agent()
    agent.session.add_message("user", "a half-finished thought")
    sid = agent.session.session_id
    run(agent, store, "/renew 1")
    assert store.peek(sid)["messages"][0]["content"] == "a half-finished thought"


def test_per_session_settings_are_restored(tmp_path):
    store = new_store(tmp_path)
    make_session(store, "set000000001", "settings", context={"rag": "off", "trace": True, "mode": "plan"})
    agent = _agent()
    out = run(agent, store, "/renew 1")
    assert agent.session.context["rag"] == "off" and agent.session.context["trace"] is True
    assert agent._interaction_mode == "plan"
    assert "retrieval off" in out and "trace on" in out and "mode plan" in out


def test_settings_are_remembered_by_the_session_they_belong_to(tmp_path):
    """remember_settings -> save -> renew round-trips the mode and the provider that answered."""
    store = new_store(tmp_path)
    gateway = FakeGateway(provider("node-qwen"))
    gateway.last_selected = gateway.providers[0]
    agent = Agent(Session(principal_id=ME), gateway)
    agent.session.add_message("user", "hello there")
    agent.set_interaction_mode("ask")
    renew.remember_settings(agent)
    store.save(agent.session)

    fresh = Agent(Session(principal_id=ME), gateway)
    out = run(fresh, store, f"/renew {agent.session.session_id}")
    assert fresh._interaction_mode == "ask"
    assert "on node-qwen" in out


def test_a_controlled_session_shows_its_badge_and_warns_when_the_active_provider_is_not_admitted(tmp_path):
    store = new_store(tmp_path)
    make_session(store, "ctl000000001", "controlled analysis",
                 context={"content_tier": "export_controlled", "last_provider": "secure-node"})
    gateway = FakeGateway(provider("cloud", "any"), provider("secure-node", "export_controlled"))
    agent = Agent(Session(principal_id=ME), gateway)
    rows = renew.session_rows(store, agent.session)
    assert "[CONTROLLED]" in "\n".join(renew.render_rows(rows))
    out = run(agent, store, "/renew 1")
    assert "[CONTROLLED]" in out
    assert "cloud (tier any) may not receive" in out
    assert "secure-node" in out.split("Providers that may:")[1]
    # the discipline itself lives in the session: the tier is still controlled after the switch
    assert agent.session.context["content_tier"] == "export_controlled"


def test_no_warning_when_the_active_provider_is_admitted(tmp_path):
    store = new_store(tmp_path)
    make_session(store, "ctl000000001", "controlled analysis", context={"content_tier": "export_controlled"})
    agent = Agent(Session(principal_id=ME), FakeGateway(provider("secure-node", "export_controlled")))
    assert "may not receive" not in run(agent, store, "/renew 1")


def test_the_list_shows_title_age_count_and_provider(tmp_path):
    store, agent = _store(tmp_path), _agent()
    first = run(agent, store, "/renew").splitlines()
    row = next(line for line in first if "aaaa11110001" in line)
    assert "Fuel temperature report" in row and "4 msgs" in row and "2h ago" in row and "node-qwen" in row


# ---- the line REPL ------------------------------------------------------------------------------


def test_repl_number_choice_renews(tmp_path, monkeypatch):
    store, agent = _store(tmp_path), _agent()
    answers = iter(["2"])
    monkeypatch.setattr(renew, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(renew, "_read_line", lambda prompt: next(answers))
    out = run(agent, store, "/renew", surface="repl")
    assert agent.session.session_id == "aaaa11110002" and "renewed Fuel loading plan" in out


def test_repl_filter_text_then_number(tmp_path, monkeypatch):
    store, agent = _store(tmp_path), _agent()
    answers = iter(["fuel", "2"])
    monkeypatch.setattr(renew, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(renew, "_read_line", lambda prompt: next(answers))
    run(agent, store, "/renew", surface="repl")
    assert agent.session.session_id == "aaaa11110002"  # 2nd of the two "fuel" rows


def test_repl_empty_input_cancels(tmp_path, monkeypatch):
    store, agent = _store(tmp_path), _agent()
    before = agent.session
    monkeypatch.setattr(renew, "_stdin_is_tty", lambda: True)
    monkeypatch.setattr(renew, "_read_line", lambda prompt: "")
    out = run(agent, store, "/renew", surface="repl")
    assert agent.session is before and "Cancelled" in out


def test_without_a_terminal_a_bare_renew_lists_and_does_not_block(tmp_path, monkeypatch):
    store, agent = _store(tmp_path), _agent()
    monkeypatch.setattr(renew, "_stdin_is_tty", lambda: False)
    monkeypatch.setattr(renew, "_read_line", lambda prompt: (_ for _ in ()).throw(AssertionError("read")))
    assert "Calibration of the detector" in run(agent, store, "/renew", surface="repl")


# ---- registry -----------------------------------------------------------------------------------


def test_renew_declares_argument_completion_with_recent_sessions(tmp_path):
    store, agent = _store(tmp_path), _agent()
    agent.session_store = store
    cmd = REGISTRY.get("/renew")
    assert cmd.complete is not None and REGISTRY.get("/resume").complete is not None
    offered = cmd.complete(agent, [])
    assert [v for v, _ in offered if v == "chat"] == ["chat"]  # the harness options lead (test_renew_sources)
    assert offered.index(("aaaa11110001", "Fuel temperature report")) < offered.index(
        ("bbbb22220001", "Calibration of the detector")
    )
    assert ("bbbb22220001", "Calibration of the detector") in offered


def test_sessions_keeps_its_subcommands():
    cmd = REGISTRY.get("/sessions")
    assert set(cmd.subcommands) == {"rename", "archive"}
    assert REGISTRY.get("/new") is not None


def test_the_prompt_completer_offers_a_commands_declared_arguments(tmp_path):
    from prompt_toolkit.document import Document

    from axiom.extensions.builtins.chat import cli
    from axiom.extensions.builtins.chat.providers.ptk_input import ChatCompleter

    store, agent = _store(tmp_path), _agent()
    completer = ChatCompleter(
        {"/renew": "x"}, args_fn=lambda head, prefix: cli._command_arg_completions(agent, store, head)
    )
    text = "/renew bbbb"
    got = list(completer.get_completions(Document(text, len(text)), None))
    assert [c.text for c in got] == ["22220001"]
    assert got[0].display_meta_text == "Calibration of the detector"
