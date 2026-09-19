# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""One call for a caller with no terminal: a chat agent configured to serve.

Every piece a headless surface needs already exists. A render provider that
paints nothing lives in ``providers.null_render``. A policy that answers the
approval gate without asking anyone lives in ``approval_policy``. A
``ChatScope`` holds what belongs to one request, including its budgets and its
stream consumer, and ``utterances`` turns that consumer's token deltas into
speakable clauses.

What did not exist is the assembly. A serving surface had to know to install
the provider, install the policy, build a scope, set two budgets on it, attach
a consumer, and know which of those are per-process and which are per-request.
Each omission fails quietly: no policy and it prompts a pipe, no scope and it
shares approvals with every other request, no deadline and it runs unbounded.

``HeadlessChat`` is that assembly. It holds the per-process half once and
mints the per-request half on demand, so the two lifetimes are visible in one
place rather than left to a caller to remember::

    chat = HeadlessChat(turn_deadline=30.0)          # once, per process
    answer = chat.turn(question, on_chunk=publish)   # once, per request

Why a handle rather than a factory function
-------------------------------------------

A function returning a configured ``ChatAgent`` would cover the per-process
half and leave the per-request half exactly where it is today: with the
caller. A subclass of ``ChatAgent`` would put per-request minting on the one
object in the design that is deliberately per-process, which is the confusion
being fixed. A small handle owns both halves and names which is which.

One piece here is not lifted from an existing seam: ``_ChunkFanOut``, so that
a request can take raw deltas and finished clauses at once. It is composition
in the shape the stream consumer contract already has, not a new contract, and
it is private because nothing outside this assembly needs it yet.

What must be passed, and what must not
--------------------------------------

``turn_deadline`` is required because there is no safe default for it. How
long a turn may take is a property of the surface: a request behind a browser
wants seconds, a scheduled run may want minutes. The value that would have to
stand in is "unbounded", and an unbounded loop on a serving path is the hazard
this entry point exists to remove, so it is refused rather than defaulted.

``approval_policy`` is optional because it has exactly one safe default:
``NonInteractiveApprovalPolicy()``, which refuses everything. Requiring it
would make every caller type the same safe value, which teaches nothing.
Passing ``None`` is refused, because ``None`` is the one value that means
"an operator answers a prompt", and this surface has no operator.

The render provider is not a parameter at all. A headless agent that could be
handed a painting provider would not be a headless agent.

The operator-local prompt sources are not a parameter either, for the same
reason. Part of the system prompt is read off the machine the process runs on:
the repository's project file, the personal context file beside it, and the
operator's prompt library. Those belong to an operator, and this surface has
none, so every scope minted here declares
``include_operator_local_prompts=False`` and a hand-built scope that declares
otherwise is refused. A caller who wants a served surface to carry standing
instructions gives them to it on purpose, through the workspace brief or a
prompt contributor, rather than having them scraped from whoever's machine
the worker happens to be running on.

The retrieval corpus is not a parameter either. A turn indexes its transcript
into the node's internal corpus when it ends and retrieves from that corpus
when the next one begins, which at a terminal is how an operator gets last
week's conversation back and here would be how one requester gets another's.
So every scope minted here reads only ``SHARED_CORPORA`` and indexes nothing,
and a hand-built scope that says otherwise is refused. A caller who wants a
served surface to have material to ground on publishes it to the shared or
community corpus, which is what those corpora are for.

Nothing here assumes text output
--------------------------------

The direction for this system is speech within about a year, so this entry
point deals in chunks and clauses and never in a terminal shape: no width, no
colour, no cursor, no ``isatty``. Two text assumptions do survive, in the
layer underneath, and they are named here rather than hidden:

* ``ChatAgent.turn`` returns a ``str``, so a caller that renders or speaks the
  answer takes it from there;
* ``ROUNDS_EXHAUSTED_MESSAGE`` and ``DEADLINE_MESSAGE`` are English sentences,
  so a surface that speaks them speaks English.

Both belong to the turn contract rather than to this assembly, and changing
either is a larger piece of work than this one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from axiom.infra.bus import EventBus
from axiom.infra.gateway import Gateway
from axiom.infra.orchestrator.session import Session

from .agent import ChatAgent
from .approval_policy import ApprovalPolicy, NonInteractiveApprovalPolicy
from .permissions import ToolPermissions
from .providers.null_render import NullRenderProvider
from .scope import MAX_TOOL_ROUNDS, OPERATOR_LOCAL_CORPUS, SHARED_CORPORA, ChatScope
from .utterances import utterance_aggregator

if TYPE_CHECKING:  # pragma: no cover - typing only
    from axiom.infra.gateway import StreamChunk


class _Unset:
    """Distinguishes "omitted" from an explicit ``None``, which is refused."""


_UNSET: Any = _Unset()


