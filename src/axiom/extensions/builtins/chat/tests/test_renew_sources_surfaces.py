# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The picker (TUI), the REPL list and the --renew startup flag all carry /renew's source column and filter."""

from __future__ import annotations

import json
import sys

import pytest

from axiom.extensions.builtins.chat import cli
from axiom.extensions.builtins.chat.commands_ext import renew
from axiom.extensions.builtins.chat.fullscreen import PickerMode
from axiom.infra.orchestrator.session import Session

from ._renew_harness_support import CLAUDE_ID, CODEX_ID, claude_session, codex_session
from ._renew_support import ME, Agent, make_session, new_store
from .test_renew_picker_and_cli import _build, _key
from .test_renew_sources import _register


@pytest.fixture
def rig(tmp_path, headless_terminal_factory, _no_foreign_harness_state, monkeypatch):
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    monkeypatch.setattr("axiom.infra.orchestrator.session.may_adopt", lambda p: False)
    home = _no_foreign_harness_state
    codex_session(home, hours_ago=1)
    claude_session(home, hours_ago=3)
    store = new_store(tmp_path)
    make_session(store, "nat000000001", "Native notes on the loop", hours_ago=2)
    agent = Agent(Session(principal_id=ME))
    return _build(headless_terminal_factory, store, agent), store, agent, home


def _sources(tui):
    return [r["source"] for r in tui._picker.items]


# ---- the TUI picker -----------------------------------------------------------------------------


def test_the_picker_shows_a_source_column_over_every_source(rig):
    tui, *_ = rig
    tui._handle_slash_command("/renew")
    assert tui._picker.mode is PickerMode.SELECT
    assert _sources(tui) == ["codex", "chat", "claude-code"]
    assert "source" in tui._chooser.headers
    col = tui._chooser.headers.index("source")
    assert [c.cells[col] for c in tui._chooser.choices] == ["codex", "chat", "claude-code"]


def test_renew_with_a_harness_opens_the_picker_narrowed_to_it(rig):
    tui, *_ = rig
    tui._handle_slash_command("/renew codex")
    assert _sources(tui) == ["codex"] and tui._picker.source_filter == "codex"
    assert "source: codex" in tui._chooser.title


def test_shift_tab_cycles_the_source_all_then_chat_then_each_reader_then_all(rig):
    from axiom.extensions.builtins.chat.commands_ext import renew_sources

    tui, *_ = rig
    tui._handle_slash_command("/renew")
    cycle = ["chat", *renew_sources.reader_names()]
    seen = []
    for _ in range(len(cycle) + 1):
        _key(tui, "s-tab")(None)
        seen.append((tui._picker.source_filter, sorted(set(_sources(tui)))))
    # chat, then each reader in name order (only the ones with a session on this machine show rows), then all
    assert [name for name, _ in seen] == [*cycle, ""]
    rows = dict(seen)
    assert rows["chat"] == ["chat"] and rows["claude-code"] == ["claude-code"] and rows["codex"] == ["codex"]
    assert "No gemini sessions to show" in tui._raw_output  # a source with nothing says so as the cycle passes it
    assert seen[-1][1] == ["chat", "claude-code", "codex"]


def test_enter_on_a_foreign_row_imports_it_and_switches_to_the_new_native_session(rig):
    tui, store, agent, _ = rig
    tui._handle_slash_command("/renew codex")
    _key(tui, "c-m")(type("E", (), {"app": tui._app, "data": "", "current_buffer": tui._input_buffer})())
    prov = agent.session.context["renewed_from"]
    assert prov["harness"] == "codex" and prov["source_session_id"] == CODEX_ID
    assert "imported, source unchanged" in tui._raw_output


def test_typing_a_source_name_in_the_picker_filters_to_it(rig):
    tui, *_ = rig
    tui._handle_slash_command("/renew")
    tui._picker_set_query("claude")
    assert _sources(tui) == ["claude-code"]


