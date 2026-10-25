# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Ctrl+C has to reach a turn that is past its first round.

Cancellation was only ever polled in two places: between streaming chunks, and
between tool executions. Only the FIRST round streams — every round after a
tool call is deliberately non-streaming so the model does not re-render text it
already showed. So once a turn made a tool call, nothing read the cancel flag
again until the turn ended on its own, and Ctrl+C did nothing at all.

The round loop already refuses to START a round that begins past the deadline.
It should equally refuse to start one the user has cancelled. That symmetry is
what these tests pin.

A single in-flight provider call still cannot be interrupted from outside; the
granularity this buys is the round, which is the granularity a tool-using turn
actually has.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.agent import (
    ChatAgent,
    ChatTurnCancelled,
)
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, ToolUseBlock
from axiom.infra.orchestrator.session import Session


@pytest.fixture
def mock_gateway():
    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    return gw


@pytest.fixture
def agent(mock_gateway, tmp_path):
    return ChatAgent(
        gateway=mock_gateway,
        bus=EventBus(log_path=tmp_path / "events.jsonl"),
        session=Session(),
    )


def _cancel_after(scope: ChatScope, rounds: int):
    """A model that asks for a tool each round, and cancels after ``rounds``.

    Stands in for the operator pressing Ctrl+C mid-turn: the flag is set from
    outside while the loop is between rounds, which is exactly what the TUI
    key binding does from its own thread.
    """
    calls = {"n": 0}

    def respond(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= rounds:
            scope.cancel_event.set()
        if not kwargs.get("tools"):
            return CompletionResponse(text="Synthesized answer.", provider="test", success=True)
        return CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
            provider="test",
            success=True,
            stop_reason="tool_use",
        )

    return respond, calls


class TestCancellationReachesLaterRounds:
    def test_a_cancelled_turn_does_not_start_another_round(self, agent, mock_gateway):
        """The defect, stated as a test.

        Without a check at the top of the loop, the turn runs to its round cap
        and the user's Ctrl+C is discovered only when it is already over.
        """
        scope = ChatScope(session=Session(), max_tool_rounds=10)
        respond, calls = _cancel_after(scope, rounds=1)
        mock_gateway.complete_with_tools.side_effect = respond

        with pytest.raises(ChatTurnCancelled):
            agent.turn("do something with tools", stream=False, scope=scope)

        assert calls["n"] <= 2, (
            f"cancelled after round 1 but the model was called {calls['n']} times; "
            "the loop kept starting rounds after the user cancelled"
        )

    def test_cancelling_later_in_the_turn_still_stops_it(self, agent, mock_gateway):
        scope = ChatScope(session=Session(), max_tool_rounds=10)
        respond, calls = _cancel_after(scope, rounds=3)
        mock_gateway.complete_with_tools.side_effect = respond

        with pytest.raises(ChatTurnCancelled):
            agent.turn("do something", stream=False, scope=scope)
        assert calls["n"] <= 4

    def test_an_uncancelled_turn_is_untouched(self, agent, mock_gateway):
        """The guard must not end turns nobody cancelled."""
        scope = ChatScope(session=Session(), max_tool_rounds=10)

        def respond(*args, **kwargs):
            if not kwargs.get("tools"):
                return CompletionResponse(text="Synthesized answer.", provider="test", success=True)
            return CompletionResponse(
                text="",
                tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                provider="test",
                success=True,
                stop_reason="tool_use",
            )

        mock_gateway.complete_with_tools.side_effect = respond
        out = agent.turn("go", stream=False, scope=scope)
        assert "Synthesized answer" in out

    def test_a_stale_cancel_does_not_kill_the_next_turn(self, agent, mock_gateway):
        """The other half of the guard, and the reason it is not stricter.

        ``turn()`` calls ``reset_cancel()`` on entry, so a flag left set by a
        turn the user already cancelled does not silently kill the turn they
        typed next. A round-loop check that fired on entry regardless would
        make the previous Ctrl+C poison the following question, which is a
        worse bug than the one being fixed.
        """
        scope = ChatScope(session=Session(), max_tool_rounds=10)
        scope.cancel_event.set()  # left over from a previous, cancelled turn

        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="Fresh answer.", provider="test", success=True
        )
        assert "Fresh answer" in agent.turn("a new question", stream=False, scope=scope)
        assert not scope.cancel_event.is_set(), "entry must clear the stale flag"


class TestItReadsAsCancellationNotAsSomethingElse:
    def test_it_is_a_cancellation_not_a_deadline(self, agent, mock_gateway):
        """A surface distinguishes these: one is the user, one is the clock."""
        from axiom.extensions.builtins.chat.agent import ChatTurnDeadlineExceeded

        scope = ChatScope(session=Session(), max_tool_rounds=10)
        respond, _ = _cancel_after(scope, rounds=1)
        mock_gateway.complete_with_tools.side_effect = respond

        with pytest.raises(ChatTurnCancelled) as excinfo:
            agent.turn("go", stream=False, scope=scope)
        assert not isinstance(excinfo.value, ChatTurnDeadlineExceeded)