class _ChunkFanOut:
    """One chunk consumer that feeds two.

    A request may want both halves at once: the raw deltas for a transcript
    arriving on a screen, and finished clauses for a voice. Both are chunk
    consumers with the same shape, so feeding both is composition rather than
    a second contract.

    Both consumers see every chunk even when the first one raises, because
    they are independent subscribers and one going away is not a reason to
    starve the other. The failure still reaches the agent's stream-consumer
    report, which logs it once and carries on.
    """

    def __init__(
        self,
        first: Callable[[StreamChunk], None],
        second: Callable[[StreamChunk], None],
    ) -> None:
        self._first = first
        self._second = second

    def __call__(self, chunk: StreamChunk) -> None:
        try:
            self._first(chunk)
        finally:
            self._second(chunk)

    def close(self) -> None:
        """End of stream for both, whichever of them holds buffered state."""
        try:
            close = getattr(self._first, "close", None)
            if close is not None:
                close()
        finally:
            close = getattr(self._second, "close", None)
            if close is not None:
                close()


class HeadlessChat:
    """A chat agent for a surface with no terminal, and a scope per request.

    Holds the per-process half once: one ``ChatAgent`` with a null render
    provider and a declared approval policy installed, so no path reaches a
    terminal prompt and none writes to stdout. Mints the per-request half on
    demand: a fresh ``ChatScope`` carrying this handle's budgets and the
    consumers this request asked for.

    Args:
        turn_deadline: Wall-clock seconds one turn may take. Required.
        gateway: The LLM gateway. Defaults to the platform's.
        bus: The event bus. Defaults to a fresh one.
        approval_policy: How this surface answers the approval gate. Omit for
            ``NonInteractiveApprovalPolicy()``, which refuses everything.
            ``None`` is refused: it means "prompt an operator".
        max_tool_rounds: Tool rounds one turn may run.

    Raises:
        ValueError: for a missing, ``None`` or non-positive ``turn_deadline``,
            a ``max_tool_rounds`` below one, or ``approval_policy=None``.
        TypeError: when ``approval_policy`` is not an ``ApprovalPolicy``.
    """

    def __init__(
        self,
        *,
        turn_deadline: float,
        gateway: Gateway | None = None,
        bus: EventBus | None = None,
        approval_policy: ApprovalPolicy = _UNSET,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
    ) -> None:
        if turn_deadline is None:
            raise ValueError(
                "HeadlessChat requires turn_deadline: the number of seconds one "
                "turn may take. There is no safe default, because the right "
                "bound belongs to the surface (seconds behind a browser, "
                "minutes for a scheduled run) and the value that would stand in "
                "is an unbounded turn. Pass turn_deadline=<seconds>."
            )
        if approval_policy is _UNSET:
            approval_policy = NonInteractiveApprovalPolicy()
        elif approval_policy is None:
            raise ValueError(
                "approval_policy=None leaves approvals to an operator at a "
                "prompt, and a headless surface has nobody to answer one. Omit "
                "approval_policy for the deny-by-default "
                "NonInteractiveApprovalPolicy(), or name the tools this surface "
                "may run: NonInteractiveApprovalPolicy(allow=['write_file'])."
            )
        elif not isinstance(approval_policy, ApprovalPolicy):
            raise TypeError(
                "approval_policy must be an ApprovalPolicy, which decides from "
                "its own configuration and reads no input stream, got "
                f"{type(approval_policy).__name__}. Pass "
                "NonInteractiveApprovalPolicy(allow=[...]) or a subclass of "
                "ApprovalPolicy."
            )

        # The scope's own budget rules, applied here rather than restated, so a
        # bad budget is a construction error instead of a first-request one.
        ChatScope(max_tool_rounds=max_tool_rounds, turn_deadline=turn_deadline)

        self._turn_deadline = float(turn_deadline)
        self._max_tool_rounds = max_tool_rounds
        self._approval_policy = approval_policy
        self._agent = ChatAgent(
            gateway=gateway or Gateway(),
            bus=bus or EventBus(),
            render=NullRenderProvider(approval_policy),
            # A serving process never reads or writes the operator's saved
            # allow/deny choices, so even the agent's default scope, which no
            # request uses, keeps its permission map in memory.
            permissions=ToolPermissions(),
            approval_policy=approval_policy,
        )

    @property
    def agent(self) -> ChatAgent:
        """The configured agent. Reconfiguring it undoes what this assembled."""
        return self._agent

    @property
    def approval_policy(self) -> ApprovalPolicy:
        """The policy both approval seams consult. Never ``None``."""
        return self._approval_policy

    @property
    def turn_deadline(self) -> float:
        """Wall-clock seconds every scope this handle mints allows one turn."""
        return self._turn_deadline

    @property
    def max_tool_rounds(self) -> int:
        """Tool rounds every scope this handle mints allows one turn."""
        return self._max_tool_rounds

    def new_scope(
        self,
        *,
        session: Session | None = None,
        on_chunk: Callable[[StreamChunk], None] | None = None,
        on_utterance: Callable[[str], None] | None = None,
    ) -> ChatScope:
        """Build one request's scope: fresh state, this handle's budgets.

        Nothing in the returned scope is shared with any other, so an approval
        answered here is answered nowhere else. Its permission map is in
        memory, so the operator's saved choices are neither read nor written,
        and it declares ``include_operator_local_prompts=False``, so the
        operator's project file, personal context file and prompt library stay
        out of the system prompt this request is answered under.

        It declares the retrieval corpus on both sides for the same reason: it
        reads ``SHARED_CORPORA`` rather than every corpus, so this machine's
        own chat transcripts cannot be retrieved into this request's prompt,
        and ``index_transcript=False``, so this request's own conversation is
        never written where a later one could retrieve it.

        ``on_chunk`` is called with every streaming chunk as it arrives.
        ``on_utterance`` is called with each finished clause, through
        ``utterances.utterance_aggregator``. Passing both installs both, and
        each sees the whole stream.

        Args:
            session: Message history to continue. A new one by default, which
                is a conversation of one turn.
            on_chunk: Per-delta consumer, for a caller that wants tokens.
            on_utterance: Per-clause consumer, for a caller that speaks.
        """
        consumer: Callable[[StreamChunk], None] | None = on_chunk
        if on_utterance is not None:
            aggregator = utterance_aggregator(on_utterance)
            consumer = _ChunkFanOut(on_chunk, aggregator) if on_chunk is not None else aggregator
        return ChatScope(
            session=session if session is not None else Session(),
            max_tool_rounds=self._max_tool_rounds,
            turn_deadline=self._turn_deadline,
            on_chunk=consumer,
            include_operator_local_prompts=False,
            retrieval_corpora=list(SHARED_CORPORA),
            index_transcript=False,
        )

    def turn(
        self,
        user_input: str,
        *,
        stream: bool = True,
        scope: ChatScope | None = None,
        on_chunk: Callable[[StreamChunk], None] | None = None,
        on_utterance: Callable[[str], None] | None = None,
    ) -> str:
        """Answer one request, in its own scope, and return the response text.

        With no ``scope`` this mints one, so two requests never share state
        even when the caller does nothing to keep them apart. With a ``scope``,
        which is how a caller continues a conversation, the consumers are
        already on it and passing them again is refused rather than silently
        ignored.

        A scope with no deadline is refused: an unbounded turn is the hazard
        this entry point exists to remove, and a hand-built scope must not be
        able to reintroduce it. Tightening a budget is always allowed. A scope
        that includes the operator-local prompt sources is refused for the same
        reason, and so is one that would index its transcript into the node's
        corpus or retrieve from it. Every message says to build it with
        ``new_scope``.

        Args:
            user_input: The request.
            stream: Stream the first round, which is what fires ``on_chunk``.
            scope: A scope from ``new_scope`` to continue in.
            on_chunk: Per-delta consumer, when this call mints the scope.
            on_utterance: Per-clause consumer, when this call mints the scope.

        Returns:
            The assistant's answer, or the sentence saying which budget ran out.
        """
        if scope is None:
            scope = self.new_scope(on_chunk=on_chunk, on_utterance=on_utterance)
        else:
            if on_chunk is not None or on_utterance is not None:
                raise ValueError(
                    "pass on_chunk and on_utterance to new_scope, or let turn() "
                    "build the scope; a scope handed to turn() already carries "
                    "its consumers and a second set would be dropped."
                )
            if scope.turn_deadline is None:
                raise ValueError(
                    "a scope handed to a headless turn must carry a "
                    "turn_deadline; build it with new_scope(), which sets this "
                    f"handle's {self._turn_deadline}s bound."
                )
            if scope.include_operator_local_prompts:
                raise ValueError(
                    "a scope handed to a headless turn must not include the "
                    "operator-local prompt sources: this surface answers "
                    "somebody other than the operator, and the operator's "
                    "project file, personal context file and prompt library "
                    "would go into their prompt. Build it with new_scope(), "
                    "which declares include_operator_local_prompts=False."
                )
            if scope.index_transcript:
                raise ValueError(
                    "a scope handed to a headless turn must not index its "
                    "transcript: this surface answers somebody other than the "
                    "operator, and their conversation would go into the corpus "
                    "every later conversation retrieves from. Build it with "
                    "new_scope(), which declares index_transcript=False."
                )
            if scope.retrieval_corpora is None or OPERATOR_LOCAL_CORPUS in scope.retrieval_corpora:
                raise ValueError(
                    "a scope handed to a headless turn must not retrieve from "
                    f"{OPERATOR_LOCAL_CORPUS!r}, and None means every corpus "
                    "including that one: it holds this machine's own chat "
                    "transcripts, which would go into the prompt of whoever "
                    "asks. Build it with new_scope(), which declares the "
                    "shared corpora, or name the corpora this surface may "
                    "read. Material a served surface should ground on belongs "
                    "in the shared or community corpus."
                )
        return self._agent.turn(user_input, stream=stream, scope=scope)


__all__ = ["HeadlessChat"]
