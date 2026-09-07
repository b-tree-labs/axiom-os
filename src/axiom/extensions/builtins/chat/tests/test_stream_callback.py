# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Watching a stream as it arrives, one chunk at a time.

The chat agent could only ever hand its stream to a render function that owns
the loop and returns a string when it is done. That is the right shape for a
terminal and the wrong shape for a surface that has to do something per delta,
because the caller never gets control back between chunks. These tests pin the
seam that fixes it and the first thing built on top of it.

Two layers, tested separately because they are separate:

* the primitive: ``ChatScope.on_chunk``, called with every chunk as it
  arrives, in both of the streaming helper's branches, before the
  cancellation and deadline checks, and unable to break the turn by raising;
* the speech-shaped layer: ``utterance_aggregator``, a consumer that wraps
  another consumer and reports whole clauses rather than tokens, because a
  voice can start speaking a finished clause and cannot start on half a word.

The aggregator is composed onto the primitive from outside, so the agent
knows nothing about sentences and the segmenter knows nothing about turns.
Both facts are asserted here.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.chat import agent as agent_mod
from axiom.extensions.builtins.chat.agent import (
    ChatAgent,
    ChatTurnCancelled,
    ChatTurnDeadlineExceeded,
)
from axiom.extensions.builtins.chat.providers.null_render import NullRenderProvider
from axiom.extensions.builtins.chat.scope import ChatScope
from axiom.extensions.builtins.chat.utterances import (
    MAX_FRAGMENT_CHARS,
    UtteranceAggregator,
    utterance_aggregator,
)
from axiom.infra.bus import EventBus
from axiom.infra.gateway import Gateway, StreamChunk
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
    fake = FakeClock()
    monkeypatch.setattr(agent_mod, "_now", fake)
    return fake


@pytest.fixture
def mock_gateway():
    from unittest.mock import MagicMock

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


def _text(s: str) -> StreamChunk:
    return StreamChunk(type="text", text=s)


def _rich_stream() -> list[StreamChunk]:
    """One round's worth of every chunk kind the helper knows how to fold."""
    return [
        _text("Hello "),
        StreamChunk(type="thinking_delta", text="pondering"),
        StreamChunk(type="tool_use_start", tool_id="t1", tool_name="search"),
        StreamChunk(type="tool_input_delta", tool_id="t1", tool_input_json='{"q":'),
        StreamChunk(type="tool_use_end", tool_id="t1", tool_input_json='{"q": "x"}'),
        _text("world"),
        StreamChunk(type="usage", input_tokens=3, output_tokens=4, cache_read_tokens=1),
    ]


def _assert_rich_response(response) -> None:
    """The response the helper has always built from ``_rich_stream``."""
    assert response.text == "Hello world"
    assert [(t.tool_id, t.name, t.input) for t in response.tool_use] == [
        ("t1", "search", {"q": "x"})
    ]
    assert response.input_tokens == 3
    assert response.output_tokens == 4
    assert response.cache_read_tokens == 1
    assert response.success is True
    assert response.provider == "test"
    assert response.model == "test-model"


def _bare_renderer(chunks) -> str:
    """The pull-shaped render callback a surface installs with set_renderer."""
    return "".join(c.text or "" for c in chunks)


# ---------------------------------------------------------------------------
# The primitive: every chunk reaches the scope's consumer
# ---------------------------------------------------------------------------


