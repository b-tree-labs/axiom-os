# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What changes on every turn goes after the history, not before it (RS-P8, ADR-272 D2).

Every wire format sends the system prompt first. The retrieved block, the clock and the person's
screen change on each turn, so while they sit in the system prompt a provider's prompt cache can
never reuse the conversation behind them. With ``chat.prompt.stable_layout`` on, those parts ride as
a labelled tail on the newest user turn and the system prompt stays the same from turn to turn.

The setting ships OFF, because it changes what the model reads every turn and the grounding evals
(RS-P8-A4) have to run on both layouts before the new one is the default. With it off, the system
prompt and the messages are exactly what they have always been.

What is stored when a tail's turn becomes history is the person's words only: the session never sees
the tail, so no earlier turn ever changes.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.extensions.builtins.settings import store as store_mod
from axiom.extensions.builtins.settings.store import SettingsStore
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
from axiom.infra.orchestrator.session import Session
from axiom.infra.prompt_composer import TURN_TAIL_LABEL

SETTING = "chat.prompt.stable_layout"
SCREEN = "--- The person's screen in the web app ---"
CLOCK = "Current date and time:"
RAG = "RAG-BLOCK-FOR"

_START = datetime(2026, 11, 10, 21, 0, tzinfo=UTC)

ANTHROPIC = "https://api.anthropic.com/v1"
OPENAI = "http://localhost:11434/v1"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """One agent on an isolated node: its own settings, no operator files, a clock that moves
    one minute per prompt, and a retrieval block that names the question it answers."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(store_mod, "_PROJECT_SETTINGS_PATH", tmp_path / "project-settings.toml")
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(agent_mod, "_REPO_ROOT", root)

    ticks = iter(range(10_000))
    monkeypatch.setattr(agent_mod, "_wall_clock", lambda: _START + timedelta(minutes=next(ticks)))
    monkeypatch.setattr(
        ChatAgent,
        "_rag_context",
        lambda self, query, limit=4, *, scope=None: f"--- Retrieved ---\n{RAG} [{query}]",
    )

    calls: list[dict] = []

    def answer(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return CompletionResponse(text=f"answer {len(calls)}", provider="test", success=True)

    gateway = MagicMock(spec=Gateway)
    gateway.available = True
    gateway.active_provider = MagicMock()
    gateway.active_provider.name = "test"
    gateway.active_provider.model = "test-model"
    gateway.active_provider.endpoint = OPENAI
    gateway.complete_with_tools.side_effect = answer
    agent = ChatAgent(
        gateway=gateway, bus=EventBus(log_path=tmp_path / "events.jsonl"), session=Session()
    )
    agent.calls = calls
    return agent


def _layout(on: bool) -> None:
    SettingsStore().set(SETTING, on, scope="global")


def _scope_with_screen(agent: ChatAgent):
    scope = agent.scope
    scope.view_context = {"screen": "charts", "route": "/charts"}
    return scope


def _text(content) -> str:
    """The text of a message's content, whether a string or a list of blocks."""
    if isinstance(content, str):
        return content
    return "\n".join(b["text"] for b in content if b.get("type") == "text")


def _history(call: dict) -> list[dict]:
    """The request's messages before its newest user turn: what a provider can cache."""
    return call["messages"][:-1]


# ---------------------------------------------------------------------------
# The switch ships off, and off is today's request, byte for byte
# ---------------------------------------------------------------------------


def test_the_layout_setting_ships_off():
    assert store_mod._DEFAULTS[SETTING] is False


def test_with_the_setting_off_the_system_prompt_and_messages_are_what_main_sends(rig):
    """A turn with retrieved context, a screen and the clock, under the layout every turn has today.

    The expectation is built from the contributions in canonical order, joined the way the flat
    render has always joined them, so it does not depend on the new rendering code.
    """
    scope = _scope_with_screen(rig)

    rig.turn("What changed?", stream=False)

    (call,) = rig.calls
    expected = "\n\n".join(c.content for c in scope.last_composer.debug() if c.content)
    # A node bound to a data platform appends its per-turn notes after the composed prompt, as it
    # always has ("--- DATA ON <site> ---" and the like). Whether this machine is bound is not this
    # test's business, so what follows the composed prompt may be such notes and nothing else.
    assert call["system"].startswith(expected)
    notes = call["system"][len(expected) :]
    assert notes == "" or notes.startswith("\n\n--- "), notes[:200]
    assert TURN_TAIL_LABEL not in call["system"]
    assert call["messages"] == [{"role": "user", "content": "What changed?"}]
    # The three per-request parts are where they have always been: in the system prompt, in
    # layer order (screen, retrieved, clock), after everything stable.
    s = call["system"]
    assert s.index(SCREEN) < s.index(RAG) < s.index(CLOCK)
    assert TURN_TAIL_LABEL not in s


