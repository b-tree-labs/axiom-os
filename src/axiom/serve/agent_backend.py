# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An agent loop behind the serving contract, bounded and opt-in.

The sanctioned serving path is retrieve, inject, single call, and the
2026-06-29 incident settled that an agent loop on a serving surface is
allowed only as a bounded, deliberate opt-in. This is that opt-in: a
:class:`~axiom.serve.chat_completions.StreamingChatBackend` implemented over
``HeadlessChat``, which already holds the per-process agent and mints one
scope per request with this surface's budgets on it.

Three properties make the loop safe to expose, and each is enforced here
rather than assumed of the caller:

* **A scope per request.** Every call mints a fresh ``ChatScope``, so two
  simultaneous callers never share a session, a permission map, an approval
  or an allowlist. The permission map is in memory, so no request reads or
  writes the operator's saved choices.
* **A deadline per request.** ``HeadlessChat`` refuses to exist without one,
  and this backend refuses a handle that carries none, so there is no
  construction path to an unbounded turn. The bound is enforced by the agent
  between chunks and between rounds. A single provider call that never
  returns anything at all is bounded by that provider client's own timeout
  and not by this seam, which is the one gap in the claim.
* **No per-prompt provider override.** A message beginning with ``@name`` or
  ``/m name`` would otherwise switch the provider on the process-wide gateway
  for the duration of the turn: a race between simultaneous requests, and a
  remote caller choosing who serves everybody else. It is refused rather than
  locked around, which removes the hazard instead of managing it. The same
  reasoning applies to the requested ``model``, which is echoed back but
  never installed on the gateway.

What ``tool_calls`` means here
------------------------------

This is the design question the module turns on, so it is answered in the
open. The handler's contract says a backend that returns ``tool_calls`` is
handing back calls it did **not** execute: the handler reports them with
``finish_reason="tool_calls"`` and stops, and running them is the client's
decision on the client's budget. An agent backend is the opposite shape. It
executes tools itself, because that is what an agent loop is, and by the time
it answers there is nothing left for a client to run.

Three answers were possible: satisfy the protocol and mean something
different by ``tool_calls``, define a separate protocol, or satisfy the
protocol exactly as written. This backend takes the third.

``tool_calls`` is never populated. A turn that ran ten tools answers with
``finish_reason="stop"``, the same as a turn that ran none, because the field
does not mean "tools were involved", it means "unexecuted, your turn". Filling
it with calls that already ran would tell an OpenAI client to run them a
second time and send the results back to a turn that is already over, which is
duplicated side effects dressed up as a protocol. So the field keeps its one
meaning, the protocol needs no second version, and the difference between the
two backends is visible where it belongs: in what the endpoint does, not in
what the field means.

Two consequences follow, and both are refusals rather than silent behaviour:

* a caller-supplied ``tools`` list is refused, because honouring it would mean
  pausing the loop and handing back a call, which is precisely what this
  backend does not do. Ignoring it silently would leave a client waiting for
  tool calls that are never coming;
* a ``tool`` role message is refused, because nothing this backend returns can
  produce one, so its presence means the client is talking to the wrong
  endpoint.

The tools a turn did run are reported to the process log against the request's
trace id. That is observability, not wire contract.

What a client sends and what is used
------------------------------------

The last ``user`` message is the turn. Earlier ``user`` and ``assistant``
messages are replayed into that request's fresh session, so the client owns
the transcript and the server keeps none of it between requests. A client
``system`` message is dropped rather than injected: the agent composes its own
system prompt, and a remote caller does not get to append instructions to a
loop that executes tools. ``max_tokens`` and ``temperature`` are accepted and
not honoured, because sampling is the gateway's routing decision.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from axiom.chat.picker import parse_per_prompt_override
from axiom.extensions.builtins.chat.agent import (
    DEADLINE_MESSAGE,
    ROUNDS_EXHAUSTED_MESSAGE,
)
from axiom.infra.orchestrator.session import Session
from axiom.serve.chat_completions import BackendResult, ChatCompletionError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from axiom.extensions.builtins.chat.headless import HeadlessChat
    from axiom.extensions.builtins.chat.scope import ChatScope

_LOGGER = logging.getLogger(__name__)

#: Message roles replayed into a request's session. Anything else the client
#: sends is either dropped (``system``) or refused (``tool``).
_HISTORY_ROLES = frozenset({"user", "assistant"})

#: Sentinel closing the chunk queue when the worker turn ends, however it ends.
_END = object()

#: Seconds to wait for a finished worker to retire. It has already run its
#: ``finally`` by the time this is reached, so this is tidiness, not a bound.
_WORKER_RETIRE_SECONDS = 5.0


