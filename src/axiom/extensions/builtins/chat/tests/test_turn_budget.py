# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A turn cannot run forever, and the surface sets the bound.

Two budgets bound one turn: how many tool rounds it may run, and how many
wall-clock seconds it may take. Both live on the ``ChatScope`` rather than on
the agent, because a serving worker holds one agent and answers many requests
with it, and a request from a web mount wants a tighter bound than the one an
operator sitting at a terminal wants.

These tests pin four things:

* the defaults are exactly today's behaviour (ten rounds, no time bound), so
  no existing caller changes;
* a per-scope budget is honoured, and two scopes on one agent can differ;
* time is checked before each round and between streaming chunks, so a single
  slow round cannot outlast the bound;
* running out of time degrades gracefully and reads differently from running
  out of rounds, and neither is a cancellation.

The clock is injected. No test here sleeps.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat.agent import (
    DEADLINE_MESSAGE,
    MAX_TOOL_ROUNDS,
    ROUNDS_EXHAUSTED_MESSAGE,
    ChatAgent,
    ChatTurnCancelled,
    ChatTurnDeadlineExceeded,
)
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.infra.bus import EventBus
from axiom.infra.gateway import CompletionResponse, Gateway, StreamChunk, ToolUseBlock
from axiom.infra.orchestrator.session import Session

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


class FakeClock:
    """A monotonic clock the test moves by hand, never by sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """Replace the agent's one clock seam so a turn's elapsed time is scripted."""
    fake = FakeClock()
    monkeypatch.setattr(agent_mod, "_now", fake)
    return fake


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


def _tool_spammer(clock: FakeClock | None = None, cost: float = 0.0):
    """A model that asks for another tool call whenever it is offered tools.

    Offered none, it synthesizes an answer from what it has. ``cost`` is the
    wall-clock time each call to it burns, so a test can script a slow model.
    """

    def respond(*args, **kwargs):
        if clock is not None:
            clock.advance(cost)
        if not kwargs.get("tools"):
            return CompletionResponse(
                text="Synthesized answer from gathered context.",
                provider="test",
                success=True,
            )
        return CompletionResponse(
            text="",
            tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
            provider="test",
            success=True,
            stop_reason="tool_use",
        )

    return respond


def _last_tools(mock_gateway) -> object:
    return mock_gateway.complete_with_tools.call_args.kwargs.get("tools")


# ---------------------------------------------------------------------------
# Defaults: nothing changes for an existing caller
# ---------------------------------------------------------------------------


class TestDefaultsAreTodaysBehaviour:
    def test_default_round_budget_is_the_module_constant(self):
        """A scope built with no arguments carries today's round cap."""
        assert ChatScope().max_tool_rounds == MAX_TOOL_ROUNDS
        assert MAX_TOOL_ROUNDS == 10

    def test_the_constant_is_still_importable_from_the_agent(self):
        """Callers that import the old name keep working, and it is one value."""
        from axiom.extensions.builtins.chat.scope import (
            MAX_TOOL_ROUNDS as scope_constant,
        )

        assert agent_mod.MAX_TOOL_ROUNDS is scope_constant

    def test_default_scope_has_no_time_bound(self):
        """No deadline means exactly today's behaviour: no wall-clock bound."""
        assert ChatScope().turn_deadline is None

    def test_the_agents_default_scope_carries_the_defaults(self, agent):
        assert agent.scope.max_tool_rounds == MAX_TOOL_ROUNDS
        assert agent.scope.turn_deadline is None

    def test_no_deadline_runs_the_full_round_budget(self, agent, mock_gateway, clock):
        """With no deadline a tool-spamming model still gets all ten rounds.

        The clock is running fast here (an hour per model call) and changes
        nothing, which is the point: an unbounded scope is unbounded.
        """
        mock_gateway.complete_with_tools.side_effect = _tool_spammer(clock, cost=3600.0)

        response = agent.turn("Keep searching forever.", stream=False)

        assert mock_gateway.complete_with_tools.call_count == MAX_TOOL_ROUNDS
        assert "Synthesized answer" in response
        assert DEADLINE_MESSAGE not in response


# ---------------------------------------------------------------------------
# The round budget belongs to the scope
# ---------------------------------------------------------------------------