def test_with_the_setting_off_nothing_about_the_tail_reaches_the_model_on_any_turn(rig):
    _scope_with_screen(rig)
    rig.turn("one", stream=False)
    rig.turn("two", stream=False)

    for call in rig.calls:
        assert all(isinstance(m["content"], str) for m in call["messages"])
        assert TURN_TAIL_LABEL not in call["system"]
        assert not any(TURN_TAIL_LABEL in _text(m["content"]) for m in call["messages"])


# ---------------------------------------------------------------------------
# RS-P8-A1: the history is byte-identical from one turn to the next
# ---------------------------------------------------------------------------


def test_two_consecutive_turns_render_byte_identical_up_to_the_end_of_the_history(rig):
    """RS-P8-A1. Screen, clock and retrieved block all differ between the turns."""
    _layout(True)
    _scope_with_screen(rig)

    rig.turn("first question", stream=False)
    rig.turn("second question", stream=False)
    rig.turn("third question", stream=False)

    first, second, third = rig.calls
    for earlier, later in ((first, second), (second, third)):
        assert later["system"] == earlier["system"]
        assert later["messages"][: len(_history(earlier))] == _history(earlier)
    # The comparison above is not vacuous: the newest turns do differ.
    assert _text(first["messages"][-1]["content"]) != _text(second["messages"][-1]["content"])


def test_the_same_two_turns_do_not_match_with_the_setting_off(rig):
    """The control for A1: today's layout changes the system prompt on every turn."""
    _layout(False)
    _scope_with_screen(rig)

    rig.turn("first question", stream=False)
    rig.turn("second question", stream=False)

    first, second = rig.calls
    assert second["system"] != first["system"]


def test_a_turn_that_became_history_is_the_persons_words_only(rig):
    """The tail is dropped from stored history, so an earlier turn never changes."""
    _layout(True)
    _scope_with_screen(rig)

    rig.turn("first question", stream=False)
    rig.turn("second question", stream=False)

    stored = [m.content for m in rig.session.messages if m.role == "user"]
    assert stored == ["first question", "second question"]
    second = rig.calls[1]
    assert second["messages"][0] == {"role": "user", "content": "first question"}


# ---------------------------------------------------------------------------
# RS-P8-A2 and A3: where each part goes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", [ANTHROPIC, OPENAI])
def test_retrieved_live_and_screen_are_in_the_turn_tail_and_never_in_the_system_prompt(
    rig, endpoint
):
    """RS-P8-A2, on both wire formats."""
    rig.gateway.active_provider.endpoint = endpoint
    _layout(True)
    _scope_with_screen(rig)

    rig.turn("What changed?", stream=False)

    (call,) = rig.calls
    for marker in (SCREEN, RAG, CLOCK):
        assert marker not in call["system"]
    newest = _text(call["messages"][-1]["content"])
    assert newest.startswith("What changed?")
    assert TURN_TAIL_LABEL in newest
    for marker in (SCREEN, RAG, CLOCK):
        assert marker in newest


def test_on_the_anthropic_format_the_tail_is_a_separate_content_block(rig):
    rig.gateway.active_provider.endpoint = ANTHROPIC
    _layout(True)
    _scope_with_screen(rig)

    rig.turn("What changed?", stream=False)

    content = rig.calls[0]["messages"][-1]["content"]
    assert [b["type"] for b in content] == ["text", "text"]
    assert content[0]["text"] == "What changed?"
    assert content[1]["text"].startswith(TURN_TAIL_LABEL)


def test_on_the_openai_format_the_labelled_tail_follows_the_words_in_one_string(rig):
    rig.gateway.active_provider.endpoint = OPENAI
    _layout(True)
    _scope_with_screen(rig)

    rig.turn("What changed?", stream=False)

    content = rig.calls[0]["messages"][-1]["content"]
    assert isinstance(content, str)
    assert content.startswith("What changed?\n\n" + TURN_TAIL_LABEL)


def test_an_extension_contribution_that_declares_nothing_renders_in_the_tail(rig, monkeypatch):
    """RS-P8-A3 through the chat agent; one that declares itself a session part stays in system."""
    monkeypatch.setattr(
        agent_mod,
        "_discover_prompt_contributions",
        lambda context=None: [
            {"layer": "domain_context", "name": "undeclared", "content": "NOTE-WITH-NO-SCOPE"},
            {
                "layer": "domain_context",
                "name": "declared",
                "content": "NOTE-SCOPED-SESSION",
                "scope": "session",
            },
        ],
    )
    _layout(True)

    rig.turn("hello", stream=False)

    (call,) = rig.calls
    assert "NOTE-WITH-NO-SCOPE" not in call["system"]
    assert "NOTE-WITH-NO-SCOPE" in _text(call["messages"][-1]["content"])
    assert "NOTE-SCOPED-SESSION" in call["system"]
    assert "NOTE-SCOPED-SESSION" not in _text(call["messages"][-1]["content"])