class AgentChatBackend:
    """Serve OpenAI chat completions from a bounded, per-request agent turn.

    Args:
        chat: The per-process ``HeadlessChat``. It carries the deadline every
            request runs under and mints each request's scope.

    Raises:
        TypeError: when ``chat`` is not a chat handle.
        ValueError: when ``chat`` carries no positive ``turn_deadline``, which
            is the only way an unbounded turn could reach a serving surface.
    """

    def __init__(self, chat: HeadlessChat) -> None:
        for method in ("new_scope", "turn"):
            if not callable(getattr(chat, method, None)):
                raise TypeError(
                    "AgentChatBackend serves a HeadlessChat, which holds the "
                    "per-process agent and mints one scope per request; got "
                    f"{type(chat).__name__}, which has no {method}(). Build one "
                    "with HeadlessChat(turn_deadline=<seconds>)."
                )
        deadline = getattr(chat, "turn_deadline", None)
        if not isinstance(deadline, int | float) or isinstance(deadline, bool) or deadline <= 0:
            raise ValueError(
                "this backend runs an agent loop for a remote caller, so every "
                "request must be time-bounded; the handle it was given carries "
                f"turn_deadline={deadline!r}. Build it with "
                "HeadlessChat(turn_deadline=<seconds>)."
            )
        self._chat = chat

    @property
    def turn_deadline(self) -> float:
        """Wall-clock seconds every request this backend serves may take."""
        return float(self._chat.turn_deadline)

    # -- the backend contract ---------------------------------------------

    def __call__(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        trace_id: str,
        **kwargs: Any,
    ) -> BackendResult:
        """Run one bounded agent turn and return its answer.

        ``tool_calls`` is never set: any tool this turn wanted has already
        run. See the module docstring for why that is the whole answer rather
        than half of one.
        """
        turn_text, session = self._prepare(messages, kwargs)
        scope = self._chat.new_scope(session=session)
        answer = self._chat.turn(turn_text, stream=False, scope=scope)
        self._log_tools(scope, trace_id)
        return _result(answer)

    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        trace_id: str,
        **kwargs: Any,
    ) -> Iterator[str | BackendResult]:
        """Run one bounded agent turn, delivering content as it is produced.

        Validation happens here rather than on first iteration, so a refusal
        is raised before a transport has committed to a stream. The turn runs
        on a worker thread and reaches this iterator through ``scope.on_chunk``,
        the push seam that exists for exactly this caller. Only text deltas are
        forwarded: a thinking delta is the model's reasoning, not its answer,
        and an OpenAI content delta cannot say which it is.

        Abandoning the iterator cancels the turn, so a caller that hangs up
        does not leave a tool loop running with nobody reading.
        """
        turn_text, session = self._prepare(messages, kwargs)
        return self._stream_turn(turn_text, session, trace_id)

    def validate_request(self, messages: list[dict[str, Any]], **kwargs: Any) -> None:
        """Raise :class:`ChatCompletionError` for anything this must refuse.

        The pre-flight a transport runs before opening a stream. It runs the
        same preparation a call runs and discards the result, so the two
        cannot drift apart into a refusal that only one of them makes.
        """
        self._prepare(messages, kwargs)

    # -- internals ---------------------------------------------------------

    def _prepare(
        self, messages: list[dict[str, Any]], kwargs: dict[str, Any]
    ) -> tuple[str, Session]:
        """Refuse what cannot be served, then split the request into a turn."""
        _refuse_caller_tools(kwargs)
        turn_text, history = _split_messages(messages)
        _refuse_provider_override(turn_text)
        session = Session()
        for role, content in history:
            session.add_message(role, content)
        return turn_text, session

    def _stream_turn(
        self, turn_text: str, session: Session, trace_id: str
    ) -> Iterator[str | BackendResult]:
        chunks: queue.Queue[Any] = queue.Queue()

        def on_chunk(chunk: Any) -> None:
            if chunk.type == "text" and chunk.text:
                chunks.put(chunk.text)

        scope = self._chat.new_scope(session=session, on_chunk=on_chunk)
        outcome: dict[str, Any] = {}

        def run_turn() -> None:
            try:
                outcome["answer"] = self._chat.turn(turn_text, stream=True, scope=scope)
            except BaseException as exc:  # noqa: BLE001 - re-raised to the caller
                outcome["error"] = exc
            finally:
                chunks.put(_END)

        worker = threading.Thread(target=run_turn, name="serve-agent-turn", daemon=True)
        worker.start()

        emitted: list[str] = []
        finished = False
        try:
            while True:
                piece = chunks.get()
                if piece is _END:
                    finished = True
                    break
                emitted.append(piece)
                yield piece
        finally:
            if not finished:
                # Nobody is reading any more. Stop the loop at its next check.
                scope.cancel_event.set()

        worker.join(timeout=_WORKER_RETIRE_SECONDS)
        if "error" in outcome:
            raise outcome["error"]

        answer = outcome.get("answer") or ""
        self._log_tools(scope, trace_id)
        # Whatever the turn ended up saying that the deltas did not already
        # carry: a time-limit notice, a round-exhaustion sentence, or a final
        # round that never streamed. Never the answer a second time.
        yield BackendResult(
            content=_unstreamed_tail("".join(emitted), answer) or None,
            truncated=_budget_exhausted(answer),
        )

    def _log_tools(self, scope: ChatScope, trace_id: str) -> None:
        """Report what the loop ran to the process log, never to the wire."""
        if scope.last_turn_tools:
            _LOGGER.info(
                "agent turn %s executed %d tool call(s): %s",
                trace_id,
                len(scope.last_turn_tools),
                ", ".join(scope.last_turn_tools),
            )