def test_a_harness_with_no_reader_is_said_not_opened_as_an_empty_picker(rig):
    tui, *_ = rig
    _register("windsurf")
    tui._handle_slash_command("/renew windsurf")
    assert tui._picker is None
    assert "no session reader" in tui._raw_output and "windsurf" in tui._raw_output


def test_sessions_stays_the_chats_own_list(rig):
    tui, *_ = rig
    tui._handle_slash_command("/sessions")
    assert _sources(tui) == ["chat"]


def test_confirm_tier_works_from_the_tui(rig):
    tui, store, agent, _ = rig
    tui._handle_slash_command(f"/renew codex:{CODEX_ID}")
    assert agent.session.context["renewed_from"]["tier_confirmed"] is False
    tui._handle_slash_command("/renew confirm-tier")
    assert agent.session.context["renewed_from"]["tier_confirmed"] is True


# ---- the REPL list ------------------------------------------------------------------------------


def test_the_repl_choice_list_numbers_rows_across_sources_and_takes_a_number(rig):
    _, store, agent, _ = rig
    shown = []
    chosen = renew.choose_in_repl(
        renew.aggregate_rows(store, agent.session), read=lambda _p: "1", write=shown.append
    )
    assert chosen["source"] == "codex"
    assert any("codex" in line and "chat" not in line.split("codex")[0] for line in shown if "1." in line)


# ---- --renew on the command line ----------------------------------------------------------------


def test_the_parser_takes_harness_and_source_as_the_same_option():
    parser = cli.get_parser()
    assert parser.parse_args(["--renew", "--harness", "codex"]).harness == "codex"
    assert parser.parse_args(["--renew", "--source", "claude-code"]).harness == "claude-code"
    assert parser.parse_args(["--renew", "fuel"]).harness is None


def test_startup_choice_with_a_source_resolves_inside_that_source(rig):
    _, store, _, _ = rig
    sid, why = renew.startup_choice(store, "flux", "codex")
    assert sid == f"codex:{CODEX_ID}" and why == ""
    sid, why = renew.startup_choice(store, "flux", "claude-code")
    assert sid == "" and "no session matches" in why
    assert renew.startup_choice(store, "__pick__", "codex") == ("__pick__", "")


def test_startup_choice_for_a_harness_with_no_reader_says_so(rig):
    _, store, _, _ = rig
    _register("windsurf")
    sid, why = renew.startup_choice(store, "x", "windsurf")
    assert sid == "" and "no session reader" in why and "windsurf" in why


def test_startup_import_creates_the_native_session_the_chat_then_loads(rig):
    _, store, _, _ = rig
    native_id, text = renew.startup_import(store, f"claude-code:{CLAUDE_ID}")
    assert native_id and "from claude-code" in text and "source unchanged" in text
    loaded = store.load(native_id)
    assert loaded.context["renewed_from"]["harness"] == "claude-code"
    assert renew.startup_import(store, "codex:019e0000-dead-0000-0000-000000000000")[0] == ""


def test_renew_json_on_the_command_line_prints_the_listing_and_exits(rig, monkeypatch, capsys):
    _, store, _, _ = rig
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "_announce_installed_product", lambda: None)
    monkeypatch.setattr(sys, "argv", ["chat", "--renew", "--harness", "codex", "--json"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"] == "codex" and [s["source"] for s in data["sessions"]] == ["codex"]


def test_renew_with_an_unknown_harness_on_the_command_line_exits_with_the_reason(rig, monkeypatch, capsys):
    _, store, _, _ = rig
    monkeypatch.setattr(cli, "SessionStore", lambda: store)
    monkeypatch.setattr(cli, "_announce_installed_product", lambda: None)
    monkeypatch.setattr(sys, "argv", ["chat", "--renew", "--harness", "nonesuch"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert "nonesuch" in capsys.readouterr().out
