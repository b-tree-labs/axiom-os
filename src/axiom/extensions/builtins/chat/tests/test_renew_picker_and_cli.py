# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The TUI's session picker as /renew drives it (real FullScreenChat, real keys), and the --renew flag."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from axiom_tests.fixtures.tui import key_sequence

from axiom.extensions.builtins.chat import cli
from axiom.extensions.builtins.chat.chooser import selection
from axiom.extensions.builtins.chat.commands_ext import renew
from axiom.extensions.builtins.chat.fullscreen import FullScreenChat, PickerMode
from axiom.infra.orchestrator.session import Session

from ._renew_support import ME, SOMEONE_ELSE, Agent, make_session, new_store


def _build(factory, store, agent):
    agent.cancel = lambda: None
    agent.allowlisted_tools = lambda: []
    tui = FullScreenChat(agent=agent, store=store, stream=True)
    app = tui._build_app()
    factory(app, columns=100, rows=30)
    tui._app = app
    return tui


def _key(tui, *keys):
    """The handler prompt_toolkit would run for ``keys`` while the picker is open."""
    match = None
    for b in tui._app.key_bindings.bindings:
        if key_sequence(b) != tuple(keys):
            continue
        try:
            live = bool(b.filter())
        except Exception:  # noqa: BLE001
            live = True
        if live:
            match = b.handler
    assert match is not None, keys
    return match


_KEYS = {"enter": "c-m", "backspace": "c-h"}  # prompt_toolkit's names for these keys


def _press(tui, key, data=None):
    handler = _key(tui, _KEYS.get(key, key) if data is None else "<any>")
    handler(SimpleNamespace(data=data if data is not None else key, app=tui._app, current_buffer=tui._input_buffer))


@pytest.fixture
def rig(tmp_path, headless_terminal_factory):
    store = new_store(tmp_path)
    make_session(store, "aaaa11110001", "Fuel temperature report", hours_ago=2,
                 context={"last_provider": "node-qwen", "content_tier": "export_controlled"})
    make_session(store, "aaaa11110002", "Calibration of the detector", hours_ago=30)
    make_session(store, "zzzz99990001", "someone else's session", principal=SOMEONE_ELSE, hours_ago=1)
    agent = Agent(Session(principal_id=ME))
    return _build(headless_terminal_factory, store, agent), store, agent


def test_bare_renew_opens_the_session_picker_with_only_my_sessions(rig):
    tui, _, _ = rig
    tui._handle_slash_command("/renew")
    assert tui._picker is not None and tui._picker.mode is PickerMode.SELECT
    ids = [c.value for c in tui._chooser.choices]
    assert ids == ["aaaa11110001", "aaaa11110002"]  # most recent first; the stranger's is absent
    row = " ".join(tui._chooser.choices[0].columns)
    assert "node-qwen" in row and "[CONTROLLED]" in row and "2h ago" in row and "2 msgs" in row


def test_bare_resume_opens_the_same_picker(rig):
    tui, _, _ = rig
    tui._handle_slash_command("/resume")
    assert tui._picker.mode is PickerMode.SELECT and len(tui._chooser.choices) == 2


def test_typing_filters_the_list_and_enter_renews_the_highlighted_row(rig):
    tui, _, agent = rig
    tui._handle_slash_command("/renew")
    for ch in "calib":
        _press(tui, "<any>", ch)
    assert [c.value for c in tui._chooser.choices] == ["aaaa11110002"]
    assert "filter: calib" in tui._chooser.title
    _press(tui, "enter")
    assert agent.session.session_id == "aaaa11110002"
    assert tui._picker is None
    assert "renewed Calibration of the detector" in tui._output_buffer.text


def test_backspace_widens_the_filter_again(rig):
    tui, _, _ = rig
    tui._handle_slash_command("/renew")
    for ch in "calib":
        _press(tui, "<any>", ch)
    for _ in range(5):
        _press(tui, "backspace")
    assert len(tui._chooser.choices) == 2


def test_a_keystroke_that_would_empty_the_list_is_ignored(rig):
    tui, _, _ = rig
    tui._handle_slash_command("/renew")
    _press(tui, "<any>", "q")
    _press(tui, "<any>", "q")
    assert tui._picker.query == "" and len(tui._chooser.choices) == 2


def test_arrow_then_enter_renews_the_second_row(rig):
    tui, _, agent = rig
    tui._handle_slash_command("/renew")
    _press(tui, "down")
    assert selection(tui._chooser) == ["aaaa11110002"]
    _press(tui, "enter")
    assert agent.session.session_id == "aaaa11110002"


def test_renew_with_an_argument_resumes_directly_and_shows_the_history(rig):
    tui, _, agent = rig
    tui._handle_slash_command("/renew fuel temp")
    assert tui._picker is None
    assert agent.session.session_id == "aaaa11110001"
    assert "renewed Fuel temperature report" in tui._output_buffer.text
    assert "Fuel temperature report message 0" in tui._output_buffer.text  # the history was rendered


def test_all_includes_archived_sessions_in_the_picker(rig):
    tui, store, _ = rig
    store.archive("aaaa11110002")
    tui._handle_slash_command("/renew")
    assert [c.value for c in tui._chooser.choices] == ["aaaa11110001"]
    tui._picker = None
    tui._handle_slash_command("/renew --all")
    assert {c.value for c in tui._chooser.choices} == {"aaaa11110001", "aaaa11110002"}


# ---- --renew on the command line ----------------------------------------------------------------


def test_the_parser_accepts_renew_bare_and_with_a_value_and_keeps_resume():
    parser = cli.get_parser()
    assert parser.parse_args(["--renew"]).renew == "__pick__"
    assert parser.parse_args(["--renew", "calib"]).renew == "calib"
    assert parser.parse_args([]).renew is None
    assert parser.parse_args(["--resume", "abc"]).resume == "abc"
    assert parser.parse_args(["--resume"]).resume == "__pick__"


def test_startup_choice_resolves_through_the_same_scoped_rows(tmp_path, monkeypatch):
    store = new_store(tmp_path)
    make_session(store, "mine00000001", "reactor notes")
    make_session(store, "theirs000001", "reactor secrets", principal=SOMEONE_ELSE)
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    assert renew.startup_choice(store, "__pick__") == ("__pick__", "")
    assert renew.startup_choice(store, "notes") == ("mine00000001", "")
    sid, why = renew.startup_choice(store, "secrets")
    assert sid == "" and "no session matches" in why
    sid, why = renew.startup_choice(store, "theirs")
    assert sid == "" and "no session matches" in why
    sid, why = renew.startup_choice(store, "reactor")
    assert sid == "mine00000001"


def test_startup_choice_ambiguity_resumes_nothing(tmp_path, monkeypatch):
    store = new_store(tmp_path)
    make_session(store, "a0000000001x", "fuel one")
    make_session(store, "a0000000002x", "fuel two")
    monkeypatch.setattr("axiom.infra.orchestrator.session._default_principal", lambda: ME)
    sid, why = renew.startup_choice(store, "fuel")
    assert sid == "" and "fuel one" in why and "fuel two" in why
