# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi chat --ask` — the other door onto the agent the HTTP face already uses.

The CLI was interactive and ONLY interactive: no positional question, no
JSON, and `--no-tui` still a REPL. So a chat surface everybody has could not
be scripted, tested, or included in a smoke run — which is why a
multi-surface report had one column and could not tell whether the surfaces
agreed.

The agent was always reachable headlessly. `chat/api.py` runs a single turn
through `HeadlessChat` for the HTTP face, properly scoped with a turn
deadline. Only the CLI had no door onto it.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.chat import cli as chat_cli


class _Scope:
    def __init__(self, messages=()):
        self.session = type("S", (), {"messages": list(messages)})()


class _Msg:
    def __init__(self, tool_calls=()):
        self.tool_calls = list(tool_calls)


class _Headless:
    """Stands in for HeadlessChat; records how it was driven."""

    last: dict = {}

    def __init__(self, *, gateway=None, turn_deadline=None):
        _Headless.last = {"deadline": turn_deadline}
        self._scope = _Scope([_Msg([{"name": "reactor_status_at"}]), _Msg()])

    def new_scope(self):
        return self._scope

    def turn(self, user_input, *, stream=True, scope=None):
        _Headless.last.update(question=user_input, stream=stream, scope_passed=scope is not None)
        return "A control rod absorbs neutrons."


@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.headless.HeadlessChat", _Headless, raising=False
    )
    monkeypatch.setattr("axiom.infra.gateway.Gateway", lambda: object(), raising=False)
    return _Headless


# --- the door exists --------------------------------------------------------


def test_the_parser_accepts_a_question_and_json():
    args = chat_cli.get_parser().parse_args(["--ask", "what is a rod", "--json"])

    assert args.ask == "what is a rod"
    assert args.json is True


def test_the_interactive_default_is_untouched():
    """Adding a door must not change what happens when nobody uses it."""
    args = chat_cli.get_parser().parse_args([])

    assert args.ask is None
    assert args.json is False


# --- one shot, one scope ----------------------------------------------------


def test_it_answers_and_exits_zero(headless, capsys):
    code = chat_cli.run_one_shot("what is a control rod")

    assert code == 0
    assert "absorbs neutrons" in capsys.readouterr().out


def test_it_runs_through_headless_with_a_turn_deadline(headless):
    """An unbounded turn is the hazard HeadlessChat exists to remove."""
    chat_cli.run_one_shot("q")

    assert headless.last["deadline"] is not None
    assert headless.last["scope_passed"] is True


def test_it_does_not_stream(headless):
    """Nothing is rendering it."""
    chat_cli.run_one_shot("q")

    assert headless.last["stream"] is False


def test_json_carries_the_answer_and_what_produced_it(headless, capsys):
    chat_cli.run_one_shot("q", as_json=True)
    out = json.loads(capsys.readouterr().out)

    assert out["answer"] == "A control rod absorbs neutrons."
    assert out["verbs_called"] == ["reactor_status_at"]
    assert out["latency_ms"] >= 0


def test_json_has_no_citations_key(headless, capsys):
    """Citations come from the serving shim's retrieval layer, not the agent.

    Emitting an empty one would report a field this surface does not have,
    and a consumer would read it as "retrieved nothing".
    """
    chat_cli.run_one_shot("q", as_json=True)

    assert "citations" not in json.loads(capsys.readouterr().out)


# --- failure is an exit code, not a traceback -------------------------------


def test_a_failed_turn_exits_nonzero(monkeypatch, capsys):
    class _Boom(_Headless):
        def turn(self, *a, **kw):
            raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.headless.HeadlessChat", _Boom, raising=False
    )
    monkeypatch.setattr("axiom.infra.gateway.Gateway", lambda: object(), raising=False)

    assert chat_cli.run_one_shot("q") == 1


def test_a_failed_turn_reports_as_json_when_asked(monkeypatch, capsys):
    class _Boom(_Headless):
        def turn(self, *a, **kw):
            raise RuntimeError("gateway unreachable")

    monkeypatch.setattr(
        "axiom.extensions.builtins.chat.headless.HeadlessChat", _Boom, raising=False
    )
    monkeypatch.setattr("axiom.infra.gateway.Gateway", lambda: object(), raising=False)

    chat_cli.run_one_shot("q", as_json=True)

    assert "gateway unreachable" in json.loads(capsys.readouterr().out)["error"]


# --- it is the shape the smoke subject reads --------------------------------


def test_the_json_is_what_clichatsubject_expects(headless, capsys):
    """The CLI surface only joins the smoke run if this shape lines up."""
    chat_cli.run_one_shot("q", as_json=True)
    payload = json.loads(capsys.readouterr().out)

    assert set(payload) >= {"answer", "verbs_called", "latency_ms"}
    assert isinstance(payload["verbs_called"], list)
