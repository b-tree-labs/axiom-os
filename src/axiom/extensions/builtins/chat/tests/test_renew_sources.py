# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""/renew lists native AND other harnesses' sessions together, labelled by source, filterable, and completes
the harness names that are real options."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.chat.command_registry import REGISTRY
from axiom.extensions.builtins.chat.commands_ext import renew, renew_sources
from axiom.infra.orchestrator.session import Session

from ._renew_harness_support import (
    CODEX_ID,
    SECRET,
    claude_session,
    codex_session,
)
from ._renew_support import ME, SOMEONE_ELSE, Agent, make_session, new_store


@pytest.fixture
def rig(tmp_path, _no_foreign_harness_state, monkeypatch):
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    monkeypatch.setattr("axiom.infra.orchestrator.session.may_adopt", lambda p: False)
    home = _no_foreign_harness_state
    store = new_store(tmp_path)
    # newest first overall: codex (1h), native (2h), claude (3h)
    codex_session(home, hours_ago=1)
    make_session(store, "nat000000001", "Native notes on the loop", hours_ago=2)
    claude_session(home, hours_ago=3)
    make_session(store, "nat000000002", "Very old native", hours_ago=40)
    return store, Agent(Session(principal_id=ME)), home


def _register(name: str) -> None:
    """Install the Axiom MCP in a harness through the installer's own writer."""
    from axiom.extensions.builtins.mcp.install import install_entry, resolve_server, spec_for

    server, command, args = resolve_server()
    install_entry(spec_for(name), server, command, args, {})


# ---- aggregate listing --------------------------------------------------------------------------


def test_the_listing_mixes_native_and_foreign_sessions_newest_first_each_with_its_source(rig):
    store, agent, _ = rig
    rows = renew.aggregate_rows(store, agent.session)
    assert [(r["source"], r["title"]) for r in rows] == [
        ("codex", "Check the flux log"),
        ("chat", "Native notes on the loop"),
        ("claude-code", "Detector calibration plan"),
        ("chat", "Very old native"),
    ]


def test_the_rendered_list_has_a_source_column_and_the_next_steps(rig):
    store, agent, _ = rig
    out = renew.renew(agent, store, "")
    lines = [ln for ln in out.splitlines() if ln.strip().startswith(("1.", "2.", "3.", "4."))]
    assert "codex" in lines[0] and "chat" in lines[1] and "claude-code" in lines[2]
    assert "/renew <number|id|text>" in out
    assert "/renew <chat|claude-code|cline|codex|continue|cursor|gemini|goose|hermes|opencode|pi>" in out
    assert "/sessions" in out


@pytest.mark.parametrize("args", [["codex"], ["--harness", "codex"], ["--source", "codex"], ["--harness=codex"]])
def test_a_harness_name_in_any_spelling_narrows_the_list_to_that_source(rig, args):
    store, agent, _ = rig
    request = renew.parse_request(args)
    assert request.source == "codex" and request.query == ""
    rows = renew.aggregate_rows(store, agent.session, source=request.source)
    assert {r["source"] for r in rows} == {"codex"}


def test_chat_means_the_native_sessions_only(rig):
    store, agent, _ = rig
    rows = renew.aggregate_rows(store, agent.session, source="chat")
    assert {r["source"] for r in rows} == {"chat"} and len(rows) == 2


def test_a_harness_name_then_text_filters_within_that_source(rig):
    store, agent, home = rig
    claude_session(home, "c1a0de00-0000-4000-8000-000000000009", first_prompt="other", title="Other thing", hours_ago=9)
    request = renew.parse_request(["claude-code", "calibration"])
    assert (request.source, request.query) == ("claude-code", "calibration")
    out = renew.renew(agent, store, request.query, source=request.source)
    assert "renewed Detector calibration plan from claude-code" in out


def test_claude_is_an_alias_and_an_unknown_source_is_named_not_ignored(rig):
    assert renew.parse_request(["claude"]).source == "claude-code"
    bad = renew.parse_request(["--harness", "nonesuch"])
    assert bad.bad_source == "nonesuch"