class TestRoundBudgetIsPerScope:
    def test_a_scope_can_run_fewer_rounds_than_the_default(self, agent, mock_gateway):
        mock_gateway.complete_with_tools.side_effect = _tool_spammer()
        scope = ChatScope(max_tool_rounds=3)

        response = agent.turn("Keep searching.", stream=False, scope=scope)

        assert mock_gateway.complete_with_tools.call_count == 3
        assert "Synthesized answer" in response

    def test_two_scopes_on_one_agent_carry_different_budgets(self, agent, mock_gateway):
        """A worker's tight budget never becomes an operator's budget."""
        tight = ChatScope(max_tool_rounds=2)
        roomy = ChatScope(max_tool_rounds=5)

        mock_gateway.complete_with_tools.side_effect = _tool_spammer()
        agent.turn("Keep searching.", stream=False, scope=tight)
        assert mock_gateway.complete_with_tools.call_count == 2

        mock_gateway.complete_with_tools.reset_mock()
        mock_gateway.complete_with_tools.side_effect = _tool_spammer()
        agent.turn("Keep searching.", stream=False, scope=roomy)
        assert mock_gateway.complete_with_tools.call_count == 5

        assert tight.max_tool_rounds == 2 and roomy.max_tool_rounds == 5

    def test_a_one_round_scope_offers_no_tools_at_all(self, agent, mock_gateway):
        """One round is the last round, so the tool surface is withheld."""
        mock_gateway.complete_with_tools.side_effect = _tool_spammer()

        response = agent.turn("Answer now.", stream=False, scope=ChatScope(max_tool_rounds=1))

        assert mock_gateway.complete_with_tools.call_count == 1
        assert _last_tools(mock_gateway) in (None, [])
        assert "Synthesized answer" in response


# ---------------------------------------------------------------------------
# The deadline
# ---------------------------------------------------------------------------


class TestDeadlineStopsTheTurn:
    def test_a_turn_that_runs_out_of_time_stops_and_says_so(self, agent, mock_gateway, clock):
        """One model call outlasts the whole budget: the next round never starts."""
        mock_gateway.complete_with_tools.side_effect = _tool_spammer(clock, cost=10.0)
        scope = ChatScope(turn_deadline=5.0)

        response = agent.turn("Keep searching.", stream=False, scope=scope)

        assert mock_gateway.complete_with_tools.call_count == 1
        assert DEADLINE_MESSAGE in response

    def test_the_time_message_differs_from_the_rounds_message(self, agent, mock_gateway, clock):
        """A reader can tell which budget ran out, because the sentences differ."""
        assert DEADLINE_MESSAGE != ROUNDS_EXHAUSTED_MESSAGE

        mock_gateway.complete_with_tools.side_effect = _tool_spammer(clock, cost=10.0)
        timed_out = agent.turn("Search.", stream=False, scope=ChatScope(turn_deadline=5.0))

        # A model that asks for tools even when offered none exhausts rounds.
        def always_tools(*args, **kwargs):
            return CompletionResponse(
                text="",
                tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                provider="test",
                success=True,
                stop_reason="tool_use",
            )

        mock_gateway.complete_with_tools.side_effect = always_tools
        out_of_rounds = agent.turn("Search.", stream=False, scope=ChatScope(max_tool_rounds=2))

        assert ROUNDS_EXHAUSTED_MESSAGE in out_of_rounds
        assert DEADLINE_MESSAGE not in out_of_rounds
        assert DEADLINE_MESSAGE in timed_out
        assert ROUNDS_EXHAUSTED_MESSAGE not in timed_out

    def test_partial_text_survives_the_timeout(self, agent, mock_gateway, clock):
        """What the model managed to say is kept, with the notice appended."""

        def slow_partial(*args, **kwargs):
            clock.advance(10.0)
            return CompletionResponse(
                text="Here is what I found so far.",
                tool_use=[ToolUseBlock(tool_id="t1", name="list_providers", input={})],
                provider="test",
                success=True,
                stop_reason="tool_use",
            )

        mock_gateway.complete_with_tools.side_effect = slow_partial

        response = agent.turn("Search.", stream=False, scope=ChatScope(turn_deadline=4.0))

        assert "Here is what I found so far." in response
        assert DEADLINE_MESSAGE in response

    def test_the_timeout_is_recorded_in_the_conversation(self, agent, mock_gateway, clock):
        """The turn ends with an assistant message, like every other ending."""
        mock_gateway.complete_with_tools.side_effect = _tool_spammer(clock, cost=10.0)
        scope = ChatScope(turn_deadline=5.0)

        agent.turn("Search.", stream=False, scope=scope)

        assert scope.session.messages[-1].role == "assistant"
        assert DEADLINE_MESSAGE in scope.session.messages[-1].content