def _refuse_caller_tools(kwargs: dict[str, Any]) -> None:
    """Refuse a request asking this backend to hand tool calls back."""
    if kwargs.get("tools"):
        raise ChatCompletionError(
            "this endpoint runs an agent loop and executes its own tools, so it "
            "never hands a tool call back for the caller to run. Drop 'tools' to "
            "let the agent work, or use the single-call completions endpoint, "
            "which reports tool calls unexecuted.",
            param="tools",
            code="tools_not_supported",
        )
    choice = kwargs.get("tool_choice")
    demands_a_call = isinstance(choice, dict) or (
        isinstance(choice, str) and choice.strip().lower() == "required"
    )
    if demands_a_call:
        raise ChatCompletionError(
            "this endpoint executes its own tools and cannot return a tool call "
            "for the caller to run, so a tool_choice demanding one cannot be "
            "satisfied. Use 'auto', 'none', or the single-call completions "
            "endpoint.",
            param="tool_choice",
            code="tool_choice_not_supported",
        )


def _refuse_provider_override(turn_text: str) -> None:
    """Refuse a message that would repoint the process-wide gateway.

    Decided from the text alone. Resolving the name against the gateway's
    roster first would both touch the shared object this refusal exists to
    protect and answer a remote caller's probe for which providers this node
    has configured.
    """
    if parse_per_prompt_override(turn_text).override_name is None:
        return
    raise ChatCompletionError(
        "a message beginning with '@' or '/m ' asks this node to switch the "
        "provider serving it. That switch is process-wide, so it would change "
        "the provider for every request in flight, and it is refused here "
        "rather than applied. Remove the prefix and send the message again.",
        param="messages",
        code="provider_override_refused",
    )


def _split_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[tuple[str, str]]]:
    """Split a request into (this turn's text, earlier turns to replay)."""
    if not isinstance(messages, list) or not messages:
        raise ChatCompletionError("'messages' must have at least one entry", param="messages")

    for message in messages:
        if message.get("role") == "tool" or message.get("tool_calls"):
            raise ChatCompletionError(
                "this endpoint executes its own tools and never returns a tool "
                "call, so a 'tool' message has nothing here to answer. Send the "
                "conversation without tool results, or use the single-call "
                "completions endpoint.",
                param="messages",
                code="tool_messages_not_supported",
            )

    last_user = None
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            last_user = index
            break
    if last_user is None:
        raise ChatCompletionError(
            "no user message to answer: this endpoint takes the last 'user' message as the turn.",
            param="messages",
        )

    turn_text = _text_of(messages[last_user])
    if not turn_text.strip():
        raise ChatCompletionError("the last user message is empty", param="messages")

    history = [
        (str(m.get("role")), _text_of(m))
        for m in messages[:last_user]
        if m.get("role") in _HISTORY_ROLES
    ]
    return turn_text, history


def _text_of(message: dict[str, Any]) -> str:
    """The text of one message, or a refusal when it is not text."""
    content = message.get("content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise ChatCompletionError(
            "this endpoint takes text messages: message content must be a "
            f"string, got {type(content).__name__}.",
            param="messages",
            code="unsupported_content",
        )
    return content


def _result(answer: str) -> BackendResult:
    """One turn's answer as a backend result. ``tool_calls`` stays empty."""
    return BackendResult(content=answer, truncated=_budget_exhausted(answer))


def _budget_exhausted(answer: str) -> bool:
    """Whether the turn stopped on a budget rather than because it was done.

    The turn contract reports this as the sentence it returns, so that is what
    is read. A turn that used its last round and still had something to say is
    indistinguishable from a finished one at this seam; when the turn contract
    grows a structured outcome, this reads that instead.
    """
    return answer.endswith(DEADLINE_MESSAGE) or answer == ROUNDS_EXHAUSTED_MESSAGE


def _unstreamed_tail(emitted: str, answer: str) -> str:
    """The part of ``answer`` the deltas have not already delivered.

    The streamed deltas and the returned answer overlap in ways that depend on
    how the turn ended: identical for a single streamed round, answer-plus-a-
    notice when the clock ran out, and answer-already-inside-the-stream when a
    later round produced it. One rule covers all of them: find the longest
    suffix of what was emitted that starts the answer, and send what follows.
    """
    if not answer:
        return ""
    for length in range(min(len(emitted), len(answer)), 0, -1):
        if emitted.endswith(answer[:length]):
            return answer[length:]
    return answer


__all__ = ["AgentChatBackend"]
