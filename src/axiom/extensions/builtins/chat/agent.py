# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat agent — native tool-use loop with LLM and approval gate.

Drives the conversation with multi-turn tool calling:
  user input → Gateway.complete_with_tools() →
  if tool_use: execute → feed results back → loop
  else: return text response

The agent is LLM-agnostic: it uses the same Gateway as every other
platform capability.

One agent can serve many conversations. Everything that belongs to a
conversation lives on a ``ChatScope`` (see ``scope``), never on the agent, so
an approval one person gave cannot answer another person's request. ``turn``
takes an optional scope: passed, the turn runs in it and touches nothing else;
omitted, the turn runs in the agent's default scope, which is what a terminal
has always been doing. Inside the agent the active scope is threaded through
every private method that needs it, as an argument that is visible at each
call site. It is deliberately not carried in a context variable: per-request
state that is invisible at the call site is the defect being fixed here, and
an ambient carrier would only change its shape.

A turn is bounded twice: by how many tool rounds it may run and by how long it
may take. Both budgets come off the active scope, so a serving worker can hold
one agent and still give a web request a tighter bound than an operator at a
terminal gets. Neither budget is a cancellation; see ``ChatTurnCancelled`` and
``ChatTurnDeadlineExceeded``.

A note with a shelf life: the deadline assumes a turn is a bounded unit with a
start and an end. That is true today. The direction for this system is
full-duplex conversation, where input arrives while the model is still
generating and may redirect a turn rather than end it, and under that model
"the turn started at T and may take D seconds" stops describing anything real.
The bound ships anyway because an unbounded loop on a serving path is a live
hazard now. A reader meeting this later should treat the assumption as a
decision that was made knowingly, not as an oversight.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.bus import EventBus
from axiom.infra.gateway import (
    CompletionResponse,
    Gateway,
    StreamChunk,
)
from axiom.infra.orchestrator.actions import (
    Action,
    ActionCategory,
    ActionStatus,
    create_action,
)
from axiom.infra.orchestrator.session import Session
from axiom.infra.prompt_context import PromptContext
from axiom.infra.prompt_registry import get_registry as _get_prompt_registry
from axiom.infra.router import QueryRouter

from .approval_policy import ApprovalPolicy
from .permissions import ToolPermissions
from .providers.base import RenderProvider
from .scope import MAX_TOOL_ROUNDS as MAX_TOOL_ROUNDS  # re-export; the loop reads the scope
from .scope import ChatScope
from .tools import (
    ToolDef,
    execute_tool,
    get_all_tools,
    get_tool_definitions,
)
from .usage import TurnUsage, UsageTracker

_log = logging.getLogger(__name__)

CONTEXT_TOKEN_BUDGET = 25000
CHARS_PER_TOKEN = 4  # rough estimate

# How a turn says which budget it ran out of. The two sentences differ because
# a reader has to be able to tell "the model kept asking for tools" from "this
# took too long", and because a surface may want to react differently to each.
ROUNDS_EXHAUSTED_MESSAGE = "I've reached the maximum number of tool-use rounds."
DEADLINE_MESSAGE = "I've reached this turn's time limit and stopped before finishing."

# The share of a turn's whole budget held back for wrapping up, used before any
# round has finished and there is nothing yet to measure. See _wrap_up_reserve.
TURN_WRAP_UP_RESERVE = 0.25

# Entry-points group through which any installed extension contributes
# domain/role specialization to the chat agent's system prompt. The platform
# stays domain-agnostic: it composes whatever contributors are installed and
# never names a specific consumer. A contributor returns a list of fragment
# dicts, each:
#   {"layer": "identity"|"policies"|"capabilities"|...,
#    "name": str, "content": str, "source": str, "required": bool}
# (mirrors the ``axiom.portfolio_member`` discovery in axiom.infra.branding.)
#
# A contributor takes either no arguments, which is what the group has always
# accepted, or one positional ``PromptContext`` describing the request it is
# contributing to. See ``_accepts_prompt_context`` for how the two are told
# apart and ``axiom.infra.prompt_context`` for what the context may carry.
PROMPT_CONTRIBUTOR_GROUP = "axiom.chat.prompt_contributor"


def _action_for(
    tool_name: str, params: dict[str, Any] | None, all_tools: dict[str, ToolDef]
) -> Action:
    """Classify a tool call from the table the model was offered.

    Every ``ToolDef`` in that table already carries the approval category
    the shared projector derived (ADR-072: built-ins, extension tools and
    projected capabilities alike), so the chat surface gates exactly what
    it advertised. A name outside the table falls back to the orchestrator
    registry, where an unknown name stays WRITE.
    """
    tool_def = all_tools.get(tool_name)
    if tool_def is None:
        return create_action(tool_name, params)
    return Action(name=tool_name, params=params or {}, category=tool_def.category)


def _completion_subject(tool_name: str) -> str:
    """Bus subject announcing a finished tool call: ``write_file`` -> ``write.file.complete``.

    A projected capability keeps its dotted name (``scan__status`` ->
    ``scan.status.complete``): the surface's ``__`` separator collapses to
    one dot, so it never yields the empty token the subject grammar rejects.
    """
    return f"{re.sub(r'_+', '.', tool_name)}.complete"


def _accepts_prompt_context(contribute: Any) -> bool:
    """Whether ``contribute`` has a positional slot for the request context.

    True when the callable declares a positional parameter, with or without a
    default, or accepts ``*args``. False for a callable that declares none,
    for one whose only parameters are keyword-only or ``**kwargs``, and for
    one whose signature cannot be read at all: an unreadable signature is not
    evidence that a context is wanted.

    The alternative was to call the contributor with a context and, on
    ``TypeError``, call it again with none. That reads the wrong signal. A
    ``TypeError`` raised inside a contributor's own body looks exactly like
    one raised by the interpreter for a bad call, so the retry would run the
    contributor's side effects twice and could accept a fragment the
    contributor composed without the context it had asked for. Deciding
    before the call keeps the two apart, and can be tested directly.
    """
    import inspect

    try:
        params = inspect.signature(contribute).parameters
    except (TypeError, ValueError):
        return False
    positional = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.VAR_POSITIONAL,
    )
    return any(param.kind in positional for param in params.values())


def _discover_prompt_contributions(context: PromptContext | None = None) -> list[dict]:
    """Collect system-prompt fragments contributed by installed extensions.

    ``context`` describes the request being composed for and is handed to
    every contributor that declares a positional parameter. A contributor
    that declares none is called with no arguments, exactly as it always
    was. Omitting ``context`` stands a neutral empty one in, so a contributor
    that asked for a context always receives one.

    Never raises: a contributor that is missing, broken, returns a bad shape,
    or mishandles the context is logged and skipped so the chat agent always
    builds a prompt.
    """
    import logging
    from importlib.metadata import entry_points

    log = logging.getLogger(__name__)
    ctx = context if context is not None else PromptContext()
    fragments: list[dict] = []
    try:
        eps = entry_points(group=PROMPT_CONTRIBUTOR_GROUP)
    except Exception as exc:
        log.warning("prompt_contributor entry-points lookup failed: %s", exc)
        return fragments
    for ep in eps:
        try:
            contribute = ep.load()
            produced = contribute(ctx) if _accepts_prompt_context(contribute) else contribute()
            for frag in produced or []:
                if frag.get("layer") and frag.get("name") and frag.get("content"):
                    fragments.append(frag)
        except Exception as exc:
            log.warning("prompt contributor %r failed: %s", getattr(ep, "name", ep), exc)
    return fragments


def _prompt_context_for(scope: ChatScope) -> PromptContext:
    """Project ``scope`` onto the field set a prompt contributor may see.

    This is the whole disclosure, written out one field at a time so a
    reviewer can read it: the acting principal and the conversation
    identifier off the session, the interaction mode, and the workspace
    brief. Everything else a scope holds stays on the scope, and the scope
    itself is never handed to a contributor. See
    ``axiom.infra.prompt_context`` for why each field is in and what is
    deliberately out.

    Reads every field defensively, so building a prompt cannot fail on the
    shape of the object it was given.
    """
    session = getattr(scope, "session", None)
    return PromptContext(
        principal_id=getattr(session, "principal_id", "") or "",
        session_id=getattr(session, "session_id", "") or "",
        interaction_mode=getattr(scope, "interaction_mode", "") or "",
        workspace_context=getattr(scope, "workspace_context", "") or "",
    )