class TestTheConsumerSeesEveryChunk:
    def test_with_a_render_provider_set(self, agent, mock_gateway):
        """The teed branch: a render provider owns the loop and the consumer still sees it all."""
        agent.set_render_provider(NullRenderProvider())
        seen: list[StreamChunk] = []
        scope = ChatScope()
        scope.on_chunk = seen.append
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        response = agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.type for c in seen] == [c.type for c in _rich_stream()]
        _assert_rich_response(response)

    def test_with_a_bare_render_callback_set(self, agent, mock_gateway):
        """The other teed case: set_renderer rather than a provider."""
        agent.set_renderer(_bare_renderer)
        seen: list[StreamChunk] = []
        scope = ChatScope()
        scope.on_chunk = seen.append
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        response = agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.type for c in seen] == [c.type for c in _rich_stream()]
        _assert_rich_response(response)

    def test_with_no_render_function_at_all(self, agent, mock_gateway):
        """The serving branch, and the only one a serving surface runs.

        A seam added to the teed branch alone would look right in a terminal
        and do nothing where it matters, so this is the test that guards the
        regression worth guarding.
        """
        assert agent._render is None and agent._renderer_callback is None
        seen: list[StreamChunk] = []
        scope = ChatScope()
        scope.on_chunk = seen.append
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        response = agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.type for c in seen] == [c.type for c in _rich_stream()]
        _assert_rich_response(response)

    def test_chunks_arrive_in_order_and_unmodified(self, agent, mock_gateway):
        seen: list[StreamChunk] = []
        scope = ChatScope()
        scope.on_chunk = seen.append
        script = [_text("a"), _text("b"), _text("c")]
        mock_gateway.stream_with_tools.return_value = iter(script)

        agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.text for c in seen] == ["a", "b", "c"]
        assert seen[0] is script[0]


class TestTheConsumerIsPerConversation:
    def test_two_scopes_on_one_agent_never_see_each_others_chunks(self, agent, mock_gateway):
        """One worker, many requests: a consumer belongs to the request."""
        first: list[StreamChunk] = []
        second: list[StreamChunk] = []
        scope_a = ChatScope()
        scope_a.on_chunk = first.append
        scope_b = ChatScope()
        scope_b.on_chunk = second.append

        mock_gateway.stream_with_tools.return_value = iter([_text("one")])
        agent._streaming_turn([], "", [], "any", scope=scope_a)
        mock_gateway.stream_with_tools.return_value = iter([_text("two")])
        agent._streaming_turn([], "", [], "any", scope=scope_b)

        assert [c.text for c in first] == ["one"]
        assert [c.text for c in second] == ["two"]

    def test_a_scope_with_a_consumer_does_not_install_one_on_the_agent(self, agent, mock_gateway):
        """The default scope stays consumer-free when a request brings its own."""
        scope = ChatScope()
        scope.on_chunk = lambda chunk: None
        mock_gateway.stream_with_tools.return_value = iter([_text("one")])

        agent._streaming_turn([], "", [], "any", scope=scope)

        assert agent.scope.on_chunk is None

    def test_the_default_scope_has_no_consumer(self):
        """A terminal sets none and behaves as it always has."""
        assert ChatScope().on_chunk is None


class TestNoConsumerChangesNothing:
    """Byte for byte, a stream with no consumer is the stream of yesterday."""

    def test_with_a_render_provider(self, agent, mock_gateway):
        agent.set_render_provider(NullRenderProvider())
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        _assert_rich_response(agent._streaming_turn([], "", [], "any", scope=ChatScope()))

    def test_with_a_bare_render_callback(self, agent, mock_gateway):
        agent.set_renderer(_bare_renderer)
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        _assert_rich_response(agent._streaming_turn([], "", [], "any", scope=ChatScope()))

    def test_with_no_render_function(self, agent, mock_gateway):
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        _assert_rich_response(agent._streaming_turn([], "", [], "any", scope=ChatScope()))

    def test_the_render_function_still_sees_the_whole_stream(self, agent, mock_gateway):
        """A consumer tees off the stream; it does not consume it."""
        rendered: list[StreamChunk] = []

        def recording_renderer(chunks) -> str:
            for c in chunks:
                rendered.append(c)
            return ""

        agent.set_renderer(recording_renderer)
        scope = ChatScope()
        scope.on_chunk = lambda chunk: None
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.type for c in rendered] == [c.type for c in _rich_stream()]


