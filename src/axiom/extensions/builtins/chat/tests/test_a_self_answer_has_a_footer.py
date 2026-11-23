# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A reply the install answers from its own facts (no model) still says where it came from.

"Am I connected to the site?" is answered from the binding in milliseconds. That reply used to end
with no footer at all (the footer is emitted after a model turn), and so with no test-mode colour
either. Now it names the data and document sources, says no model answered and what did, and is
coloured in test mode like every other reply.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import where
from axiom.infra.serving_mode import SAMPLE_DATA_ENV


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv(SAMPLE_DATA_ENV, raising=False)

    class _S:
        def get(self, key, default=None):
            return default

    monkeypatch.setattr("axiom.extensions.builtins.settings.store.SettingsStore", lambda: _S())


def _agent(
    monkeypatch, question_answer=("Connected to alpha-site at node.example.org.", "binding.status")
):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.status import selfanswer
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import Gateway
    from axiom.infra.orchestrator.session import Session

    monkeypatch.setattr(selfanswer, "answer_with_source", lambda text: question_answer)
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.last_selected = None
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    gw.preview_provider.return_value = None
    agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session())
    render = MagicMock()
    agent.set_render_provider(render)
    return agent, render


def test_a_self_answer_renders_a_footer_that_names_the_sources_and_no_model(monkeypatch):
    agent, render = _agent(monkeypatch)
    reply = agent.turn("am I connected to the site?")
    assert reply.startswith("Connected to alpha-site")
    render.render_status.assert_called_once()
    d = render.render_status.call_args.kwargs["where"]
    stamp = where.stamp_from_dict(d)
    assert "data: " in stamp and "documents: " in stamp
    assert "answered from this install's own facts: binding.status" in stamp
    assert "?" not in stamp


def test_a_test_mode_self_answer_is_marked_test_mode(monkeypatch):
    monkeypatch.setenv(SAMPLE_DATA_ENV, "1")
    agent, render = _agent(monkeypatch)
    agent.turn("am I connected to the site?")
    d = render.render_status.call_args.kwargs["where"]
    assert where.is_test_mode(d) and where.stamp_from_dict(d).startswith("TEST MODE")


def test_the_one_shot_footer_and_verbs_credit_the_self_answer(monkeypatch):
    from types import SimpleNamespace

    from axiom.extensions.builtins.chat.commands.cli import _answer_footer

    scope = SimpleNamespace(
        session=SimpleNamespace(
            context={"turn_data": {"self_answered_by": "binding.status"}}, messages=[]
        )
    )
    gw = MagicMock()
    gw.last_selected = None
    gw.preview_provider.return_value = None
    footer, d = _answer_footer(gw, scope)
    assert footer.startswith("↳ data: ") and "binding.status" in footer
    assert d["answered_by"] == "binding.status" and d["model"] == ""