DEFAULT_SCOPE_ATTR = "_default_scope"


def _active_scope(agent: Any, scope: ChatScope | None) -> ChatScope:
    """Return the conversation this call runs in.

    Two candidates and no others: the scope handed to the call, which wins,
    and otherwise the agent's own default. Nothing here consults ambient
    state, so reading a call site tells you which conversation it touches.

    The default is resolved out of the instance dictionary and created on
    first use, so it exists however the agent was built.
    """
    if scope is not None:
        return scope
    default = agent.__dict__.get(DEFAULT_SCOPE_ATTR)
    if default is None:
        default = ChatScope()
        agent.__dict__[DEFAULT_SCOPE_ATTR] = default
    return default


def _scoped(field: str, doc: str) -> property:
    """Expose one of the default scope's fields as an agent attribute.

    Every caller that has always reached for ``agent.session`` (or any other
    name below) keeps working unchanged; the value it reads and writes is the
    default scope's. A request that brought its own scope never comes through
    here at all.
    """

    def _get(self: ChatAgent) -> Any:
        return getattr(_active_scope(self, None), field)

    def _set(self: ChatAgent, value: Any) -> None:
        setattr(_active_scope(self, None), field, value)

    return property(_get, _set, doc=doc)


class ChatTurnCancelled(Exception):
    """Raised when a turn is interrupted by the user via threading.Event."""


def _cancellable_stream(chunks, cancel_event, poll: float = 0.05):
    """Yield from a blocking stream while staying answerable to cancellation.

    A provider generator blocks until it has something to say, and the poll
    that watches for Ctrl+C lived *inside* the loop over it. So for as long as
    the model took to produce its first token — precisely the window in which a
    person reaches for Ctrl+C — the flag was set and nothing read it.

    The provider call cannot be interrupted from outside. What can be stopped
    is *waiting* on it: a worker pumps the stream into a queue while the
    consumer polls that queue on a timeout and answers the cancel flag between
    polls. On cancellation the consumer raises immediately and the worker is
    left to finish into a queue nobody reads. The abandoned request costs one
    connection until the provider is done with it, which is the price of
    answering the user now rather than whenever the model gets around to it.

    An exception raised by the producer is carried across the thread boundary
    and re-raised here: moving iteration onto a worker must not turn a provider
    error into silence.
    """
    import queue as _queue
    import threading as _threading

    handoff: _queue.Queue = _queue.Queue()
    done = object()

    def _pump():
        try:
            for chunk in chunks:
                handoff.put((None, chunk))
        except BaseException as exc:  # noqa: BLE001 — re-raised in the consumer
            handoff.put((exc, None))
        finally:
            handoff.put((None, done))

    worker = _threading.Thread(target=_pump, daemon=True)
    worker.start()

    while True:
        try:
            error, item = handoff.get(timeout=poll)
        except _queue.Empty:
            # Only when there is nothing to hand on. A chunk the provider
            # already produced is delivered first, because the surrounding
            # contract is that a produced chunk is never lost to a race with
            # cancellation or the clock — the consumer's own polls decide what
            # to do about it afterwards. This wrapper's job is narrower: stop
            # *waiting* forever on a provider that has not spoken yet.
            if cancel_event.is_set():
                raise ChatTurnCancelled("Turn cancelled by user")
            continue
        if error is not None:
            raise error
        if item is done:
            return
        yield item


class ChatTurnDeadlineExceeded(Exception):
    """Raised inside a turn that has used up its wall-clock budget.

    Deliberately not a ``ChatTurnCancelled``: nobody interrupted, the clock
    ran out, and a handler for one must never swallow the other. ``turn``
    catches this and returns ``DEADLINE_MESSAGE`` rather than raising, so a
    caller sees a message for a timeout and an exception for an interruption.
    """


def _now() -> float:
    """Read the monotonic clock.

    One seam, so a test can drive a turn past its deadline without sleeping.
    """
    return time.monotonic()


def _wrap_up_reserve(deadline: float, slowest_round: float) -> float:
    """Seconds held back at the end of a turn for the model to answer in.

    Below this much remaining, the loop stops offering tools so the model
    synthesizes an answer from what it already gathered instead of opening a
    tool round it has no time to finish and being cut off mid-thought.

    "Too little time" is judged from the turn's own behaviour: enough for
    another round as slow as the slowest one this turn has already run. Before
    any round has finished there is nothing to measure, so a fixed share of the
    whole budget stands in, and that share is also the floor once measurements
    exist, because a run of quick rounds says little about the next one.
    """
    return max(slowest_round, TURN_WRAP_UP_RESERVE * deadline)


def _raise_if_out_of_time(scope: ChatScope) -> None:
    """Raise ``ChatTurnDeadlineExceeded`` once ``scope``'s turn is out of time.

    A scope with no deadline never raises, which is what makes an unbounded
    conversation behave exactly as it did before deadlines existed.
    """
    remaining = scope.time_remaining(_now())
    if remaining is not None and remaining <= 0:
        raise ChatTurnDeadlineExceeded(f"turn exceeded its {scope.turn_deadline}s budget")