class TestTheConsumerRunsBeforeTheChecks:
    """A chunk that was produced is delivered, whatever the clock says next."""

    def test_a_chunk_produced_after_cancellation_is_still_delivered(self, agent, mock_gateway):
        seen: list[StreamChunk] = []
        scope = ChatScope()
        scope.on_chunk = seen.append
        scope.cancel_event.set()
        mock_gateway.stream_with_tools.return_value = iter([_text("produced"), _text("never")])

        with pytest.raises(ChatTurnCancelled):
            agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.text for c in seen] == ["produced"]

    def test_a_chunk_produced_after_the_deadline_is_still_delivered(
        self, agent, mock_gateway, clock
    ):
        seen: list[StreamChunk] = []
        scope = ChatScope(turn_deadline=1.0)
        scope.on_chunk = seen.append
        scope.turn_start = clock.t
        clock.advance(60.0)
        mock_gateway.stream_with_tools.return_value = iter([_text("produced"), _text("never")])

        with pytest.raises(ChatTurnDeadlineExceeded):
            agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.text for c in seen] == ["produced"]

    def test_the_same_holds_on_the_teed_branch(self, agent, mock_gateway):
        agent.set_render_provider(NullRenderProvider())
        seen: list[StreamChunk] = []
        scope = ChatScope()
        scope.on_chunk = seen.append
        scope.cancel_event.set()
        mock_gateway.stream_with_tools.return_value = iter([_text("produced"), _text("never")])

        with pytest.raises(ChatTurnCancelled):
            agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.text for c in seen] == ["produced"]


class TestAFailingConsumerCannotKillTheTurn:
    def test_the_turn_completes_and_its_result_is_unchanged(self, agent, mock_gateway):
        scope = ChatScope()

        def explode(chunk):
            raise RuntimeError("the browser went away")

        scope.on_chunk = explode
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        _assert_rich_response(agent._streaming_turn([], "", [], "any", scope=scope))

    def test_the_same_on_the_teed_branch(self, agent, mock_gateway):
        agent.set_render_provider(NullRenderProvider())
        scope = ChatScope()

        def explode(chunk):
            raise RuntimeError("the browser went away")

        scope.on_chunk = explode
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        _assert_rich_response(agent._streaming_turn([], "", [], "any", scope=scope))

    def test_a_failure_is_logged_once_per_stream_not_once_per_chunk(
        self, agent, mock_gateway, caplog
    ):
        """A disconnected consumer fails on every token; the log must not."""
        scope = ChatScope()

        def explode(chunk):
            raise RuntimeError("the browser went away")

        scope.on_chunk = explode
        mock_gateway.stream_with_tools.return_value = iter([_text(str(i)) for i in range(50)])

        with caplog.at_level(logging.WARNING, logger=agent_mod.__name__):
            agent._streaming_turn([], "", [], "any", scope=scope)

        failures = [r for r in caplog.records if "consumer" in r.getMessage()]
        assert len(failures) == 1

    def test_the_failure_is_not_swallowed_silently(self, agent, mock_gateway, caplog):
        scope = ChatScope()

        def explode(chunk):
            raise RuntimeError("the browser went away")

        scope.on_chunk = explode
        mock_gateway.stream_with_tools.return_value = iter([_text("a")])

        with caplog.at_level(logging.WARNING, logger=agent_mod.__name__):
            agent._streaming_turn([], "", [], "any", scope=scope)

        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_a_later_stream_reports_again(self, agent, mock_gateway, caplog):
        """Suppression is per stream, so a new request is not muted by an old one."""
        scope = ChatScope()

        def explode(chunk):
            raise RuntimeError("the browser went away")

        scope.on_chunk = explode

        with caplog.at_level(logging.WARNING, logger=agent_mod.__name__):
            mock_gateway.stream_with_tools.return_value = iter([_text("a"), _text("b")])
            agent._streaming_turn([], "", [], "any", scope=scope)
            mock_gateway.stream_with_tools.return_value = iter([_text("c"), _text("d")])
            agent._streaming_turn([], "", [], "any", scope=scope)

        failures = [r for r in caplog.records if "consumer" in r.getMessage()]
        assert len(failures) == 2