def test_json_listing_has_source_titles_and_counts_but_never_content(rig):
    store, agent, _ = rig
    data = json.loads(renew.list_json(store, agent.session, source=None))
    assert {s["source"] for s in data["sessions"]} == {"chat", "codex", "claude-code"}
    flat = json.dumps(data)
    assert "The flux is flat" not in flat and "Calibration needs" not in flat
    assert all("message_count" in s and "title" in s for s in data["sessions"])


# ---- scope --------------------------------------------------------------------------------------


def test_another_principal_is_never_shown_this_machines_harness_files(rig):
    store, _, _ = rig
    stranger = Session(principal_id=SOMEONE_ELSE)
    rows = renew.aggregate_rows(store, stranger)
    assert [r for r in rows if r["source"] != "chat"] == []
    assert renew_sources.foreign_rows(SOMEONE_ELSE) == []
    assert renew_sources.foreign_rows("") == []


def test_a_stranger_cannot_import_by_naming_a_foreign_id(rig):
    store, _, _ = rig
    agent = Agent(Session(principal_id=SOMEONE_ELSE))
    out = renew.resume_session(store, agent, f"codex:{CODEX_ID}")
    assert "not found" in out
    assert sorted(store.list_sessions()) == ["nat000000001", "nat000000002"]  # nothing was created
    assert agent.session.context.get("renewed_from") is None


# ---- secrets ------------------------------------------------------------------------------------


def test_a_session_whose_title_reads_as_a_secret_is_listed_without_it(tmp_path, _no_foreign_harness_state, monkeypatch):
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    home = _no_foreign_harness_state
    claude_session(home, first_prompt=f"use key {SECRET} for the api", title="")
    store = new_store(tmp_path)
    agent = Agent(Session(principal_id=ME))
    out = renew.renew(agent, store, "")
    assert SECRET not in out
    assert "withheld" in out
    assert SECRET not in renew.list_json(store, agent.session)


# ---- completion ---------------------------------------------------------------------------------


def test_completion_offers_installed_union_reader_and_marks_the_ones_with_no_reader(rig):
    store, agent, _ = rig
    agent.session_store = store
    _register("codex")      # installed, has a reader
    _register("windsurf")   # installed, NO reader
    _register("vscode")     # installed, NO reader
    offered = dict(REGISTRY.get("/renew").complete(agent, []))
    assert offered["chat"]
    assert "Axiom MCP installed" in offered["codex"] and "no session reader" not in offered["codex"]
    assert "sessions from claude-code" in offered["claude-code"]  # reader, MCP not installed: still an option
    assert offered["windsurf"].endswith("no session reader yet")
    assert offered["vscode"].endswith("no session reader yet")
    assert "sessions from gemini" in offered["gemini"]  # a verified reader ships for it: not marked waiting
    assert "letta" not in offered and "aider" not in offered  # neither installed nor readable


def test_a_harness_nobody_installed_and_nobody_reads_is_not_an_option(rig):
    store, agent, _ = rig
    agent.session_store = store
    assert "letta" not in dict(REGISTRY.get("/renew").complete(agent, []))


# ---- a harness with no reader says so -----------------------------------------------------------


def test_choosing_an_installed_harness_with_no_reader_says_what_is_missing_and_what_exists(rig):
    store, agent, _ = rig
    _register("windsurf")
    out = renew.renew(agent, store, "", source="windsurf")
    assert "no session reader" in out and "windsurf" in out
    assert "What is missing" in out and "unverified" in out   # said plainly, not guessed
    assert "rules files can be written to" in out             # what exists for it
    assert "claude-code" in out and "codex" in out            # what works now
    assert agent.session.context.get("renewed_from") is None


def test_a_harness_with_a_reader_but_no_sessions_on_this_machine_says_so(tmp_path, _no_foreign_harness_state, monkeypatch):
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    agent = Agent(Session(principal_id=ME))
    out = renew.renew(agent, new_store(tmp_path), "", source="codex")
    assert "No codex sessions were found" in out and "/sessions" in out