class _ChunkNotifier:
    """Hands each streaming chunk to a scope's consumer, and absorbs its failures.

    The consumer belongs to whoever made the request and sits downstream of a
    network this process does not control: a browser reading server-sent
    events closes the tab and the next write raises. The turn is still worth
    finishing, because its answer goes into the session and its tool calls
    have already run, so a consumer that fails is reported and generation
    carries on rather than dying with it.

    Reported once per stream rather than once per chunk. A consumer that has
    gone away fails on every token, and one log line per token turns a
    disconnected browser into a denial of service against the log. The
    failure is never swallowed silently: the first one is logged with its
    traceback.

    ``close`` is the end-of-stream signal for a consumer that holds buffered
    state. It runs exactly once, from the streaming helper's ``finally``, so
    it happens whether the stream ended normally, was cancelled, or ran out
    of time. A consumer with no ``close`` is left alone.
    """

    def __init__(self, consumer: Callable[[StreamChunk], None] | None) -> None:
        self._consumer = consumer
        self._reported = False

    def __call__(self, chunk: StreamChunk) -> None:
        if self._consumer is None:
            return
        try:
            self._consumer(chunk)
        except Exception:
            self._report()

    def close(self) -> None:
        consumer, self._consumer = self._consumer, None
        close = getattr(consumer, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:
            self._report()

    def _report(self) -> None:
        if self._reported:
            return
        self._reported = True
        _log.warning(
            "chat stream consumer raised; the turn continues and further "
            "failures on this stream are not logged",
            exc_info=True,
        )


class ChatAgent:
    """Interactive agent with native tool calling and approval gates.

    Holds only what belongs to the process. Everything that belongs to a
    conversation is on a ``ChatScope``; the agent keeps one default scope for
    the surface that built it, and serves any number of others through the
    ``scope`` argument on ``turn``.
    """

    #: Agent attribute -> the ``ChatScope`` field it reads and writes. Every
    #: name here used to be instance state on the agent and is now a view of
    #: one conversation's state, so no caller had to change.
    SCOPED_ATTRIBUTES: dict[str, str] = {
        "session": "session",
        "permissions": "permissions",
        "gate": "gate",
        "last_turn_tools": "last_turn_tools",
        "_session_allowlist": "allowlist",
        "_session_mode": "session_mode",
        "_interaction_mode": "interaction_mode",
        "_pending_images": "pending_images",
        "_workspace_context": "workspace_context",
        "_last_retrieved": "last_retrieved",
        "_last_composer": "last_composer",
        "_turn_query": "turn_query",
        "_turn_start": "turn_start",
        "_cancel_event": "cancel_event",
    }

    session = _scoped("session", "The default scope's message history.")
    permissions = _scoped(
        "permissions",
        "The default scope's per-tool allow/ask/deny map. Set by the operator "
        "choosing 'A'/'D' at an approval prompt or via /permissions, and "
        "persisted to $AXI_STATE_DIR/tool_permissions.json.",
    )
    gate = _scoped("gate", "The default scope's approval gate.")
    last_turn_tools = _scoped(
        "last_turn_tools",
        "Names of the tools the default scope's most recent turn ran, in "
        "order. Read by the post-turn advisor hook; reset at every turn.",
    )
    _session_allowlist = _scoped(
        "allowlist", "Tools the default scope approved for the rest of its life."
    )
    _session_mode = _scoped("session_mode", "The default scope's routing tier hint (--mode flag).")
    _interaction_mode = _scoped("interaction_mode", "The default scope's ask/plan/agent mode.")
    _pending_images = _scoped(
        "pending_images",
        "Images queued by `/image <path>` for the default scope's next turn. "
        "Injected into the API messages list, not the session history, so "
        "storage stays text-only while the model sees the image.",
    )
    _workspace_context = _scoped("workspace_context", "The default scope's workspace brief.")
    _last_retrieved = _scoped(
        "last_retrieved", "Chunks retrieved for the default scope's in-flight turn."
    )
    _last_composer = _scoped(
        "last_composer", "PromptComposer from the default scope's last system prompt."
    )
    _turn_query = _scoped("turn_query", "The default scope's in-flight query.")
    _turn_start = _scoped("turn_start", "When the default scope's turn started.")
    _cancel_event = _scoped("cancel_event", "Cancellation for the default scope's in-flight turn.")

    def __init__(
        self,
        gateway: Gateway | None = None,
        bus: EventBus | None = None,
        session: Session | None = None,
        render: RenderProvider | None = None,
        permissions: ToolPermissions | None = None,
        approval_policy: ApprovalPolicy | None = None,
    ):
        self.gateway = gateway or Gateway()
        self.bus = bus or EventBus()
        self.usage = UsageTracker()
        # The conversation this agent runs when a caller passes no scope. A
        # terminal never passes one, so this is the whole of its state. Its
        # permission map is the operator's persisted file (a bare ChatScope
        # starts in memory instead) unless a caller injects its own.
        self._default_scope = ChatScope(
            session=session or Session(),
            permissions=(
                permissions if permissions is not None else ToolPermissions.load_default()
            ),
        )
        self._render = render
        # How this surface answers the approval gate. Left unset, a pending
        # write prompts an operator exactly as it always has. Set, the policy
        # decides instead and nothing prompts, which is how a surface with no
        # keyboard declares itself. The platform never infers this from
        # ``isatty`` or any other property of the process.
        self._approval_policy = approval_policy
        self._router = QueryRouter()
        # Backward-compat: bare callback for tests
        self._renderer_callback: Callable[[Iterator[StreamChunk]], str] | None = None
        # RAG store — lazily initialized if rag.database_url is configured
        self._rag_store: Any | None = None
        self._rag_init_attempted = False

    @property
    def scope(self) -> ChatScope:
        """The conversation this agent runs when a caller passes no scope."""
        return _active_scope(self, None)

    def cancel(self, *, scope: ChatScope | None = None) -> None:
        """Signal cancellation of the in-flight turn in ``scope``."""
        _active_scope(self, scope).cancel_event.set()

    def is_cancelled(self, *, scope: ChatScope | None = None) -> bool:
        """Return True if a cancellation has been requested for ``scope``."""
        return _active_scope(self, scope).cancel_event.is_set()

    def reset_cancel(self, *, scope: ChatScope | None = None) -> None:
        """Clear ``scope``'s cancellation flag before starting a new turn."""
        _active_scope(self, scope).cancel_event.clear()

    def allowlisted_tools(self, *, scope: ChatScope | None = None) -> list[str]:
        """Return sorted list of tools ``scope`` always approves."""
        return sorted(_active_scope(self, scope).allowlist)

    def revoke_allowlist(
        self, tool_name: str | None = None, *, scope: ChatScope | None = None
    ) -> None:
        """Remove a tool from ``scope``'s allowlist, or clear all if None."""
        allowlist = _active_scope(self, scope).allowlist
        if tool_name is None:
            allowlist.clear()
        else:
            allowlist.discard(tool_name)

    def set_renderer(self, callback: Callable[[Iterator[StreamChunk]], str]) -> None:
        """Set a streaming renderer callback (backward-compat)."""
        self._renderer_callback = callback

    def set_render_provider(self, render: RenderProvider) -> None:
        """Set the render provider for rich output."""
        self._render = render

    @property
    def approval_policy(self) -> ApprovalPolicy | None:
        """The declared approval policy, or ``None`` while approvals prompt."""
        return self._approval_policy

    def set_approval_policy(self, policy: ApprovalPolicy | None) -> None:
        """Declare how this surface answers approvals.

        A policy decides without asking anyone, so a surface with no operator
        stops depending on a prompt. ``None`` restores the interactive path.
        """
        self._approval_policy = policy

    INTERACTION_MODES = ("ask", "plan", "agent")

    def set_interaction_mode(self, mode: str, *, scope: ChatScope | None = None) -> None:
        """Set ``scope``'s chat interaction mode.

        - ``ask``  : single completion, no tools at all (Q&A only)
        - ``plan`` : tools listed but model directed to plan-then-stop
        - ``agent``: full autonomous tool-use loop (default)
        """
        if mode not in self.INTERACTION_MODES:
            raise ValueError(
                f"unknown interaction mode {mode!r} (expected one of {self.INTERACTION_MODES})"
            )
        _active_scope(self, scope).interaction_mode = mode

    def turn(
        self,
        user_input: str,
        stream: bool = True,
        *,
        raw: bool = False,
        scope: ChatScope | None = None,
    ) -> str:
        """Process one user turn in ``scope`` and return the assistant response.

        ``scope`` is the conversation this turn belongs to. Passed, the turn
        reads and writes that scope and nothing else: a worker serving many
        people builds one per request and no answer given in one can reach
        another. Omitted, the turn runs in the agent's default scope, which is
        what a terminal has always done.

        Thin wrapper that applies any per-prompt provider override
        (spec-chat-model-picker §3) before dispatching to the impl. The
        override is restored when the turn finishes, success or failure.
        """
        from axiom.chat.picker import apply_per_prompt_override

        scope = _active_scope(self, scope)
        self.reset_cancel(scope=scope)
        scope.last_turn_tools = []
        with apply_per_prompt_override(user_input, self.gateway) as picker:
            if picker.error_message:
                return picker.error_message
            return self._turn_impl(picker.stripped_prompt, stream=stream, raw=raw, scope=scope)

    def _turn_impl(
        self,
        user_input: str,
        stream: bool = True,
        *,
        raw: bool = False,
        scope: ChatScope | None = None,
    ) -> str:
        """Process one user turn and return the assistant response.

        Multi-turn tool-use loop:
        1. Add user message to session
        2. Build messages + system prompt
        3. Call Gateway with tools
        4. If tool_use in response: execute, store results, loop
        5. Return final text response

        When ``raw=True`` (the benchmarking bypass for Issue 2):

        - the system prompt is empty (no identity, policies, RAG, CLAUDE.md);
        - messages contain only the single user turn;
        - no tools are exposed (single gateway call, no tool-use loop);
        - the session is not mutated (fully ephemeral — no pollution of
          durable history with benchmark traffic).

        The routing-classifier audit log still fires under ``raw=True`` —
        that's metadata, not augmentation. Default is ``raw=False``.

        The loop runs at most ``scope.max_tool_rounds`` rounds and, when the
        scope carries a deadline, stops once ``scope.turn_deadline`` seconds
        have passed. A budget that could not run a turn raises ``ValueError``
        before any model call is made.
        """
        scope = _active_scope(self, scope)
        scope.validate_budgets()

        # Classify before adding to session so context window = prior turns only
        routing = self._router.classify(
            user_input,
            session_mode=scope.session_mode,
            context=scope.session.messages[-10:],
        )
        routing_tier = routing.tier.value

        # Audit log — record routing decision (no plaintext).
        # Fires in BOTH normal and raw modes; the audit is metadata, not
        # augmentation.
        try:
            from axiom.infra.routing_audit import hash_query, log_routing_decision

            log_routing_decision(
                session_id=getattr(scope.session, "id", ""),
                query_hash=hash_query(user_input),
                tier=routing_tier,
                classifier=routing.classifier,
                provider=getattr(self.gateway, "_provider_override", ""),
                matched_terms=routing.matched_terms,
                reason=routing.reason,
            )
        except Exception:
            pass  # audit is best-effort; never block the chat loop

        # ---- raw bypass --------------------------------------------------
        # Single-shot, ephemeral, no augmentation. Used by benchmark
        # harnesses to compare wrapped vs. naked model output.
        if raw:
            return self._raw_turn(user_input, routing_tier=routing_tier, routing_decision=routing)

        scope.session.add_message("user", user_input)

        # Stash for T0-1 retrieval audit log (written at turn completion).
        scope.turn_query = user_input
        scope.turn_start = _now()

        system = self._build_system_prompt(scope=scope)
        messages = self._build_messages(scope=scope)
        all_tools = get_all_tools()
        tools = get_tool_definitions(all_tools)

        # Inject queued image attachments into the just-added user message.
        # Session storage stays text-only; only the API call sees the bytes.
        if scope.pending_images and messages and messages[-1].get("role") == "user":
            from .attachments import build_user_message, detect_provider_kind

            provider = self.gateway.active_provider
            kind = detect_provider_kind(provider) if provider else "openai"
            last_text = messages[-1].get("content", "")
            if isinstance(last_text, str):
                messages[-1] = build_user_message(last_text, scope.pending_images, kind)
            scope.pending_images = []

        # Apply interaction mode (ask/plan/agent) — see set_interaction_mode.
        if scope.interaction_mode == "ask":
            tools = None  # no tool surface; pure Q&A turn
        elif scope.interaction_mode == "plan":
            # Build comma-joined list of tool names for the system prompt,
            # then strip tools from the API call so no tool_use blocks fire.
            all_tool_names = ", ".join(t["function"]["name"] for t in (tools or []))
            system = (
                system + "\n\n--- PLAN MODE ---\n"
                "You are in PLAN mode. Produce a numbered plan describing what "
                "you would do, which tools you would call (by name) and in what "
                "order, and what the user should review before approval. "
                "DO NOT call any tools in this turn — output the plan as text only. "
                f"Available tools you can name in the plan: {all_tool_names}\n"
                "End with a single line: `Reply 'go' (or switch to agent mode) to execute.`"
            )
            tools = None  # hard-strip: prevent any tool_use blocks in plan mode

        response = None
        max_rounds = scope.max_tool_rounds
        slowest_round = 0.0
        for _round in range(max_rounds):
            # A round that starts after the user cancelled must not start at
            # all — the same rule the deadline check below already applies.
            #
            # Without this, cancellation was only ever read between streaming
            # chunks and between tool executions. Only round 0 streams (see
            # ``use_stream`` below), so once a turn made a tool call nothing
            # read the flag again and Ctrl+C did nothing for the rest of the
            # turn. A single in-flight provider call still cannot be
            # interrupted from outside; the round is the granularity a
            # tool-using turn actually has.
            if scope.cancel_event.is_set():
                raise ChatTurnCancelled("Turn cancelled by user")

            # A round that starts after the deadline must not start at all.
            round_started = _now()
            remaining = scope.time_remaining(round_started)
            if remaining is not None and remaining <= 0:
                return self._out_of_time(response, scope=scope)

            # First round streams to show immediate output.
            # Subsequent rounds (after tool results) use non-streaming to
            # prevent the model from re-rendering text it already showed.
            use_stream = stream and _round == 0

            # On the final allowed round, withhold the tool surface so the
            # model is forced to synthesize a text answer from everything it
            # has already retrieved — rather than requesting yet another tool
            # call, exhausting the loop, and returning the apology fallback as
            # if it were a real answer.
            #
            # Running low on time does the same thing for the same reason: a
            # tool round started with no time to finish it ends as a cut-off
            # turn, where wrapping up now ends as an answer.
            is_final_round = _round == max_rounds - 1
            out_of_time_for_tools = remaining is not None and remaining <= _wrap_up_reserve(
                scope.turn_deadline or 0.0, slowest_round
            )
            round_tools = None if (is_final_round or out_of_time_for_tools) else tools

            try:
                # Stream only when something is watching. A chunk consumer on
                # the scope counts: a serving surface installs one and no
                # renderer at all, and without this it would take the
                # non-streaming path and the consumer would never fire.
                if (
                    use_stream
                    and not is_final_round
                    and self.gateway.available
                    and (self._render or self._renderer_callback or scope.on_chunk)
                ):
                    response = self._streaming_turn(
                        messages, system, round_tools, routing_tier, scope=scope
                    )
                elif self.gateway.available:
                    response = self._non_streaming_turn(
                        messages, system, round_tools, routing_tier, routing_decision=routing
                    )
                else:
                    response = self._legacy_turn(user_input, system, scope=scope)
            except ChatTurnDeadlineExceeded:
                # The clock ran out mid-round. Nobody interrupted, so this
                # ends as a message rather than as a cancellation.
                return self._out_of_time(response, scope=scope)

            slowest_round = max(slowest_round, _now() - round_started)

            # Record usage for this API call
            self._record_usage(response)

            # If no tool calls, we're done
            if not response.tool_use:
                # If this was a non-streamed round, render the final text now
                if not use_stream and response.text and self._render:
                    self._render.render_message("assistant", response.text)
                scope.session.add_message("assistant", response.text)
                self._schedule_session_index(scope=scope)
                self._check_answer_provenance(response.text, scope=scope)
                self._log_rag_audit(response.text, scope=scope)
                self._log_prompt_observability(scope=scope)
                return response.text

            # Process tool calls, classified from the table the model was offered
            tool_results = self._process_tool_calls(response, all_tools, scope=scope)

            # Build the assistant message with tool calls
            assistant_tool_calls = [
                {"name": t.name, "id": t.tool_id, "input": t.input} for t in response.tool_use
            ]

            # Store in session
            scope.session.add_message(
                "assistant",
                response.text,
                tool_calls=assistant_tool_calls,
            )

            # Add to working messages for next API round
            messages.append(
                {
                    "role": "assistant",
                    "content": response.text or "",
                    "tool_calls": [
                        {
                            "id": t.tool_id,
                            "type": "function",
                            "function": {
                                "name": t.name,
                                "arguments": json.dumps(t.input),
                            },
                        }
                        for t in response.tool_use
                    ],
                }
            )

            # Add tool results as messages (both session and working list)
            for tool_id, name, result in tool_results:
                result_json = json.dumps(result)
                scope.session.add_message(
                    "tool",
                    result_json,
                    tool_calls=[
                        {"tool_call_id": tool_id, "name": name},
                    ],
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": name,
                        "content": result_json,
                    }
                )

            # Rebuild messages for next round (trim for context window)
            messages = self._trim_messages(messages, system)

        # Exceeded max rounds
        fallback = (response.text if response else None) or ROUNDS_EXHAUSTED_MESSAGE
        scope.session.add_message("assistant", fallback)
        self._schedule_session_index(scope=scope)
        return fallback

    def _out_of_time(
        self, response: CompletionResponse | None, *, scope: ChatScope | None = None
    ) -> str:
        """End a turn that used up its wall-clock budget.

        Keeps whatever the model managed to say and appends the time-limit
        notice, so the reader gets the partial work and knows why it stopped.
        The notice is its own sentence and differs from the round-exhaustion
        one, because those are different failures with different fixes.
        """
        scope = _active_scope(self, scope)
        partial = ((response.text if response else "") or "").strip()
        message = f"{partial}\n\n{DEADLINE_MESSAGE}" if partial else DEADLINE_MESSAGE
        scope.session.add_message("assistant", message)
        self._schedule_session_index(scope=scope)
        return message

    def _raw_turn(
        self,
        user_input: str,
        routing_tier: str = "any",
        routing_decision: Any = None,
    ) -> str:
        """Single-shot bypass — no system prompt, no RAG, no tools, no session.

        Raw-model benchmark support: lets benchmarks measure raw
        model quality through Axiom's HTTP wrapper without any of the
        augmentation layers that the normal path adds.

        Behavior contract:

        - ``system=""`` (no identity, policies, retrieved context);
        - ``messages=[{"role": "user", "content": user_input}]`` (the bare
          user turn — no prior history, no RAG block);
        - ``tools=None`` (no tool surface, single gateway call, no loop);
        - no session is mutated, in any scope (no add_message before,
          during, or after). This helper is handed no scope for that reason.

        The routing-classifier audit still fires (it's emitted in
        :meth:`turn` before this helper runs). Usage tokens are still
        recorded against the agent's ``UsageTracker`` for cost accounting.
        """
        messages = [{"role": "user", "content": user_input}]

        if self.gateway.available:
            response = self.gateway.complete_with_tools(
                messages=messages,
                system="",
                tools=None,
                routing_tier=routing_tier,
                routing_decision=routing_decision,
            )
        else:
            # Stub mode — no provider configured. Fall back to plain
            # complete() so the endpoint still produces something.
            stub = self.gateway.complete(
                prompt=user_input,
                system="",
                task="chat",
                max_tokens=2000,
            )
            response = CompletionResponse(
                text=stub.text,
                provider=stub.provider,
                model=stub.model,
                success=stub.success,
                error=stub.error,
            )

        self._record_usage(response)
        return response.text or ""

    def _record_usage(self, response: CompletionResponse) -> None:
        """Record usage from a completion response."""
        model = response.model or (
            self.gateway.active_provider.model if self.gateway.active_provider else ""
        )
        self.usage.record_turn(
            TurnUsage(
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cache_read_tokens=response.cache_read_tokens,
                model=model,
            )
        )

    def _streaming_turn(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        routing_tier: str = "any",
        *,
        scope: ChatScope | None = None,
    ) -> CompletionResponse:
        """Execute a streaming turn and collect the full response.

        Polls ``scope``'s cancellation, so cancelling one conversation never
        interrupts another's stream, and ``scope``'s deadline, because one
        round can be long on its own and a bound that only fires between
        rounds is not a bound. Cancellation is checked first: a person who
        interrupted gets the interrupted message even if the clock also ran
        out.

        Two ways to watch the stream, and they run together. The render
        function is handed the iterator and owns the loop, which is the right
        shape for a terminal and no use to a caller that has to act per delta.
        ``scope.on_chunk`` is that caller's seam: it is called with every
        chunk, in whichever branch below runs, and before the cancellation and
        deadline checks, so a chunk that was produced is delivered rather than
        lost to a race with the clock. It is told the stream is over from the
        ``finally``, so a consumer holding a partial clause flushes it whether
        the turn finished, was interrupted, or ran out of time.
        """
        scope = _active_scope(self, scope)
        cancel_event = scope.cancel_event
        notify = _ChunkNotifier(scope.on_chunk)
        chunks = self.gateway.stream_with_tools(
            messages=messages,
            system=system,
            tools=tools,
            routing_tier=routing_tier,
        )
        # Answerable to Ctrl+C while the provider is still thinking — the polls
        # below only run once a chunk exists, and the wait for the first one is
        # exactly when a person interrupts.
        chunks = _cancellable_stream(chunks, cancel_event)

        # Collect chunks into a CompletionResponse
        text_parts = []
        tool_blocks: dict[str, dict[str, str]] = {}  # tool_id -> {name, input_json}
        thinking_parts = []
        usage_input = 0
        usage_output = 0
        usage_cache = 0

        # Render callback: prefer provider, fall back to bare callback
        render_fn = None
        if self._render:
            render_fn = self._render.stream_text
        elif self._renderer_callback:
            render_fn = self._renderer_callback

        try:
            if render_fn:
                # Create a tee iterator — render while collecting
                collected_chunks = []

                def tee_chunks():
                    for c in chunks:
                        collected_chunks.append(c)
                        # Push before the polls below, so a consumer is handed
                        # the chunk that was produced even when it is the last
                        # one the clock allows.
                        notify(c)
                        yield c
                        # Poll between chunks; raise after yielding so the
                        # renderer sees at most one chunk before cancellation.
                        if cancel_event.is_set():
                            raise ChatTurnCancelled("Turn cancelled by user")
                        _raise_if_out_of_time(scope)

                render_fn(tee_chunks())

                # Reconstruct from collected chunks
                for c in collected_chunks:
                    if c.type == "text":
                        text_parts.append(c.text)
                    elif c.type == "tool_use_start":
                        tool_blocks[c.tool_id] = {"name": c.tool_name, "input_json": ""}
                    elif c.type == "tool_input_delta":
                        if c.tool_id in tool_blocks:
                            tool_blocks[c.tool_id]["input_json"] += c.tool_input_json
                    elif c.type == "tool_use_end":
                        if c.tool_id in tool_blocks:
                            tool_blocks[c.tool_id]["input_json"] = c.tool_input_json
                    elif c.type == "thinking_delta":
                        thinking_parts.append(c.text)
                    elif c.type == "usage":
                        usage_input += c.input_tokens
                        usage_output += c.output_tokens
                        usage_cache += c.cache_read_tokens
            else:
                for c in chunks:
                    # Same push, same reason, and it has to be here too: this
                    # is the branch a surface with no renderer takes, which is
                    # every serving surface.
                    notify(c)
                    if cancel_event.is_set():
                        raise ChatTurnCancelled("Turn cancelled by user")
                    _raise_if_out_of_time(scope)
                    if c.type == "text":
                        text_parts.append(c.text)
                    elif c.type == "tool_use_start":
                        tool_blocks[c.tool_id] = {"name": c.tool_name, "input_json": ""}
                    elif c.type == "tool_input_delta":
                        if c.tool_id in tool_blocks:
                            tool_blocks[c.tool_id]["input_json"] += c.tool_input_json
                    elif c.type == "tool_use_end":
                        if c.tool_id in tool_blocks:
                            tool_blocks[c.tool_id]["input_json"] = c.tool_input_json
                    elif c.type == "thinking_delta":
                        thinking_parts.append(c.text)
                    elif c.type == "usage":
                        usage_input += c.input_tokens
                        usage_output += c.output_tokens
                        usage_cache += c.cache_read_tokens
        finally:
            # The stream is over however it ended. A consumer holding a
            # partial clause flushes it here, so the last one is not lost to
            # a cancellation or to the clock.
            notify.close()

        # Render thinking block if present
        if thinking_parts and self._render:
            self._render.render_thinking("".join(thinking_parts))

        from axiom.infra.gateway import ToolUseBlock

        tool_use_list = []
        for tid, info in tool_blocks.items():
            try:
                parsed_input = json.loads(info["input_json"]) if info["input_json"] else {}
            except json.JSONDecodeError:
                parsed_input = {}
            tool_use_list.append(
                ToolUseBlock(
                    tool_id=tid,
                    name=info["name"],
                    input=parsed_input,
                )
            )

        return CompletionResponse(
            text="".join(text_parts),
            tool_use=tool_use_list,
            provider=self.gateway.active_provider.name if self.gateway.active_provider else "stub",
            model=self.gateway.active_provider.model if self.gateway.active_provider else "",
            success=True,
            input_tokens=usage_input,
            output_tokens=usage_output,
            cache_read_tokens=usage_cache,
        )

    def _non_streaming_turn(
        self,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]],
        routing_tier: str = "any",
        routing_decision: Any = None,
    ) -> CompletionResponse:
        """Execute a non-streaming turn with tool-use.

        ``routing_decision`` is plumbed through to the gateway so that
        if the request is blocked (e.g. EC content with no EC provider
        configured), the user-visible error message can surface the
        matched keyword + classifier stage.
        """
        return self.gateway.complete_with_tools(
            messages=messages,
            system=system,
            tools=tools,
            routing_tier=routing_tier,
            routing_decision=routing_decision,
        )

    def _legacy_turn(
        self,
        user_input: str,
        system: str,
        *,
        scope: ChatScope | None = None,
    ) -> CompletionResponse:
        """Fallback: text-only prompt without native tool-use.

        Used when the gateway has no providers (stub mode) or provider
        doesn't support tool-use. The recent history it flattens into the
        prompt is ``scope``'s, never another conversation's.
        """
        # Build a flat text prompt with tool descriptions
        all_tools = get_all_tools()
        tools_desc = "\n".join(
            f"- {t.name}: {t.description} ({'read' if t.category == ActionCategory.READ else 'write'})"
            for t in all_tools.values()
        )

        recent = _active_scope(self, scope).session.messages[-6:]
        parts = []
        for msg in recent[:-1]:
            parts.append(f"[{msg.role}] {msg.content}")
        parts.append(f"[user] {user_input}")
        parts.append(f"\nAvailable tools:\n{tools_desc}")
        prompt = "\n".join(parts)

        response = self.gateway.complete(
            prompt=prompt,
            system=system,
            task="chat",
            max_tokens=2000,
        )

        # Parse text-based tool calls for legacy mode
        tool_use = self._parse_legacy_tool_calls(response.text)

        return CompletionResponse(
            text=response.text,
            tool_use=tool_use,
            provider=response.provider,
            model=response.model,
            success=response.success,
            error=response.error,
        )

    def _parse_legacy_tool_calls(self, text: str) -> list:
        """Extract tool calls from legacy [tool: name] {params} format."""
        from axiom.infra.gateway import ToolUseBlock

        calls = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("[tool:"):
                try:
                    name_end = line.index("]")
                    name = line[6:name_end].strip()
                    params_str = line[name_end + 1 :].strip()
                    params = json.loads(params_str) if params_str else {}
                    calls.append(
                        ToolUseBlock(
                            tool_id=f"legacy_{name}",
                            name=name,
                            input=params,
                        )
                    )
                except (ValueError, json.JSONDecodeError):
                    continue
        return calls

    def _process_tool_calls(
        self,
        response: CompletionResponse,
        all_tools: dict[str, ToolDef] | None = None,
        *,
        scope: ChatScope | None = None,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Execute ``scope``'s tool calls through ``scope``'s approval gate.

        ``all_tools`` is the tool table the model was offered this turn; each
        call is classified READ/WRITE from it (see ``_action_for``). It is
        rescanned when the caller holds none.

        Every approval decision made here is read from, and written back to,
        the conversation that asked: its permission map, its allowlist, its
        gate. An "always allow" answered in one conversation therefore says
        nothing about any other, which is what lets one agent serve many
        people.

        Returns list of (tool_id, tool_name, result_dict).
        """
        results = []
        if all_tools is None:
            all_tools = get_all_tools()
        scope = _active_scope(self, scope)

        for tool_block in response.tool_use:
            if scope.cancel_event.is_set():
                results.append((tool_block.tool_id, tool_block.name, {"cancelled": True}))
                continue

            scope.last_turn_tools.append(tool_block.name)

            action = _action_for(tool_block.name, tool_block.input, all_tools)
            scope.gate.submit(action)

            # Per-tool persisted permission overrides the prompt entirely.
            perm = scope.permissions.get(tool_block.name)
            if perm == "deny":
                scope.gate.reject(action.action_id, "Denied by tool permission")
                if self._render:
                    self._render.render_action_result(action)
                else:
                    from .renderer import render_action_result

                    render_action_result(action)
                results.append(
                    (
                        tool_block.tool_id,
                        tool_block.name,
                        {"error": "Denied by tool permission (set via /permissions)"},
                    )
                )
                continue
            if perm == "allow" and action.status == ActionStatus.PENDING:
                # User previously chose "Always allow" for this tool.
                scope.gate.approve(action.action_id)

            # If still pending (i.e. write action with no override), prompt.
            if action.status == ActionStatus.PENDING:
                if tool_block.name in scope.allowlist:
                    # Previously allowlisted — auto-approve without prompting
                    choice = "a"
                elif self._approval_policy is not None:
                    # This surface declared how it answers. No prompt is shown,
                    # on any front end, and the decision carries its reason.
                    choice = self._approval_policy.decide(action)
                elif self._render:
                    choice = self._render.render_approval_prompt(action)
                else:
                    from .renderer import render_approval_prompt

                    choice = render_approval_prompt(action)

                if choice == "A":
                    # Persist across sessions (tool_permissions.json).
                    scope.permissions.set(tool_block.name, "allow")
                    scope.gate.approve(action.action_id)
                elif choice == "a":
                    scope.gate.approve(action.action_id)
                elif choice == "D":
                    # Persist deny across sessions (tool_permissions.json).
                    scope.permissions.set(tool_block.name, "deny")
                    scope.gate.reject(action.action_id, "Denied (Always)")
                    if self._render:
                        self._render.render_action_result(action)
                    else:
                        from .renderer import render_action_result

                        render_action_result(action)
                    results.append(
                        (
                            tool_block.tool_id,
                            tool_block.name,
                            {"error": "Denied (Always) by user"},
                        )
                    )
                    continue
                else:
                    # A policy says why it refused; an operator's "r" does not.
                    # Either way the caller reads the same field.
                    why = getattr(choice, "reason", "")
                    refusal = f"Rejected by approval policy: {why}" if why else "Rejected by user"
                    scope.gate.reject(action.action_id, why or "User rejected")
                    if self._render:
                        self._render.render_action_result(action)
                    else:
                        from .renderer import render_action_result

                        render_action_result(action)
                    results.append(
                        (
                            tool_block.tool_id,
                            tool_block.name,
                            {"error": refusal},
                        )
                    )
                    continue

            # Execute approved action with timing
            t0 = time.monotonic()
            if self._render:
                self._render.render_tool_start(tool_block.name, tool_block.input)
            from axiom.infra.authority import (
                record_tool_refusal,
                register_authority_hook,
            )
            from axiom.infra.hooks import ApprovalRequired, HookDenied
            from axiom.infra.tool_gateway import dispatch_tool as _dispatch

            from .tool_errors import safe_run

            # Declared authority (P5): every chat tool call consults GUARD.
            # Idempotent; default-allow with a receipt until a site policy loads.
            register_authority_hook()

            principal_id = getattr(scope.session, "principal_id", "") or ""

            def _invoke_tool(
                _name=tool_block.name,
                _input=tool_block.input,
                _principal=principal_id,
            ):
                # Hook deny / approval-required are non-retryable structured
                # responses, NOT real exceptions. They short-circuit safe_run
                # by returning the dict directly so the LLM sees them as
                # tool results rather than typed errors.
                try:
                    return _dispatch(
                        tool_name=_name,
                        args=_input,
                        principal=_principal,
                        eventbus=self.bus,
                        dispatcher=execute_tool,
                        ext_origin="chat",
                    )
                except HookDenied as denial:
                    # A refusal is a first-class record, not just an error string.
                    record_tool_refusal(
                        tool_name=_name,
                        principal=_principal,
                        reason=denial.reason,
                        hook_source=denial.hook_source,
                        args=_input,
                    )
                    return {"error": f"denied by hook: {denial.reason}"}
                except ApprovalRequired as approval:
                    return {"error": f"approval required: {approval.reason}"}

            # safe_run handles the typed-error + jittered-retry policy and
            # NEVER raises — every result either is the tool's success dict
            # or a typed error dict the LLM can reason about.
            result = safe_run(_invoke_tool, tool_name=tool_block.name)

            elapsed = time.monotonic() - t0
            if "error" in result and "error_type" in result:
                action.fail(result["message"])
            else:
                action.complete(result)

            if self._render:
                self._render.render_tool_result(tool_block.name, result, elapsed)
            else:
                from .renderer import render_action_result

                render_action_result(action)

            self.bus.publish(
                _completion_subject(tool_block.name),
                {"action_id": action.action_id, "result": result},
                source="chat",
            )

            results.append((tool_block.tool_id, tool_block.name, result))

        return results

    def _get_rag_store(self) -> Any | None:
        """Lazily initialize the RAG store from settings.

        Resolves through ``axiom.rag.store_factory.create_store`` so the
        URL scheme picks the backend: ``postgresql://`` / ``sqlite:///``
        give a local store; ``http(s)://`` gives the served retrieval
        store of the site node this node is joined to. Chat never grows
        retrieval logic of its own. A bad or unreachable URL degrades to
        "no RAG context" and the turn still runs.
        """
        if self._rag_init_attempted:
            return self._rag_store
        self._rag_init_attempted = True
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            url = SettingsStore().get("rag.database_url", "")
            if not url:
                return None
            from axiom.rag.store_factory import create_store

            store = create_store(url)
            connect = getattr(store, "connect", None)
            if callable(connect):
                connect()
            self._rag_store = store
        except Exception as exc:  # noqa: BLE001 — RAG not available; chat still works
            _log.warning(
                "RAG store unavailable; continuing without RAG context (%s: %s)",
                type(exc).__name__,
                exc,
            )
        return self._rag_store

    def _log_prompt_observability(self, *, scope: ChatScope | None = None) -> None:
        """Emit the T0-3 prompt-composition observability record for ``scope``."""
        try:
            scope = _active_scope(self, scope)
            composer = scope.last_composer
            if composer is None:
                return
            from axiom.infra.prompt_observability import log_prompt_composition

            log_prompt_composition(
                composer.observability_payload(),
                session_id=getattr(scope.session, "session_id", "") or "",
                principal_id=getattr(scope.session, "principal_id", "") or "",
                composition=getattr(self, "_composition", None),
            )
        except Exception:
            pass

    def _check_answer_provenance(
        self, response_text: str, *, scope: ChatScope | None = None
    ) -> None:
        """Record whether this answer's citations name sources that exist.

        Separate from the audit below on purpose. The audit writes a row and
        so needs a store and something retrieved; this needs neither, and the
        turn it most needs to examine is exactly the one the audit skips —
        an answer carrying ``[C1]`` when the retriever returned nothing has
        invented its evidence outright, and returning early on an empty
        retrieval is how that case stayed invisible.

        Records the finding on the scope. It does not change what is
        returned: what a surface should do about an ungrounded answer differs
        by surface and is not decided here.
        """
        try:
            scope = _active_scope(self, scope)
            from axiom.rag.answer_gate import check_answer_provenance

            scope.last_provenance = check_answer_provenance(response_text, scope.last_retrieved)
        except Exception:
            # Never block an answer on the check itself failing. The finding
            # being absent is visible as ``last_provenance`` staying None,
            # which is not the same as a finding that says GROUNDED.
            pass

    def _log_rag_audit(self, response_text: str, *, scope: ChatScope | None = None) -> None:
        """Record what the retriever surfaced this turn vs what the model cited.

        Runs after the response completes. Every field of the record comes
        from ``scope``, so an audit row is never written against one person's
        session with another person's retrieved chunks. Swallows all errors:
        audit logging must never block the chat path.
        """
        try:
            scope = _active_scope(self, scope)
            retrieved = scope.last_retrieved or []
            if not retrieved:
                return
            store = self._rag_store
            if store is None:
                return
            from axiom.rag.citation import postprocess_citations
            from axiom.rag.retrieval_audit import log_retrieval_audit

            envelope = postprocess_citations(response_text, retrieved)
            latency_ms = int((time.monotonic() - (scope.turn_start or time.monotonic())) * 1000)
            log_retrieval_audit(
                store,
                query_text=scope.turn_query,
                retrieved=retrieved,
                envelope=envelope,
                session_id=getattr(scope.session, "session_id", "") or "",
                principal_id=getattr(scope.session, "principal_id", "") or "",
                latency_ms=latency_ms,
            )
        except Exception:
            pass  # audit is best-effort

    def _rag_context(self, query: str, limit: int = 4, *, scope: ChatScope | None = None) -> str:
        """Retrieve relevant RAG chunks for *query*. Returns formatted string or ''.

        Uses the T0-1 retriever (RRF fusion over vector + text rankings,
        citation keys) and the ``rag_context_block`` formatter so the model
        sees stable [C<n>] markers the citation postprocessor can verify. What
        comes back is recorded on ``scope`` for that conversation's citation
        audit and nowhere else.

        Candidates are drawn from ``scope.retrieval_corpora`` and from nothing
        else. ``None`` is every corpus, which is what a terminal has always
        sent; a surface answering other people names the corpora it may read,
        and the node's own corpus, where this machine's chat transcripts are
        indexed, is not among them.

        No access context is passed, and that is deliberate rather than
        overlooked. The retriever applies its access filter only when one is
        given, and the filter reads a chunk's tier, classification and site
        through lookup callables that no caller in this codebase wires; with
        none of them the tier reads ``public``, the classification
        ``unclassified`` and the site ``None``, which permits every chunk.
        Passing an access context here would read as a control and be none, so
        the corpus filter above is the control. See ``ChatScope``.
        """
        scope = _active_scope(self, scope)
        store = self._get_rag_store()
        if store is None or not query.strip():
            scope.last_retrieved = []
            return ""
        try:
            from axiom.rag.context_block import build_rag_context_block
            from axiom.rag.retriever import retrieve

            # A store that runs hybrid search server-side (the served
            # retrieval store of a joined site node) takes the query as
            # text in one round-trip; embedding here would put retrieval
            # logic on the client. Same rule as ``axiom_rag__retrieve``.
            #
            # Against a local store, embed the query so the retriever
            # fuses vector + keyword rankings (RRF). Text-only ranking has
            # poor recall on semantic queries and silently degrades
            # grounding. Falls back to keyword-only when no embedding
            # provider is configured.
            query_embedding = None
            if not getattr(store, "does_own_hybrid", False):
                try:
                    from axiom.rag.embeddings import embed_texts

                    vecs = embed_texts([query])
                    if vecs:
                        query_embedding = vecs[0]
                except Exception:
                    query_embedding = None
            corpora = scope.retrieval_corpora
            retrieved = retrieve(
                store=store,
                query_text=query,
                query_embedding=query_embedding,
                corpora=list(corpora) if corpora is not None else None,
                limit=limit,
            )
            scope.last_retrieved = retrieved
            block = build_rag_context_block(retrieved)
            if not block:
                return ""
            # Low-confidence hint — surface when the best match is weak.
            if retrieved and retrieved[0].similarity < 0.15:
                block = block + (
                    "\n\n[Low RAG confidence — run `neut rag index` or "
                    '`neut note "..."` to add more context]'
                )
            return block
        except Exception:
            scope.last_retrieved = []
            return ""

    def _schedule_session_index(self, *, scope: ChatScope | None = None) -> None:
        """Fire-and-forget: index ``scope``'s session file after every turn.

        Runs in a daemon thread so it never blocks the response path.

        Only when ``scope.index_transcript`` says this conversation belongs in
        the node's corpus. A terminal says yes, which is why an operator can
        ask next week about what they discussed today. A surface answering
        other people says no: one person's conversation indexed where every
        later conversation retrieves from is a disclosure, and the corpus
        filter on the read side is the other half of the same fix.

        What lands in the corpus is the session file as the surface last saved
        it. The surfaces that persist a conversation call ``SessionStore.save``
        after ``turn`` returns and this runs inside ``turn``, so the turn in
        flight is not in the file yet and arrives with the next one. A
        conversation no surface has saved has no file and is never indexed,
        which is the whole of the protection the shipped serving path had
        before ``index_transcript`` existed.
        """
        scope = _active_scope(self, scope)
        if not scope.index_transcript:
            return
        session_id = getattr(scope.session, "session_id", None)
        if not session_id:
            return

        def _run():
            try:
                from axiom.extensions.builtins.settings.store import SettingsStore

                url = SettingsStore().get("rag.database_url", "")
                if not url:
                    return
                session_path = _REPO_ROOT / "runtime" / "sessions" / f"{session_id}.json"
                if not session_path.exists():
                    return
                from axiom.rag.personal import ingest_session_file
                from axiom.rag.store import CORPUS_INTERNAL
                from axiom.rag.store_factory import create_store

                store = create_store(url)
                if not hasattr(store, "upsert_chunks"):
                    # Served retrieval (a joined node) is read-only from
                    # here: there is no local corpus to index into.
                    return
                store.connect()
                ingest_session_file(session_path, store, corpus=CORPUS_INTERNAL)
                store.close()
            except Exception:
                pass  # best-effort

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def _build_system_prompt(self, *, scope: ChatScope | None = None) -> str:
        """Build ``scope``'s system prompt via T0-3 PromptComposer.

        Each contribution is routed to one of the seven named layers;
        cache boundaries and compaction behavior come for free. The
        composer instance is stashed on ``scope.last_composer`` so the
        turn-completion hook can emit the observability fragment.

        Everything person-specific in the prompt (the workspace brief, the
        session context, the retrieved block, the long-term memory) is read
        from ``scope``, so no part of one person's prompt can be built from
        another's material.

        Three sources are read from the operator's machine instead of from the
        request, and ``scope.include_operator_local_prompts`` decides whether
        they belong in this conversation's prompt: the repository's project
        file, the personal context file under that root's dotted config
        directory, and the operator's prompt library. Each is guarded on its
        own. Nothing here reads the environment or the terminal to decide it.
        """
        from axiom.infra.prompt_composer import PromptComposer

        scope = _active_scope(self, scope)
        composer = PromptComposer()

        # Layer 1 — identity ---------------------------------------------------
        base = _get_prompt_registry().resolve("neut_agent_base")
        composer.add(
            "identity",
            name="neut_agent_base",
            content=base.content,
            source="prompt_registry",
            required=True,
        )

        # Layer 1 — AXI persona (persona.md → identity).  Best-effort:
        # if the persona file is missing or unreadable, the prompt-registry
        # base above keeps chat working.
        try:
            from pathlib import Path as _Path

            from axiom.agents.persona_loader import load_agent_persona

            _persona_dir = _Path(__file__).parent / "agents" / "axi"
            _persona_text = load_agent_persona(_persona_dir)
            if _persona_text:
                composer.add(
                    "identity",
                    name="persona:axi",
                    content=_persona_text,
                    source="agent:axi",
                    required=True,
                )
        except Exception:
            pass

        # Layers 1/2/3 — role/policy/capability specialization contributed by
        # installed extensions (domain-agnostic: the platform composes whatever
        # consumers registered via the prompt_contributor entry-point, and names
        # none of them). Each contributor that wants to know which request it is
        # contributing to is handed this scope's declared field set, built fresh
        # per prompt so no two requests share one. See _prompt_context_for /
        # _discover_prompt_contributions / PROMPT_CONTRIBUTOR_GROUP.
        for frag in _discover_prompt_contributions(_prompt_context_for(scope)):
            composer.add(
                frag["layer"],
                name=frag["name"],
                content=frag["content"],
                source=frag.get("source", "extension"),
                required=bool(frag.get("required", False)),
            )

        # Layer 4 — domain_context (CLAUDE.md, workspace, personal, context files)
        #
        # Three of the sources composed here are read from the machine this
        # process runs on rather than from the request: the project file just
        # below, the personal context file after the workspace brief, and the
        # operator's prompt library at the end of this method. Each is guarded
        # separately by the scope's declaration, so dropping one guard is a
        # visible edit rather than a silent widening. See
        # ``ChatScope.include_operator_local_prompts``.
        claude_md = _REPO_ROOT / "CLAUDE.md"
        if scope.include_operator_local_prompts and claude_md.exists():
            try:
                content = claude_md.read_text(encoding="utf-8")[:8000]
                composer.add(
                    "domain_context",
                    name="claude_md",
                    content=f"--- Project context (CLAUDE.md) ---\n{content}",
                    source="workspace",
                    required=False,
                )
            except OSError:
                pass

        if scope.workspace_context:
            composer.add(
                "domain_context",
                name="active_workspace",
                content=f"--- Active workspace ---\n{scope.workspace_context}",
                source="workspace",
                required=False,
            )

        personal_ctx = _REPO_ROOT / ".claude" / "context.md"
        if scope.include_operator_local_prompts and personal_ctx.exists():
            try:
                content = personal_ctx.read_text(encoding="utf-8")[:2000]
                composer.add(
                    "domain_context",
                    name="personal_context",
                    content=f"--- Personal context ---\n{content}",
                    source="user",
                    required=False,
                )
            except OSError:
                pass

        # Layer 5 — session_memory (per-session ephemeral context)
        ctx_content = scope.session.context.get("file_content", "")
        if ctx_content:
            composer.add(
                "session_memory",
                name="context_file",
                content=f"--- Additional context ---\n{ctx_content[:4000]}",
                source="session",
                required=False,
            )

        ctx_md = scope.session.context.get("context_markdown", "")
        if ctx_md:
            composer.add(
                "session_memory",
                name="terminal_context",
                content=(
                    "--- Context from terminal command ---\n"
                    "The user just viewed the following output and wants to discuss it. "
                    "Reference this content when answering.\n\n" + ctx_md[:6000]
                ),
                source="session",
                required=False,
            )

        # Layer 5 — session_memory, long-term history
        # Pull prior-session episodic fragments for this principal so the
        # agent has continuity across sessions. Composition is optional —
        # an unwired ChatAgent runs stateless, which is fine for CLI dev.
        composition = getattr(self, "_composition", None)
        principal_id = getattr(scope.session, "principal_id", "") or ""
        if composition is not None and principal_id:
            try:
                from axiom.memory.session_summary import inject_session_memory

                inject_session_memory(
                    composer,
                    composition,
                    principal_id=principal_id,
                    max_fragments=10,
                )
            except Exception:
                # Long-term memory is a best-effort enhancement — never
                # block the chat path on a memory-layer failure.
                pass

        # Layer 6 — retrieved (RAG context block from T0-1)
        last_user = ""
        for msg in reversed(scope.session.messages):
            if msg.role == "user":
                last_user = msg.content
                break
        rag_ctx = self._rag_context(last_user, scope=scope)
        if rag_ctx:
            composer.add(
                "retrieved",
                name="rag_context_block",
                content=rag_ctx,
                source="t0-1",
                required=False,
            )

        # The operator's prompt library (~/.axi/prompts/*.md and
        # <project>/.axi/prompts/*.md). Each file's frontmatter declares
        # which layer it targets (default: domain_context). Third of the
        # operator-local sources, guarded on its own.
        if scope.include_operator_local_prompts:
            try:
                from .user_prompts import add_user_prompts_to

                add_user_prompts_to(composer)
            except Exception:
                # Best-effort enhancement — never block the chat path.
                pass

        scope.last_composer = composer
        return composer.render_text()

    def _build_messages(self, *, scope: ChatScope | None = None) -> list[dict[str, Any]]:
        """Build messages list in API format from ``scope``'s session history."""
        messages = []
        for msg in _active_scope(self, scope).session.messages:
            if msg.role == "tool":
                # Reconstruct tool result message
                tc_info = msg.tool_calls[0] if msg.tool_calls else {}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_info.get("tool_call_id", ""),
                        "name": tc_info.get("name", ""),
                        "content": msg.content,
                    }
                )
            elif msg.role == "assistant" and msg.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content,
                        "tool_calls": [
                            {
                                "id": tc.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": tc.get("name", ""),
                                    "arguments": json.dumps(tc.get("input", {})),
                                },
                            }
                            for tc in msg.tool_calls
                        ],
                    }
                )
            else:
                messages.append(
                    {
                        "role": msg.role,
                        "content": msg.content,
                    }
                )

        return self._trim_messages(messages)

    def _trim_messages(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
    ) -> list[dict[str, Any]]:
        """Trim messages to fit context window budget (T0-2).

        Delegates to ``build_window`` which uses real tokenization
        (tiktoken when available), preserves tool-use pairing, and
        injects a summary of dropped history so nothing silently
        vanishes from the model's view.
        """
        from axiom.infra.conversation_window import build_window
        from axiom.infra.token_counter import count_tokens

        system_tokens = count_tokens(system) if system else 0
        return build_window(
            messages,
            max_tokens=CONTEXT_TOKEN_BUDGET,
            system_tokens=system_tokens,
        )
