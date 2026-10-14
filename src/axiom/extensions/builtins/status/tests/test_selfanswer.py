# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Self-questions answer deterministically, with no model round trip.

The live failure this closes: the facts were in the system prompt, and
a small model with a long tool list still wandered off and took two
minutes. Carrying the facts is necessary; answering without the model
is what makes it fast AND certain.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.status import selfknowledge as sk
from axiom.extensions.builtins.status.selfanswer import answer_about_self


@pytest.fixture(autouse=True)
def _fresh():
    sk.reset_summary_cache()
    yield
    sk.reset_summary_cache()


def test_the_screenshot_question_is_answered_not_speculated(monkeypatch):
    monkeypatch.setattr(
        sk, "_federation", lambda: {"configured": False, "peers": [], "identity": None}
    )
    answer = answer_about_self("am i federated with UT TRIGA?")
    assert answer is not None
    assert "not federated with any peer" in answer
    # The failure mode, explicitly excluded:
    for bad in ("if such a function existed", "we don't have direct access", "typically"):
        assert bad not in answer.lower()


def test_federation_answer_names_peers_the_way_a_human_does(monkeypatch):
    monkeypatch.setattr(
        sk,
        "_federation",
        lambda: {
            "configured": True,
            "peers": [{"name": "peer-a:owner", "node_id": "0e37", "url": "ssh://x"}],
            "identity": None,
        },
    )
    answer = answer_about_self("are you federated with anything?")
    assert "peer-a:owner" in answer
    assert "0e37" not in answer  # the opaque id is not the human answer
    # The negative half must be stated, so "not listed" is answerable.
    assert "anything not listed" in answer


def test_first_person_voice():
    for q in ("what version are you running?", "what site am i on?"):
        a = answer_about_self(q)
        assert a and a.startswith("I")
        assert "the system" not in a.lower()


def test_precision_domain_questions_fall_through_to_the_model():
    for q in (
        "what is a control rod?",
        "summarize last night's backup",
        "how do I federate two reactors in the report?",  # about content, not me
        "",
        "x" * 500,
    ):
        assert answer_about_self(q) is None, q


def test_answers_are_fresh_not_stored(monkeypatch):
    monkeypatch.setattr(
        sk, "_federation", lambda: {"configured": False, "peers": [], "identity": None}
    )
    assert "not federated" in answer_about_self("am i federated?")
    # State changes → the NEXT derivation says so (no stored description).
    monkeypatch.setattr(
        sk,
        "_federation",
        lambda: {
            "configured": True,
            "peers": [{"name": "site-a", "node_id": "n", "url": None}],
            "identity": None,
        },
    )
    sk.reset_summary_cache()
    assert "site-a" in answer_about_self("am i federated?")


def test_agent_short_circuits_without_calling_the_model(monkeypatch):
    """The point of the fast path: no gateway call at all."""
    pytest.importorskip("axiom.extensions.builtins.chat.agent")
    from axiom.extensions.builtins.chat.agent import ChatAgent

    monkeypatch.setattr(
        sk, "_federation", lambda: {"configured": False, "peers": [], "identity": None}
    )

    class ExplodingGateway:
        def __getattr__(self, name):  # any model touch fails the test
            raise AssertionError(f"the model was called ({name}) — fast path missed")

    agent = ChatAgent.__new__(ChatAgent)  # no __init__: we drive _turn_impl only
    sent: list[str] = []

    class Session:
        messages: list = []

        def add_message(self, role, content):
            sent.append((role, content))

    class Scope:
        session = Session()
        session_mode = "default"
        last_turn_tools: list = []
        on_chunk = None
        turn_query = ""
        turn_start = 0.0

    agent.gateway = ExplodingGateway()
    agent._render = None
    agent._renderer_callback = None
    out = ChatAgent._turn_impl(agent, "am i federated with UT TRIGA?", stream=False, scope=Scope())
    assert "not federated with any peer" in out
    assert ("assistant", out) in sent
