# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every reply's footer names the data source and the document source plainly, and test mode shows in colour.

No question marks: a guess is said in words ("(unverified)", "location unknown"), never as ``?``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat import where
from axiom.extensions.builtins.data_platform import read_target
from axiom.infra.orchestrator.session import Session
from tests.test_gateway import _gateway_with_providers, _make_provider

FACE = "https://node.example.org"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    for name in ("DATABASE_URL", "DP1_RAG_DSN", "AXIOM_DB_URL", where.SAMPLE_DATA_ENV):
        monkeypatch.delenv(name, raising=False)

    class _S:
        def get(self, key, default=None):
            return default

    monkeypatch.setattr("axiom.extensions.builtins.settings.store.SettingsStore", lambda: _S())
    monkeypatch.setattr(read_target, "holds_medallion", lambda: False)
    return state


def _bind(state) -> None:
    (state / read_target.STATE_FILENAME).write_text(
        json.dumps(
            {
                "current": "alpha-site",
                "bindings": {"alpha-site": {"site": "alpha-site", "face_url": FACE}},
            }
        ),
        encoding="utf-8",
    )


def _report(monkeypatch, endpoint="https://llm.somewhere.example/v1"):
    provider = _make_provider(name="guess", endpoint=endpoint, priority=1)
    agent = SimpleNamespace(
        gateway=_gateway_with_providers([provider], monkeypatch), session=Session()
    )
    agent.gateway._select_provider("chat")
    return where.where_report(agent)


def _every_text(report):
    d = where.to_dict(report)
    return [
        where.render_stamp(report),
        where.stamp_from_dict(d),
        where.label_from_dict(d),
        where.render_block(report),
        where.render_indicator(report),
    ]


@pytest.mark.parametrize("state", ["local", "bound", "test"])
def test_no_footer_or_label_contains_a_question_mark(isolated, monkeypatch, state):
    if state == "bound":
        _bind(isolated)
    if state == "test":
        monkeypatch.setenv(where.SAMPLE_DATA_ENV, "1")
    for text in _every_text(_report(monkeypatch)):
        assert "?" not in text, text


def test_an_unknown_placement_is_said_in_words():
    d = {"placement": "somewhere-new", "host": "", "verified": False}
    assert where.label_from_dict(d) == "location unknown (unverified)"


def test_an_unverified_remote_is_said_in_words():
    d = {"placement": "remote-node", "host": "h.example", "verified": False}
    assert where.label_from_dict(d) == "REMOTE h.example (unverified)"


def test_the_banner_and_model_line_do_not_guess(isolated, monkeypatch):
    from axiom.extensions.builtins.chat import bound_model
    from axiom.extensions.builtins.chat.start_context import start_context_lines

    _bind(isolated)
    monkeypatch.setattr(bound_model, "_CHOSEN", {"model": "m"})  # no "where" recorded
    assert "?" not in where.banner_line()
    monkeypatch.setattr(bound_model, "_CHOSEN", {})
    assert "?" not in where.banner_line()
    provider = SimpleNamespace(model="", name="", endpoint="")
    assert not any(
        "?" in line for line in start_context_lines(provider, brand=SimpleNamespace(cli_name="axi"))
    )


def test_the_footer_names_both_sources_in_every_mode(isolated, monkeypatch):
    local = where.render_stamp(_report(monkeypatch))
    assert "data: " in local and "documents: " in local and "model: " in local
    _bind(isolated)
    bound = where.render_stamp(_report(monkeypatch))
    assert (
        "data: alpha-site via node.example.org" in bound and "documents: node.example.org" in bound
    )


# ---- test mode in colour, on every reply -----------------------------------------------------


def test_is_test_mode_reads_the_report(isolated, monkeypatch):
    assert not where.is_test_mode(where.to_dict(_report(monkeypatch)))
    monkeypatch.setenv(where.SAMPLE_DATA_ENV, "1")
    assert where.is_test_mode(where.to_dict(_report(monkeypatch)))
    assert not where.is_test_mode(None) and not where.is_test_mode({})


def _stamp_dict(monkeypatch, test: bool):
    if test:
        monkeypatch.setenv(where.SAMPLE_DATA_ENV, "1")
    # A model on this machine, so a remote placement's own colour cannot stand in for test mode's.
    return where.to_dict(_report(monkeypatch, endpoint="http://localhost:11434/v1"))


def test_the_terminal_renderer_colours_a_test_mode_reply(monkeypatch, capsys):
    from axiom.extensions.builtins.chat.providers import ansi_render

    monkeypatch.setattr(ansi_render, "_c", lambda colour, text: f"<{colour}>{text}</>")
    r = ansi_render.AnsiRenderProvider()
    r.render_status("m", 1, 2, 0.0, None, _stamp_dict(monkeypatch, True))
    out = capsys.readouterr().out
    footer = [ln for ln in out.splitlines() if "↳" in ln][0]
    assert ansi_render._Colors.YELLOW in footer and "TEST MODE" in footer


def test_the_terminal_renderer_does_not_colour_a_real_reply(monkeypatch, capsys):
    from axiom.extensions.builtins.chat.providers import ansi_render

    monkeypatch.setattr(ansi_render, "_c", lambda colour, text: f"<{colour}>{text}</>")
    r = ansi_render.AnsiRenderProvider()
    r.render_status("m", 1, 2, 0.0, None, _stamp_dict(monkeypatch, False))
    footer = [ln for ln in capsys.readouterr().out.splitlines() if "↳" in ln][0]
    assert ansi_render._Colors.YELLOW not in footer


def test_the_rich_renderer_marks_a_test_mode_reply_bold_yellow(monkeypatch):
    from axiom.extensions.builtins.chat.providers import rich_render

    r = rich_render.RichRenderProvider.__new__(rich_render.RichRenderProvider)
    printed = []
    r.console = SimpleNamespace(print=lambda *a, **k: printed.append(a[0]))
    r.render_status("m", 1, 2, 0.0, None, _stamp_dict(monkeypatch, True))
    assert any(
        isinstance(p, str) and p.startswith("  [bold yellow]") and "TEST MODE" in p for p in printed
    )


def test_the_full_screen_view_styles_a_test_mode_stamp_as_a_warning():
    from axiom.extensions.builtins.chat.fullscreen import _OutputLexer

    lexer = _OutputLexer.__new__(_OutputLexer)
    styled = lexer._style_line("  ↳ TEST MODE (invented sample data) · data: invented sample data")
    assert styled[0][0] == "class:warning"