class TestCancellationBeforeTheFirstChunk:
    """The case a person actually hits: Ctrl+C while it says "Thinking".

    You cancel when you are waiting, not once text is already flowing. But the
    cancel poll lived inside ``for c in chunks:``, and that loop does not begin
    until the provider produces its first chunk. On a slow time-to-first-token
    the flag was set and nothing read it, so Ctrl+C did nothing for as long as
    the model took to start speaking — which is exactly the window in which
    somebody presses it.

    The provider call cannot be interrupted from outside. What can be done is
    stop *waiting* on it: pump the stream on a worker thread and let the
    consumer answer the cancel flag while the provider is still thinking. The
    abandoned request finishes into a queue nobody reads.
    """

    def test_a_stream_that_never_starts_is_still_cancellable(self):
        import threading

        from axiom.extensions.builtins.chat.agent import _cancellable_stream

        cancel = threading.Event()
        entered = threading.Event()
        release = threading.Event()

        def never_starts():
            entered.set()
            release.wait(10)  # a provider that has not produced a token yet
            yield "too late"

        stream = _cancellable_stream(never_starts(), cancel, poll=0.01)
        result: dict = {}

        def consume():
            try:
                for _ in stream:
                    pass
            except BaseException as exc:  # noqa: BLE001 — recording it is the test
                result["raised"] = exc

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        assert entered.wait(5), "the producer should have started"
        cancel.set()
        t.join(timeout=5)

        assert not t.is_alive(), "cancel must not wait for the provider"
        assert isinstance(result.get("raised"), ChatTurnCancelled)
        release.set()

    def test_chunks_still_flow_when_nobody_cancels(self):
        import threading

        from axiom.extensions.builtins.chat.agent import _cancellable_stream

        cancel = threading.Event()
        out = list(_cancellable_stream(iter(["a", "b", "c"]), cancel, poll=0.01))
        assert out == ["a", "b", "c"]

    def test_a_provider_error_still_reaches_the_caller(self):
        """Moving the iteration to a thread must not swallow its exception."""
        import threading

        from axiom.extensions.builtins.chat.agent import _cancellable_stream

        cancel = threading.Event()

        def explodes():
            yield "first"
            raise RuntimeError("provider blew up")

        with pytest.raises(RuntimeError, match="provider blew up"):
            list(_cancellable_stream(explodes(), cancel, poll=0.01))

    def test_cancelling_mid_stream_still_works(self):
        import threading

        from axiom.extensions.builtins.chat.agent import _cancellable_stream

        cancel = threading.Event()

        def slow_after_first():
            yield "first"
            threading.Event().wait(10)
            yield "never"

        stream = _cancellable_stream(slow_after_first(), cancel, poll=0.01)
        assert next(stream) == "first"
        cancel.set()
        with pytest.raises(ChatTurnCancelled):
            next(stream)


class TestTheStreamingTurnActuallyUsesIt:
    """The wiring, not the helper.

    A mutant that deleted the wrapper from ``_streaming_turn`` and left the raw
    provider generator in its place passed every other test here, because they
    all exercise ``_cancellable_stream`` directly. An unwired fix is the failure
    this whole change exists to correct, so it gets its own test: drive a real
    streaming turn whose provider has not yet produced a token, and cancel it.
    """

    def test_ctrl_c_during_thinking_ends_a_real_streaming_turn(self, agent, mock_gateway):
        import threading

        scope = ChatScope(session=Session(), max_tool_rounds=10)
        entered = threading.Event()
        release = threading.Event()

        def never_starts(*args, **kwargs):
            def gen():
                entered.set()
                release.wait(10)  # provider still thinking
                yield MagicMock(type="text", text="too late")

            return gen()

        mock_gateway.stream_with_tools.side_effect = never_starts
        agent._render = None
        agent._renderer_callback = lambda chunks: [c for c in chunks]

        result: dict = {}

        def run():
            try:
                agent.turn("a slow question", stream=True, scope=scope)
            except BaseException as exc:  # noqa: BLE001 — recording it is the test
                result["raised"] = exc

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert entered.wait(5), "the provider should have been called"

        scope.cancel_event.set()  # what the TUI key binding does
        t.join(timeout=5)

        assert not t.is_alive(), (
            "cancelling during 'Thinking' must not wait for the provider's "
            "first token — this is the reported defect"
        )
        assert isinstance(result.get("raised"), ChatTurnCancelled)
        release.set()