@pytest.mark.parametrize("on", [False, True])
def test_a_contribution_with_a_scope_nobody_knows_still_composes_and_is_per_request(
    rig, monkeypatch, on
):
    """An extension's typo in ``scope`` never costs a turn its prompt, with the layout on or off."""
    monkeypatch.setattr(
        agent_mod,
        "_discover_prompt_contributions",
        lambda context=None: [
            {
                "layer": "domain_context",
                "name": "typo",
                "content": "NOTE-WITH-A-TYPO",
                "scope": "sesion",
            },
        ],
    )
    _layout(on)

    rig.turn("hello", stream=False)

    (call,) = rig.calls
    in_system = "NOTE-WITH-A-TYPO" in call["system"]
    in_tail = "NOTE-WITH-A-TYPO" in _text(call["messages"][-1]["content"])
    assert (in_system, in_tail) == ((False, True) if on else (True, False))


def test_with_the_setting_off_an_undeclared_contribution_stays_in_the_system_prompt(
    rig, monkeypatch
):
    monkeypatch.setattr(
        agent_mod,
        "_discover_prompt_contributions",
        lambda context=None: [
            {"layer": "domain_context", "name": "undeclared", "content": "NOTE-WITH-NO-SCOPE"}
        ],
    )
    _layout(False)

    rig.turn("hello", stream=False)

    assert "NOTE-WITH-NO-SCOPE" in rig.calls[0]["system"]


def test_the_tools_go_in_a_fixed_order_sorted_by_name(rig):
    _layout(True)

    rig.turn("hello", stream=False)

    names = [t["function"]["name"] for t in rig.calls[0]["tools"]]
    assert names == sorted(names)
    assert len(names) > 1


# ---------------------------------------------------------------------------
# Tool rounds and streaming
# ---------------------------------------------------------------------------


def test_a_tool_round_keeps_one_tail_on_the_user_turn_and_adds_none(rig):
    """The tail is not repeated on each round, and the tool exchange stays behind the user turn."""
    _layout(True)
    _scope_with_screen(rig)
    calls = rig.calls

    def respond(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        if len(calls) == 1:
            return CompletionResponse(
                text="checking",
                tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                provider="test",
                success=True,
                stop_reason="tool_use",
            )
        return CompletionResponse(text="done", provider="test", success=True)

    rig.gateway.complete_with_tools.side_effect = respond

    rig.turn("which providers?", stream=False)

    first, second = calls
    assert second["system"] == first["system"]
    assert second["messages"][0] == first["messages"][0]
    assert [m["role"] for m in second["messages"]] == ["user", "assistant", "tool"]
    carrying = [m for m in second["messages"] if TURN_TAIL_LABEL in _text(m["content"])]
    assert len(carrying) == 1 and carrying[0] is second["messages"][0]
    assert _text(second["messages"][0]["content"]).count(TURN_TAIL_LABEL) == 1


def test_a_streamed_turn_is_given_the_same_system_prompt_and_messages(rig, monkeypatch):
    _layout(True)
    _scope_with_screen(rig)
    seen: list[dict] = []

    def stream(messages, system, tools, routing_tier="any", *, scope=None):
        seen.append(copy.deepcopy({"messages": messages, "system": system}))
        return CompletionResponse(text="streamed", provider="test", success=True)

    monkeypatch.setattr(rig, "_streaming_turn", stream)
    rig.scope.on_chunk = lambda chunk: None

    rig.turn("What changed?", stream=True)
    rig.turn("And then?", stream=False)

    (streamed,) = seen
    assert streamed["messages"][-1]["content"].startswith("What changed?\n\n" + TURN_TAIL_LABEL)
    for marker in (SCREEN, RAG, CLOCK):
        assert marker not in streamed["system"]
    assert rig.calls[0]["system"] == streamed["system"]


def test_a_turn_with_an_image_keeps_the_tail_after_the_image_and_the_words(rig):
    rig.gateway.active_provider.endpoint = ANTHROPIC
    from axiom.extensions.builtins.chat.attachments import ImageAttachment

    _layout(True)
    rig.scope.pending_images = [
        ImageAttachment(path=Path("a.png"), media_type="image/png", b64_data="iVBORw0KGgo=")
    ]

    rig.turn("what is this?", stream=False)

    content = rig.calls[0]["messages"][-1]["content"]
    assert content[0]["type"] == "image"
    assert content[-2]["text"] == "what is this?"
    assert content[-1]["text"].startswith(TURN_TAIL_LABEL)


# ---------------------------------------------------------------------------
# Resume and the legacy path
# ---------------------------------------------------------------------------


def test_a_resumed_session_sends_the_stored_history_without_any_tail(rig):
    """The history comes back from the stored words, so no tail is ever replayed."""
    _layout(True)
    _scope_with_screen(rig)
    rig.turn("first question", stream=False)
    stored = [(m.role, m.content) for m in rig.session.messages]

    resumed = Session()
    for role, content in stored:
        resumed.add_message(role, content)
    rig.scope.session = resumed
    rig.turn("second question", stream=False)

    second = rig.calls[1]
    assert second["messages"][:2] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "answer 1"},
    ]
    assert not any(TURN_TAIL_LABEL in _text(m["content"]) for m in _history(second))