class TestTheEndOfStreamSignal:
    """A consumer that holds state is told when there will be no more chunks."""

    def test_close_is_called_when_the_stream_ends(self, agent, mock_gateway):
        closed: list[bool] = []

        class Consumer:
            def __call__(self, chunk):
                pass

            def close(self):
                closed.append(True)

        scope = ChatScope()
        scope.on_chunk = Consumer()
        mock_gateway.stream_with_tools.return_value = iter([_text("a")])

        agent._streaming_turn([], "", [], "any", scope=scope)

        assert closed == [True]

    def test_close_is_called_once(self, agent, mock_gateway):
        closed: list[bool] = []

        class Consumer:
            def __call__(self, chunk):
                pass

            def close(self):
                closed.append(True)

        scope = ChatScope()
        scope.on_chunk = Consumer()
        mock_gateway.stream_with_tools.return_value = iter([_text("a"), _text("b")])

        agent._streaming_turn([], "", [], "any", scope=scope)

        assert closed == [True]

    def test_a_plain_function_needs_no_close(self, agent, mock_gateway):
        """The primitive is a callable. ``close`` is optional and absence is fine."""
        seen: list[StreamChunk] = []
        scope = ChatScope()
        scope.on_chunk = seen.append
        mock_gateway.stream_with_tools.return_value = iter([_text("a")])

        response = agent._streaming_turn([], "", [], "any", scope=scope)

        assert [c.text for c in seen] == ["a"]
        assert response.text == "a"

    def test_a_raising_close_does_not_kill_the_turn(self, agent, mock_gateway):
        class Consumer:
            def __call__(self, chunk):
                pass

            def close(self):
                raise RuntimeError("the browser went away")

        scope = ChatScope()
        scope.on_chunk = Consumer()
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        _assert_rich_response(agent._streaming_turn([], "", [], "any", scope=scope))


class TestStreamingIsReachableWithoutARenderer:
    """The gate on the streaming branch has to know about the new consumer."""

    def test_a_turn_streams_when_the_scope_carries_only_a_consumer(self, agent, mock_gateway):
        seen: list[StreamChunk] = []
        scope = ChatScope()
        scope.on_chunk = seen.append
        mock_gateway.stream_with_tools.return_value = iter([_text("hi")])

        response = agent.turn("hello", stream=True, scope=scope)

        assert mock_gateway.stream_with_tools.called
        assert [c.text for c in seen] == ["hi"]
        assert response == "hi"

    def test_a_turn_with_neither_renderer_nor_consumer_does_not_stream(self, agent, mock_gateway):
        """Unchanged: nothing to stream to means the non-streaming path."""
        from axiom.infra.gateway import CompletionResponse

        mock_gateway.complete_with_tools.return_value = CompletionResponse(
            text="plain", provider="test", success=True
        )

        response = agent.turn("hello", stream=True, scope=ChatScope())

        assert not mock_gateway.stream_with_tools.called
        assert response == "plain"


# ---------------------------------------------------------------------------
# The speech-shaped layer: utterance aggregation
# ---------------------------------------------------------------------------