class TestDeadlineIsCheckedBetweenStreamingChunks:
    def test_a_long_stream_is_cut_at_the_deadline(self, agent, mock_gateway, clock):
        """One round can be long on its own, so time is checked inside it."""
        consumed: list[StreamChunk] = []

        def counting_renderer(chunks_iter):
            for c in chunks_iter:
                consumed.append(c)
            return ""

        agent.set_renderer(counting_renderer)

        def long_stream():
            # Bounded so a lost deadline check fails this test instead of
            # hanging the suite, but far longer than the budget allows.
            for _ in range(5000):
                clock.advance(1.0)
                yield StreamChunk(type="text", text="tick")

        mock_gateway.stream_with_tools.return_value = long_stream()

        response = agent.turn(
            "Talk for a long time.",
            stream=True,
            scope=ChatScope(turn_deadline=4.0, max_tool_rounds=5),
        )

        assert 0 < len(consumed) <= 6, "the stream ran past its deadline"
        assert DEADLINE_MESSAGE in response

    def test_streaming_turn_raises_when_the_scope_is_already_out_of_time(
        self, agent, mock_gateway, clock
    ):
        """The streaming helper signals the timeout rather than returning text."""
        agent.set_renderer(lambda chunks: "".join(c.text or "" for c in chunks))
        mock_gateway.stream_with_tools.return_value = iter(
            [StreamChunk(type="text", text=f"chunk{i}") for i in range(5)]
        )
        scope = ChatScope(turn_deadline=1.0)
        scope.turn_start = clock.t
        clock.advance(60.0)

        with pytest.raises(ChatTurnDeadlineExceeded):
            agent._streaming_turn([], "", [], "any", scope=scope)

    def test_a_scope_with_no_deadline_streams_to_the_end(self, agent, mock_gateway, clock):
        """No deadline, no cut: every chunk is delivered however slow the clock."""
        consumed: list[StreamChunk] = []

        def counting_renderer(chunks_iter):
            for c in chunks_iter:
                clock.advance(3600.0)
                consumed.append(c)
            return ""

        agent.set_renderer(counting_renderer)
        mock_gateway.stream_with_tools.return_value = iter(
            [StreamChunk(type="text", text=f"chunk{i}") for i in range(5)]
        )

        agent._streaming_turn([], "", [], "any", scope=ChatScope())

        assert len(consumed) == 5


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


class TestDegradesGracefullyBeforeTheDeadline:
    def test_too_little_time_left_withholds_the_tool_surface(self, agent, mock_gateway, clock):
        """The model wraps up with what it has instead of being cut off.

        Budget 10s, each model call burns 4s. Round 0 leaves 6s against a 4s
        reserve, so tools are still offered. Round 1 leaves 2s against that
        same reserve, which is not enough for another round as slow as the
        ones already seen, so round 2 is offered no tools and answers.
        """
        mock_gateway.complete_with_tools.side_effect = _tool_spammer(clock, cost=4.0)

        response = agent.turn(
            "Keep searching.",
            stream=False,
            scope=ChatScope(turn_deadline=10.0, max_tool_rounds=MAX_TOOL_ROUNDS),
        )

        assert mock_gateway.complete_with_tools.call_count == 3
        assert _last_tools(mock_gateway) in (None, [])
        assert "Synthesized answer" in response
        assert DEADLINE_MESSAGE not in response
        assert ROUNDS_EXHAUSTED_MESSAGE not in response

    def test_the_reserve_is_the_slowest_round_seen(self):
        """ "Too little" means: not enough for another round as slow as the worst."""
        assert agent_mod._wrap_up_reserve(20.0, slowest_round=9.0) == 9.0

    def test_the_reserve_never_falls_below_a_quarter_of_the_budget(self):
        """Before any round has finished there is nothing to measure, so a
        fixed share of the budget is held back instead."""
        assert agent_mod._wrap_up_reserve(8.0, slowest_round=0.0) == 2.0
        assert agent_mod._wrap_up_reserve(8.0, slowest_round=1.0) == 2.0

    def test_the_first_round_is_always_offered_tools(self, agent, mock_gateway, clock):
        """A short budget must not mean a turn that can never use a tool."""
        mock_gateway.complete_with_tools.side_effect = _tool_spammer(clock, cost=0.1)

        agent.turn("Search.", stream=False, scope=ChatScope(turn_deadline=1.0))

        first_call = mock_gateway.complete_with_tools.call_args_list[0]
        assert first_call.kwargs.get("tools")


# ---------------------------------------------------------------------------
# A timeout is not a cancellation
# ---------------------------------------------------------------------------


