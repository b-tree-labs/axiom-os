# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One permission surface: every front end shows and parses the same approval choices.

The full-screen UI, the bare renderer and both line-REPL render providers must
map identical keystrokes to identical outcomes, and their legends must offer
the same five choices. Colour may differ; vocabulary may not.
"""

from __future__ import annotations

import re

import pytest

from axiom.extensions.builtins.chat.permissions import (
    APPROVAL_LEGEND,
    APPROVAL_RETRY,
    parse_approval_choice,
)
from axiom.extensions.builtins.chat.providers.ansi_render import AnsiRenderProvider
from axiom.extensions.builtins.chat.providers.rich_render import RichRenderProvider
from axiom.extensions.builtins.chat.renderer import render_approval_prompt
from axiom.infra.orchestrator.actions import create_action

from .test_tui_probes import _build_tui

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# What the operator types -> what the agent loop receives. Same on every surface.
CASES = [
    ("a", "a"),
    ("approve", "a"),
    ("APPROVE", "a"),
    ("A", "A"),
    ("r", "r"),
    ("reject", "r"),
    ("s", "r"),
    ("skip", "r"),
    ("D", "D"),
    ("  a  ", "a"),
]
INVALID = ["", "x", "yes", "d", "always"]


def _action():
    return create_action("write_file", {"path": "/tmp/x"})


def _console_surfaces():
    """(name, callable(action) -> choice) for every console front end."""
    return [
        ("renderer", render_approval_prompt),
        ("ansi", AnsiRenderProvider().render_approval_prompt),
        ("rich", RichRenderProvider().render_approval_prompt),
    ]


class TestParser:
    @pytest.mark.parametrize(("raw", "expected"), CASES)
    def test_maps_every_accepted_answer(self, raw, expected):
        assert parse_approval_choice(raw) == expected

    @pytest.mark.parametrize("raw", INVALID)
    def test_rejects_everything_else(self, raw):
        assert parse_approval_choice(raw) is None

    def test_legend_and_retry_name_the_same_five_keys(self):
        keys = [k for k, _ in APPROVAL_LEGEND]
        assert keys == ["a", "A", "r", "D", "s"]
        for key in keys:
            assert f"[{key}]" in APPROVAL_RETRY


class TestConsoleSurfaces:
    @pytest.mark.parametrize(("raw", "expected"), CASES)
    def test_same_answer_gives_same_choice_everywhere(self, raw, expected, monkeypatch):
        for name, prompt in _console_surfaces():
            answers = iter([raw])
            monkeypatch.setattr("builtins.input", lambda _p="", _a=answers: next(_a))
            assert prompt(_action()) == expected, name

    def test_invalid_answer_reprompts_then_accepts(self, monkeypatch, capsys):
        for name, prompt in _console_surfaces():
            answers = iter(["x", "D"])
            monkeypatch.setattr("builtins.input", lambda _p="", _a=answers: next(_a))
            assert prompt(_action()) == "D", name
            assert APPROVAL_RETRY in _ANSI.sub("", capsys.readouterr().out), name

    def test_eof_is_a_reject_not_an_approve(self, monkeypatch):
        def _eof(_p=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        for name, prompt in _console_surfaces():
            assert prompt(_action()) == "r", name

    def test_every_legend_offers_all_five_choices(self, monkeypatch, capsys):
        monkeypatch.setattr("builtins.input", lambda _p="": "a")
        for name, prompt in _console_surfaces():
            prompt(_action())
            shown = _ANSI.sub("", capsys.readouterr().out)
            for key, _ in APPROVAL_LEGEND:
                assert f"[{key}]" in shown, f"{name} legend lacks [{key}]"


class TestFullScreenSurface:
    @pytest.mark.parametrize(("raw", "expected"), CASES)
    def test_same_answer_gives_same_choice(self, raw, expected, headless_terminal_factory):
        from axiom.extensions.builtins.chat.fullscreen import _ApprovalRequest

        tui, _ = _build_tui(headless_terminal_factory)
        req = _ApprovalRequest(action=_action())
        tui._approval_pending = req
        tui._handle_approval_input(raw)
        assert req.choice == expected
        assert req.event.is_set()
        assert tui._approval_pending is None

    @pytest.mark.parametrize("raw", INVALID)
    def test_invalid_answer_reprompts_and_keeps_waiting(self, raw, headless_terminal_factory):
        from axiom.extensions.builtins.chat.fullscreen import _ApprovalRequest

        tui, _ = _build_tui(headless_terminal_factory)
        req = _ApprovalRequest(action=_action())
        tui._approval_pending = req
        tui._handle_approval_input(raw)
        assert not req.event.is_set()
        assert tui._approval_pending is req
        assert APPROVAL_RETRY in tui._output_buffer.text

    def test_legend_offers_all_five_choices(self, headless_terminal_factory):
        import threading

        from axiom.extensions.builtins.chat.fullscreen import _TuiRenderProvider

        tui, _ = _build_tui(headless_terminal_factory)
        provider = _TuiRenderProvider(tui)
        # render_approval_prompt blocks until answered; answer from a helper thread.
        threading.Timer(0.05, lambda: tui._handle_approval_input("a")).start()
        assert provider.render_approval_prompt(_action()) == "a"
        for key, _ in APPROVAL_LEGEND:
            assert f"[{key}]" in tui._output_buffer.text