class TestAggregatorSentenceBoundaries:
    def test_emits_a_sentence_followed_by_whitespace(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Hello there. "))

        assert spoken == ["Hello there."]

    def test_holds_a_sentence_until_the_whitespace_arrives(self):
        """Mid-stream, a trailing period is not yet a boundary: it may be a decimal."""
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Hello there."))
        assert spoken == []

        agg(_text(" Next."))
        assert spoken == ["Hello there."]

    def test_question_marks_and_exclamations_end_sentences(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Ready? Go! Now "))

        assert spoken == ["Ready?", "Go!"]

    def test_token_by_token_deltas_reassemble_into_clauses(self):
        """The real shape of the input: words split across chunk boundaries."""
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        for token in ["Hel", "lo", " wor", "ld.", " Bye", "!", " "]:
            agg(_text(token))

        assert spoken == ["Hello world.", "Bye!"]

    def test_a_closing_quote_stays_with_its_sentence(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text('She said "go." Then she left. '))

        assert spoken == ['She said "go."', "Then she left."]

    def test_an_ellipsis_ends_a_clause(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Well… let me think. "))

        assert spoken == ["Well…", "let me think."]

    def test_a_closing_bracket_stays_with_its_sentence(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("(It ran twice.) Then it stopped. "))

        assert spoken == ["(It ran twice.)", "Then it stopped."]

    def test_several_sentences_in_one_chunk_all_emit(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("One. Two. Three. "))

        assert spoken == ["One.", "Two.", "Three."]

    def test_whitespace_between_sentences_is_not_spoken(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("One.\n\n   Two.\n"))

        assert spoken == ["One.", "Two."]

    def test_an_empty_utterance_is_never_reported(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("   \n  "))
        agg(_text(""))
        agg.close()

        assert spoken == []


class TestAggregatorDoesNotSplitWhatLooksLikeASentenceEnd:
    def test_a_decimal_number_survives(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Pi is 3.14 exactly. "))

        assert spoken == ["Pi is 3.14 exactly."]

    def test_a_decimal_split_across_two_chunks_survives(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Pi is 3."))
        agg(_text("14 exactly. "))

        assert spoken == ["Pi is 3.14 exactly."]

    def test_a_title_abbreviation_survives(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Dr. Smith arrived. "))

        assert spoken == ["Dr. Smith arrived."]

    def test_a_dotted_abbreviation_survives(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Use a tool, e.g. search, first. "))

        assert spoken == ["Use a tool, e.g. search, first."]

    def test_initials_survive(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("J. R. R. Tolkien wrote it. "))

        assert spoken == ["J. R. R. Tolkien wrote it."]

    def test_a_sentence_that_really_ends_in_an_abbreviation_is_held(self):
        """The documented cost of a fixed abbreviation list, pinned deliberately.

        This is a pragmatic segmenter, not a sentence tokenizer. It cannot
        tell a closing "etc." from a mid-sentence one, so it keeps
        accumulating and the clause lands at the next real boundary or at
        the end of the stream. Nothing is ever lost, only merged.
        """
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Apples, pears, etc. "))
        assert spoken == []

        agg.close()
        assert spoken == ["Apples, pears, etc."]


class TestAggregatorLengthCap:
    def test_a_punctuation_free_run_emits_at_the_cap(self):
        """A long clause with no punctuation must not hold the whole turn."""
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append, max_fragment_chars=20)

        agg(_text("alpha beta gamma delta epsilon"))

        assert spoken == ["alpha beta gamma"]

    def test_the_cap_cuts_at_a_word_boundary(self):
        """A voice cannot begin on half a word, so the cut lands between words."""
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append, max_fragment_chars=20)

        agg(_text("alpha beta gamma delta epsilon"))
        agg.close()

        assert spoken == ["alpha beta gamma", "delta epsilon"]
        assert all(" ".join(s.split()) == s for s in spoken)

    def test_a_run_with_no_word_boundary_is_cut_at_the_cap(self):
        """Degenerate input (a URL, an identifier): progress beats purity."""
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append, max_fragment_chars=8)

        agg(_text("a" * 20))

        assert spoken == ["a" * 8, "a" * 8]

    def test_a_complete_sentence_is_emitted_whole_even_past_the_cap(self):
        """The cap is a floor on progress, not a ceiling on utterance length."""
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append, max_fragment_chars=10)

        agg(_text("one two three four five. "))

        assert spoken == ["one two three four five."]

    def test_the_default_cap_is_the_module_constant(self):
        assert utterance_aggregator(on_utterance=lambda s: None).max_fragment_chars == (
            MAX_FRAGMENT_CHARS
        )

    def test_a_cap_below_one_is_rejected(self):
        with pytest.raises(ValueError):
            utterance_aggregator(on_utterance=lambda s: None, max_fragment_chars=0)


class TestAggregatorOnlyAggregatesSpeech:
    def test_thinking_deltas_are_ignored(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(StreamChunk(type="thinking_delta", text="I should think about this. "))
        agg.close()

        assert spoken == []

    def test_tool_chunks_are_ignored(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(StreamChunk(type="tool_use_start", tool_id="t1", tool_name="search"))
        agg(StreamChunk(type="tool_input_delta", tool_id="t1", tool_input_json='{"q": "x"}. '))
        agg(StreamChunk(type="tool_use_end", tool_id="t1", tool_input_json='{"q": "x"}'))
        agg.close()

        assert spoken == []

    def test_usage_chunks_are_ignored(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(StreamChunk(type="usage", input_tokens=3, output_tokens=4))
        agg.close()

        assert spoken == []

    def test_text_around_non_speech_chunks_still_joins_up(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Half a "))
        agg(StreamChunk(type="thinking_delta", text="hmm"))
        agg(StreamChunk(type="usage", output_tokens=1))
        agg(_text("thought. "))

        assert spoken == ["Half a thought."]


class TestAggregatorFlushesItsTail:
    def test_close_reports_what_is_left(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("A clause with no terminator"))
        assert spoken == []

        agg.close()
        assert spoken == ["A clause with no terminator"]

    def test_close_is_idempotent(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg(_text("Tail"))
        agg.close()
        agg.close()

        assert spoken == ["Tail"]

    def test_close_on_an_empty_buffer_reports_nothing(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg.close()

        assert spoken == []

    def test_chunks_after_close_are_ignored(self):
        spoken: list[str] = []
        agg = utterance_aggregator(on_utterance=spoken.append)

        agg.close()
        agg(_text("too late. "))

        assert spoken == []


class TestAggregatorDoesNotGuardItsConsumer:
    """The agent absorbs a failing consumer; doing it here as well would hide it."""

    def test_a_raising_utterance_consumer_is_not_caught_here(self):
        def explode(utterance: str) -> None:
            raise RuntimeError("the voice went away")

        agg = utterance_aggregator(on_utterance=explode)

        with pytest.raises(RuntimeError):
            agg(_text("One. "))

    def test_a_clause_lost_to_a_raising_consumer_is_not_repeated(self):
        """The buffer advances before the report, so a failure drops one clause."""
        spoken: list[str] = []

        def flaky(utterance: str) -> None:
            if utterance == "One.":
                raise RuntimeError("the voice went away")
            spoken.append(utterance)

        agg = utterance_aggregator(on_utterance=flaky)

        with pytest.raises(RuntimeError):
            agg(_text("One. "))
        agg(_text("Two. "))

        assert spoken == ["Two."]


# ---------------------------------------------------------------------------
# The two layers composed: an aggregator installed on a scope
# ---------------------------------------------------------------------------


class TestAggregatorComposedOntoAScope:
    def test_a_turn_reports_clauses_rather_than_tokens(self, agent, mock_gateway):
        spoken: list[str] = []
        scope = ChatScope()
        scope.on_chunk = utterance_aggregator(on_utterance=spoken.append)
        mock_gateway.stream_with_tools.return_value = iter(
            [_text("First clause. "), _text("Second "), _text("clause. ")]
        )

        response = agent._streaming_turn([], "", [], "any", scope=scope)

        assert spoken == ["First clause.", "Second clause."]
        assert response.text == "First clause. Second clause. "

    def test_the_tail_survives_a_cancellation(self, agent, mock_gateway):
        """The last clause is never lost, however the stream ends."""
        spoken: list[str] = []
        scope = ChatScope()
        scope.on_chunk = utterance_aggregator(on_utterance=spoken.append)
        scope.cancel_event.set()
        mock_gateway.stream_with_tools.return_value = iter([_text("A half-finished thought")])

        with pytest.raises(ChatTurnCancelled):
            agent._streaming_turn([], "", [], "any", scope=scope)

        assert spoken == ["A half-finished thought"]

    def test_the_tail_survives_a_timeout(self, agent, mock_gateway, clock):
        spoken: list[str] = []
        scope = ChatScope(turn_deadline=1.0)
        scope.on_chunk = utterance_aggregator(on_utterance=spoken.append)
        scope.turn_start = clock.t
        clock.advance(60.0)
        mock_gateway.stream_with_tools.return_value = iter([_text("A half-finished thought")])

        with pytest.raises(ChatTurnDeadlineExceeded):
            agent._streaming_turn([], "", [], "any", scope=scope)

        assert spoken == ["A half-finished thought"]

    def test_the_tail_survives_on_the_teed_branch_too(self, agent, mock_gateway):
        agent.set_render_provider(NullRenderProvider())
        spoken: list[str] = []
        scope = ChatScope()
        scope.on_chunk = utterance_aggregator(on_utterance=spoken.append)
        mock_gateway.stream_with_tools.return_value = iter([_text("No terminator here")])

        agent._streaming_turn([], "", [], "any", scope=scope)

        assert spoken == ["No terminator here"]

    def test_a_speaking_consumer_that_fails_does_not_kill_the_turn(self, agent, mock_gateway):
        """The aggregator does not guard its consumer; the agent guards both."""
        scope = ChatScope()

        def explode(utterance: str) -> None:
            raise RuntimeError("the voice went away")

        scope.on_chunk = utterance_aggregator(on_utterance=explode)
        mock_gateway.stream_with_tools.return_value = iter(_rich_stream())

        _assert_rich_response(agent._streaming_turn([], "", [], "any", scope=scope))

    def test_the_aggregator_is_the_public_class(self):
        """One name for the type, one factory for composition."""
        agg = utterance_aggregator(on_utterance=lambda s: None)
        assert isinstance(agg, UtteranceAggregator)