class TestTimeoutAndCancellationAreDifferentThings:
    def test_a_timeout_does_not_set_the_cancel_event(self, agent, mock_gateway, clock):
        """The person did not interrupt, so nothing may claim they did."""
        mock_gateway.complete_with_tools.side_effect = _tool_spammer(clock, cost=10.0)
        scope = ChatScope(turn_deadline=5.0)

        agent.turn("Search.", stream=False, scope=scope)

        assert not agent.is_cancelled(scope=scope)
        assert not scope.cancel_event.is_set()

    def test_the_caller_can_tell_them_apart(self, agent, mock_gateway, clock):
        """A timeout returns a message; a cancellation raises. Distinct paths."""
        agent.set_renderer(lambda chunks: "".join(c.text or "" for c in chunks))

        def long_stream():
            # Bounded so a lost deadline check fails this test instead of
            # hanging the suite, but far longer than the budget allows.
            for _ in range(5000):
                clock.advance(1.0)
                yield StreamChunk(type="text", text="tick")

        mock_gateway.stream_with_tools.return_value = long_stream()
        timed_out = agent.turn(
            "Talk.", stream=True, scope=ChatScope(turn_deadline=3.0, max_tool_rounds=5)
        )
        assert DEADLINE_MESSAGE in timed_out

        cancelled_scope = ChatScope(max_tool_rounds=5)
        mock_gateway.stream_with_tools.return_value = iter(
            [StreamChunk(type="text", text=f"chunk{i}") for i in range(5)]
        )
        cancelled_scope.cancel_event.set()
        with pytest.raises(ChatTurnCancelled):
            agent._streaming_turn([], "", [], "any", scope=cancelled_scope)

    def test_the_deadline_exception_is_not_a_cancellation(self):
        """Catching one must never catch the other."""
        assert not issubclass(ChatTurnDeadlineExceeded, ChatTurnCancelled)
        assert not issubclass(ChatTurnCancelled, ChatTurnDeadlineExceeded)

    def test_cancellation_wins_when_both_have_fired(self, agent, mock_gateway, clock):
        """A person who interrupted gets the interrupted message, not a timeout."""
        agent.set_renderer(lambda chunks: "".join(c.text or "" for c in chunks))
        scope = ChatScope(turn_deadline=1.0)
        scope.turn_start = clock.t
        clock.advance(60.0)
        scope.cancel_event.set()
        mock_gateway.stream_with_tools.return_value = iter([StreamChunk(type="text", text="chunk")])

        with pytest.raises(ChatTurnCancelled):
            agent._streaming_turn([], "", [], "any", scope=scope)


# ---------------------------------------------------------------------------
# The arithmetic the loop reads
# ---------------------------------------------------------------------------


class TestTimeRemaining:
    def test_an_unbounded_scope_has_no_remaining_time_to_report(self):
        assert ChatScope().time_remaining(now=1_000_000.0) is None

    def test_remaining_time_counts_down_from_the_turns_start(self):
        scope = ChatScope(turn_deadline=30.0)
        scope.turn_start = 100.0

        assert scope.time_remaining(now=100.0) == 30.0
        assert scope.time_remaining(now=110.0) == 20.0
        assert scope.time_remaining(now=130.0) == 0.0
        assert scope.time_remaining(now=135.0) == -5.0


# ---------------------------------------------------------------------------
# A budget that cannot run a turn is rejected, not silently obeyed
# ---------------------------------------------------------------------------


class TestImpossibleBudgetsAreRejected:
    @pytest.mark.parametrize("deadline", [0, 0.0, -1.0, -30])
    def test_a_deadline_of_zero_or_less_is_rejected(self, deadline):
        with pytest.raises(ValueError, match="turn_deadline"):
            ChatScope(turn_deadline=deadline)

    @pytest.mark.parametrize("rounds", [0, -1, -10])
    def test_a_round_budget_below_one_is_rejected(self, rounds):
        with pytest.raises(ValueError, match="max_tool_rounds"):
            ChatScope(max_tool_rounds=rounds)

    def test_a_budget_broken_after_construction_is_caught_at_the_turn(self, agent, mock_gateway):
        """A worker that recycles a scope cannot smuggle a bad budget past
        construction: the turn checks before it runs anything."""
        mock_gateway.complete_with_tools.side_effect = _tool_spammer()
        scope = ChatScope()
        scope.turn_deadline = 0

        with pytest.raises(ValueError, match="turn_deadline"):
            agent.turn("Search.", stream=False, scope=scope)

        assert mock_gateway.complete_with_tools.call_count == 0

    def test_a_valid_budget_passes_validation(self):
        """The check rejects the impossible and nothing else."""
        assert ChatScope(turn_deadline=0.5, max_tool_rounds=1).turn_deadline == 0.5
        assert ChatScope(turn_deadline=None).turn_deadline is None
