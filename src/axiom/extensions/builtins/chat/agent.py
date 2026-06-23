# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat agent — native tool-use loop with LLM and approval gate.

Drives the conversation with multi-turn tool calling:
  user input → Gateway.complete_with_tools() →
  if tool_use: execute → feed results back → loop
  else: return text response

The agent is LLM-agnostic — it uses the same Gateway as neut signal.
"""

from __future__ import annotations

import json
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
    ActionCategory,
    ActionStatus,
    create_action,
)
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.session import Session
from axiom.infra.prompt_registry import get_registry as _get_prompt_registry
from axiom.infra.router import QueryRouter

from .providers.base import RenderProvider
from .tools import (
    execute_tool,
    get_all_tools,
    get_tool_definitions,
)
from .usage import TurnUsage, UsageTracker

MAX_TOOL_ROUNDS = 10
CONTEXT_TOKEN_BUDGET = 25000
CHARS_PER_TOKEN = 4  # rough estimate

# Entry-points group through which any installed extension contributes
# domain/role specialization to the chat agent's system prompt. The platform
# stays domain-agnostic: it composes whatever contributors are installed and
# never names a specific consumer. A contributor is a zero-arg callable that
# returns a list of fragment dicts, each:
#   {"layer": "identity"|"policies"|"capabilities"|...,
#    "name": str, "content": str, "source": str, "required": bool}
# (mirrors the ``axiom.portfolio_member`` discovery in axiom.infra.branding.)
PROMPT_CONTRIBUTOR_GROUP = "axiom.chat.prompt_contributor"


def _discover_prompt_contributions() -> list[dict]:
    """Collect system-prompt fragments contributed by installed extensions.

    Never raises: a contributor that is missing, broken, or returns a bad
    shape is logged and skipped so the chat agent always builds a prompt.
    """
    import logging
    from importlib.metadata import entry_points

    log = logging.getLogger(__name__)
    fragments: list[dict] = []
    try:
        eps = entry_points(group=PROMPT_CONTRIBUTOR_GROUP)
    except Exception as exc:  # pragma: no cover - importlib edge cases
        log.warning("prompt_contributor entry-points lookup failed: %s", exc)
        return fragments
    for ep in eps:
        try:
            contribute = ep.load()
            for frag in contribute() or []:
                if frag.get("layer") and frag.get("name") and frag.get("content"):
                    fragments.append(frag)
        except Exception as exc:
            log.warning("prompt contributor %r failed: %s", getattr(ep, "name", ep), exc)
    return fragments


class ChatTurnCancelled(Exception):
    """Raised when a turn is interrupted by the user via threading.Event."""


class ChatAgent:
    """Interactive agent with native tool calling and approval gates."""

    def __init__(
        self,
        gateway: Gateway | None = None,
        bus: EventBus | None = None,
        session: Session | None = None,
        render: RenderProvider | None = None,
    ):
        from .permissions import ToolPermissions

        self.gateway = gateway or Gateway()
        self.bus = bus or EventBus()
        self.gate = ApprovalGate()
        self.session = session or Session()
        self.usage = UsageTracker()
        # Per-tool allow/ask/deny modes set at runtime by the user choosing
        # 'A'/'D' at an approval prompt or via /permissions. Consulted before
        # the approval gate so persisted choices skip the prompt.
        self.permissions = ToolPermissions()
        # Image attachments queued by `/image <path>` for the next turn.
        # Injected into the API messages list (not the session history) so
        # storage stays text-only while the LLM sees the image.
        self._pending_images: list[Any] = []
        self._render = render
        self._router = QueryRouter()
        self._session_mode: str = "auto"  # overridden by --mode flag
        self._session_allowlist: set[str] = set()
        # Interaction mode (TUI shift+tab cycles ask/plan/agent).
        # ``agent`` = full tool-use loop (default).
        # ``ask``   = single completion, no tools at all.
        # ``plan``  = tools listed but system prompt directs model to plan-then-stop.
        self._interaction_mode: str = "agent"
        # Backward-compat: bare callback for tests
        self._renderer_callback: Callable[[Iterator[StreamChunk]], str] | None = None
        # RAG store — lazily initialized if rag.database_url is configured
        self._rag_store: Any | None = None
        self._rag_init_attempted = False
        # Cancellation: set by cancel(), polled between streaming chunks
        self._cancel_event = threading.Event()
        # Chunks retrieved for the in-flight turn (used by citation
        # postprocessor to verify the model's inline [C<n>] markers).
        self._last_retrieved: list = []
        # PromptComposer from the most recent _build_system_prompt — used
        # by the T0-3 observability fragment writer at turn completion.
        self._last_composer: Any | None = None
        # Workspace context set by CLI when model.yaml is detected
        self._workspace_context: str = ""

    def cancel(self) -> None:
        """Signal cancellation of the in-flight turn."""
        self._cancel_event.set()

    def is_cancelled(self) -> bool:
        """Return True if a cancellation has been requested."""
        return self._cancel_event.is_set()

    def reset_cancel(self) -> None:
        """Clear the cancellation flag before starting a new turn."""
        self._cancel_event.clear()

    def allowlisted_tools(self) -> list[str]:
        """Return sorted list of always-approved tools for this session."""
        return sorted(self._session_allowlist)

    def revoke_allowlist(self, tool_name: str | None = None) -> None:
        """Remove a tool from the allowlist, or clear all if tool_name is None."""
        if tool_name is None:
            self._session_allowlist.clear()
        else:
            self._session_allowlist.discard(tool_name)

    def set_renderer(self, callback: Callable[[Iterator[StreamChunk]], str]) -> None:
        """Set a streaming renderer callback (backward-compat)."""
        self._renderer_callback = callback

    def set_render_provider(self, render: RenderProvider) -> None:
        """Set the render provider for rich output."""
        self._render = render

    INTERACTION_MODES = ("ask", "plan", "agent")

    def set_interaction_mode(self, mode: str) -> None:
        """Set the chat interaction mode.

        - ``ask``  : single completion, no tools at all (Q&A only)
        - ``plan`` : tools listed but model directed to plan-then-stop
        - ``agent``: full autonomous tool-use loop (default)
        """
        if mode not in self.INTERACTION_MODES:
            raise ValueError(
                f"unknown interaction mode {mode!r} (expected one of {self.INTERACTION_MODES})"
            )
        self._interaction_mode = mode

    def turn(
        self,
        user_input: str,
        stream: bool = True,
        *,
        raw: bool = False,
    ) -> str:
        """Process one user turn and return the assistant response.

        Thin wrapper that applies any per-prompt provider override
        (spec-chat-model-picker §3) before dispatching to the impl. The
        override is restored when the turn finishes, success or failure.
        """
        from axiom.chat.picker import apply_per_prompt_override

        self.reset_cancel()
        with apply_per_prompt_override(user_input, self.gateway) as picker:
            if picker.error_message:
                return picker.error_message
            return self._turn_impl(picker.stripped_prompt, stream=stream, raw=raw)

    def _turn_impl(
        self,
        user_input: str,
        stream: bool = True,
        *,
        raw: bool = False,
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
        """

        # Classify before adding to session so context window = prior turns only
        routing = self._router.classify(
            user_input,
            session_mode=self._session_mode,
            context=self.session.messages[-10:],
        )
        routing_tier = routing.tier.value

        # Audit log — record routing decision (no plaintext).
        # Fires in BOTH normal and raw modes; the audit is metadata, not
        # augmentation.
        try:
            from axiom.infra.routing_audit import hash_query, log_routing_decision

            log_routing_decision(
                session_id=getattr(self.session, "id", ""),
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
            return self._raw_turn(
                user_input, routing_tier=routing_tier, routing_decision=routing
            )

        self.session.add_message("user", user_input)

        # Stash for T0-1 retrieval audit log (written at turn completion).
        self._turn_query = user_input
        self._turn_start = time.monotonic()

        system = self._build_system_prompt()
        messages = self._build_messages()
        tools = get_tool_definitions()

        # Inject queued image attachments into the just-added user message.
        # Session storage stays text-only; only the API call sees the bytes.
        if self._pending_images and messages and messages[-1].get("role") == "user":
            from .attachments import build_user_message, detect_provider_kind

            provider = self.gateway.active_provider
            kind = detect_provider_kind(provider) if provider else "openai"
            last_text = messages[-1].get("content", "")
            if isinstance(last_text, str):
                messages[-1] = build_user_message(last_text, self._pending_images, kind)
            self._pending_images = []

        # Apply interaction mode (ask/plan/agent) — see set_interaction_mode.
        if self._interaction_mode == "ask":
            tools = None  # no tool surface; pure Q&A turn
        elif self._interaction_mode == "plan":
            # Build comma-joined list of tool names for the system prompt,
            # then strip tools from the API call so no tool_use blocks fire.
            all_tool_names = ", ".join(
                t["function"]["name"] for t in (tools or [])
            )
            system = (
                system
                + "\n\n--- PLAN MODE ---\n"
                "You are in PLAN mode. Produce a numbered plan describing what "
                "you would do, which tools you would call (by name) and in what "
                "order, and what the user should review before approval. "
                "DO NOT call any tools in this turn — output the plan as text only. "
                f"Available tools you can name in the plan: {all_tool_names}\n"
                "End with a single line: `Reply 'go' (or switch to agent mode) to execute.`"
            )
            tools = None  # hard-strip: prevent any tool_use blocks in plan mode

        response = None
        for _round in range(MAX_TOOL_ROUNDS):
            # First round streams to show immediate output.
            # Subsequent rounds (after tool results) use non-streaming to
            # prevent the model from re-rendering text it already showed.
            use_stream = stream and _round == 0

            # On the final allowed round, withhold the tool surface so the
            # model is forced to synthesize a text answer from everything it
            # has already retrieved — rather than requesting yet another tool
            # call, exhausting the loop, and returning the apology fallback as
            # if it were a real answer.
            is_final_round = _round == MAX_TOOL_ROUNDS - 1
            round_tools = None if is_final_round else tools

            if (
                use_stream
                and not is_final_round
                and self.gateway.available
                and (self._render or self._renderer_callback)
            ):
                response = self._streaming_turn(messages, system, round_tools, routing_tier)
            elif self.gateway.available:
                response = self._non_streaming_turn(
                    messages, system, round_tools, routing_tier, routing_decision=routing
                )
            else:
                response = self._legacy_turn(user_input, system)

            # Record usage for this API call
            self._record_usage(response)

            # If no tool calls, we're done
            if not response.tool_use:
                # If this was a non-streamed round, render the final text now
                if not use_stream and response.text and self._render:
                    self._render.render_message("assistant", response.text)
                self.session.add_message("assistant", response.text)
                self._schedule_session_index()
                self._log_rag_audit(response.text)
                self._log_prompt_observability()
                return response.text

            # Process tool calls
            tool_results = self._process_tool_calls(response)

            # Build the assistant message with tool calls
            assistant_tool_calls = [
                {"name": t.name, "id": t.tool_id, "input": t.input} for t in response.tool_use
            ]

            # Store in session
            self.session.add_message(
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
                self.session.add_message(
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
        fallback = (
            response.text if response else None
        ) or "I've reached the maximum number of tool-use rounds."
        self.session.add_message("assistant", fallback)
        self._schedule_session_index()
        return fallback

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
        - ``self.session`` is not mutated (no add_message before, during,
          or after).

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
    ) -> CompletionResponse:
        """Execute a streaming turn and collect the full response."""
        chunks = self.gateway.stream_with_tools(
            messages=messages,
            system=system,
            tools=tools,
            routing_tier=routing_tier,
        )

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

        if render_fn:
            # Create a tee iterator — render while collecting
            collected_chunks = []

            def tee_chunks():
                for c in chunks:
                    collected_chunks.append(c)
                    yield c
                    # Poll between chunks; raise after yielding so the
                    # renderer sees at most one chunk before cancellation.
                    if self._cancel_event.is_set():
                        raise ChatTurnCancelled("Turn cancelled by user")

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
                if self._cancel_event.is_set():
                    raise ChatTurnCancelled("Turn cancelled by user")
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

    def _legacy_turn(self, user_input: str, system: str) -> CompletionResponse:
        """Fallback: text-only prompt without native tool-use.

        Used when the gateway has no providers (stub mode) or provider
        doesn't support tool-use.
        """
        # Build a flat text prompt with tool descriptions
        all_tools = get_all_tools()
        tools_desc = "\n".join(
            f"- {t.name}: {t.description} ({'read' if t.category == ActionCategory.READ else 'write'})"
            for t in all_tools.values()
        )

        recent = self.session.messages[-6:]
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
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Execute tool calls through the approval gate.

        Returns list of (tool_id, tool_name, result_dict).
        """
        results = []

        for tool_block in response.tool_use:
            if self._cancel_event.is_set():
                results.append(
                    (tool_block.tool_id, tool_block.name, {"cancelled": True})
                )
                continue

            action = create_action(tool_block.name, tool_block.input)
            self.gate.submit(action)

            # Per-tool persisted permission overrides the prompt entirely.
            perm = self.permissions.get(tool_block.name)
            if perm == "deny":
                self.gate.reject(action.action_id, "Denied by tool permission")
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
                self.gate.approve(action.action_id)

            # If still pending (i.e. write action with no override), prompt.
            if action.status == ActionStatus.PENDING:
                if tool_block.name in self._session_allowlist:
                    # Previously allowlisted — auto-approve without prompting
                    choice = "a"
                elif self._render:
                    choice = self._render.render_approval_prompt(action)
                else:
                    from .renderer import render_approval_prompt

                    choice = render_approval_prompt(action)

                if choice == "A":
                    # Persist for the rest of the session.
                    self.permissions.set(tool_block.name, "allow")
                    self.gate.approve(action.action_id)
                elif choice == "a":
                    self.gate.approve(action.action_id)
                elif choice == "D":
                    # Persist deny for the rest of the session.
                    self.permissions.set(tool_block.name, "deny")
                    self.gate.reject(action.action_id, "Denied (Always)")
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
                    self.gate.reject(action.action_id, "User rejected")
                    if self._render:
                        self._render.render_action_result(action)
                    else:
                        from .renderer import render_action_result

                        render_action_result(action)
                    results.append(
                        (
                            tool_block.tool_id,
                            tool_block.name,
                            {"error": "Rejected by user"},
                        )
                    )
                    continue

            # Execute approved action with timing
            t0 = time.monotonic()
            if self._render:
                self._render.render_tool_start(tool_block.name, tool_block.input)
            from axiom.infra.hooks import ApprovalRequired, HookDenied
            from axiom.infra.tool_gateway import dispatch_tool as _dispatch

            from .tool_errors import safe_run

            principal_id = getattr(self.session, "principal_id", "") or ""

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
                f"{tool_block.name.replace('_', '.')}.complete",
                {"action_id": action.action_id, "result": result},
                source="chat",
            )

            results.append((tool_block.tool_id, tool_block.name, result))

        return results

    def _get_rag_store(self) -> Any | None:
        """Lazily initialize the RAG store from settings."""
        if self._rag_init_attempted:
            return self._rag_store
        self._rag_init_attempted = True
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            url = SettingsStore().get("rag.database_url", "")
            if not url:
                return None
            from axiom.rag.store import RAGStore

            store = RAGStore(url)
            store.connect()
            self._rag_store = store
        except Exception:
            pass  # RAG not available — chat still works
        return self._rag_store

    def _log_prompt_observability(self) -> None:
        """Emit the T0-3 prompt-composition observability record."""
        try:
            composer = getattr(self, "_last_composer", None)
            if composer is None:
                return
            from axiom.infra.prompt_observability import log_prompt_composition

            log_prompt_composition(
                composer.observability_payload(),
                session_id=getattr(self.session, "session_id", "") or "",
                principal_id=getattr(self.session, "principal_id", "") or "",
                composition=getattr(self, "_composition", None),
            )
        except Exception:
            pass

    def _log_rag_audit(self, response_text: str) -> None:
        """Record what the retriever surfaced this turn vs what the model cited.

        Runs after the response completes. Swallows all errors — audit
        logging must never block the chat path.
        """
        try:
            retrieved = getattr(self, "_last_retrieved", None) or []
            if not retrieved:
                return
            store = self._rag_store
            if store is None:
                return
            from axiom.rag.citation import postprocess_citations
            from axiom.rag.retrieval_audit import log_retrieval_audit

            envelope = postprocess_citations(response_text, retrieved)
            latency_ms = int(
                (time.monotonic() - getattr(self, "_turn_start", time.monotonic())) * 1000
            )
            log_retrieval_audit(
                store,
                query_text=getattr(self, "_turn_query", ""),
                retrieved=retrieved,
                envelope=envelope,
                session_id=getattr(self.session, "session_id", "") or "",
                principal_id=getattr(self.session, "principal_id", "") or "",
                latency_ms=latency_ms,
            )
        except Exception:
            pass  # audit is best-effort

    def _rag_context(self, query: str, limit: int = 4) -> str:
        """Retrieve relevant RAG chunks for *query*. Returns formatted string or ''.

        Uses the T0-1 retriever (RRF fusion over vector + text rankings,
        access-filter-aware, citation keys) and the ``rag_context_block``
        formatter so the model sees stable [C<n>] markers the citation
        postprocessor can verify.
        """
        store = self._get_rag_store()
        if store is None or not query.strip():
            self._last_retrieved = []
            return ""
        try:
            from axiom.rag.context_block import build_rag_context_block
            from axiom.rag.retriever import retrieve

            # Embed the query so the retriever fuses vector + keyword
            # rankings (RRF). Text-only ranking has poor recall on semantic
            # queries and silently degrades grounding. Falls back to
            # keyword-only when no embedding provider is configured.
            query_embedding = None
            try:
                from axiom.rag.embeddings import embed_texts

                vecs = embed_texts([query])
                if vecs:
                    query_embedding = vecs[0]
            except Exception:
                query_embedding = None
            retrieved = retrieve(
                store=store,
                query_text=query,
                query_embedding=query_embedding,
                limit=limit,
            )
            self._last_retrieved = retrieved
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
            self._last_retrieved = []
            return ""

    def _schedule_session_index(self) -> None:
        """Fire-and-forget: index the current session file after every turn.

        Runs in a daemon thread so it never blocks the response path.
        The session file is written by the session manager before this
        method is called, so the latest turn is always included.
        """
        session_id = getattr(self.session, "session_id", None)
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
                from axiom.rag.store import CORPUS_INTERNAL, RAGStore

                store = RAGStore(url)
                store.connect()
                ingest_session_file(session_path, store, corpus=CORPUS_INTERNAL)
                store.close()
            except Exception:
                pass  # best-effort

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def _build_system_prompt(self) -> str:
        """Build the system prompt via T0-3 PromptComposer.

        Each contribution is routed to one of the seven named layers;
        cache boundaries and compaction behavior come for free. The
        composer instance is stashed on ``self._last_composer`` so the
        turn-completion hook can emit the observability fragment.
        """
        from axiom.infra.prompt_composer import PromptComposer

        composer = PromptComposer()

        # Layer 1 — identity ---------------------------------------------------
        base = _get_prompt_registry().resolve("neut_agent_base")
        composer.add(
            "identity", name="neut_agent_base",
            content=base.content, source="prompt_registry",
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
                    "identity", name="persona:axi",
                    content=_persona_text, source="agent:axi", required=True,
                )
        except Exception:
            pass

        # Layers 1/2/3 — role/policy/capability specialization contributed by
        # installed extensions (domain-agnostic: the platform composes whatever
        # consumers registered via the prompt_contributor entry-point, and names
        # none of them). See _discover_prompt_contributions / PROMPT_CONTRIBUTOR_GROUP.
        for frag in _discover_prompt_contributions():
            composer.add(
                frag["layer"], name=frag["name"], content=frag["content"],
                source=frag.get("source", "extension"),
                required=bool(frag.get("required", False)),
            )

        # Layer 4 — domain_context (CLAUDE.md, workspace, personal, context files)
        claude_md = _REPO_ROOT / "CLAUDE.md"
        if claude_md.exists():
            try:
                content = claude_md.read_text(encoding="utf-8")[:8000]
                composer.add(
                    "domain_context", name="claude_md",
                    content=f"--- Project context (CLAUDE.md) ---\n{content}",
                    source="workspace", required=False,
                )
            except OSError:
                pass

        if self._workspace_context:
            composer.add(
                "domain_context", name="active_workspace",
                content=f"--- Active workspace ---\n{self._workspace_context}",
                source="workspace", required=False,
            )

        personal_ctx = _REPO_ROOT / ".claude" / "context.md"
        if personal_ctx.exists():
            try:
                content = personal_ctx.read_text(encoding="utf-8")[:2000]
                composer.add(
                    "domain_context", name="personal_context",
                    content=f"--- Personal context ---\n{content}",
                    source="user", required=False,
                )
            except OSError:
                pass

        # Layer 5 — session_memory (per-session ephemeral context)
        ctx_content = self.session.context.get("file_content", "")
        if ctx_content:
            composer.add(
                "session_memory", name="context_file",
                content=f"--- Additional context ---\n{ctx_content[:4000]}",
                source="session", required=False,
            )

        ctx_md = self.session.context.get("context_markdown", "")
        if ctx_md:
            composer.add(
                "session_memory", name="terminal_context",
                content=(
                    "--- Context from terminal command ---\n"
                    "The user just viewed the following output and wants to discuss it. "
                    "Reference this content when answering.\n\n" + ctx_md[:6000]
                ),
                source="session", required=False,
            )

        # Layer 5 — session_memory, long-term history
        # Pull prior-session episodic fragments for this principal so the
        # agent has continuity across sessions. Composition is optional —
        # an unwired ChatAgent runs stateless, which is fine for CLI dev.
        composition = getattr(self, "_composition", None)
        principal_id = getattr(self.session, "principal_id", "") or ""
        if composition is not None and principal_id:
            try:
                from axiom.memory.session_summary import inject_session_memory

                inject_session_memory(
                    composer, composition,
                    principal_id=principal_id,
                    max_fragments=10,
                )
            except Exception:
                # Long-term memory is a best-effort enhancement — never
                # block the chat path on a memory-layer failure.
                pass

        # Layer 6 — retrieved (RAG context block from T0-1)
        last_user = ""
        for msg in reversed(self.session.messages):
            if msg.role == "user":
                last_user = msg.content
                break
        rag_ctx = self._rag_context(last_user)
        if rag_ctx:
            composer.add(
                "retrieved", name="rag_context_block",
                content=rag_ctx, source="t0-1", required=False,
            )

        # User-authored prompt fragments (~/.axi/prompts/*.md and
        # <project>/.axi/prompts/*.md). Each file's frontmatter declares
        # which layer it targets (default: domain_context).
        try:
            from .user_prompts import add_user_prompts_to

            add_user_prompts_to(composer)
        except Exception:
            # Best-effort enhancement — never block the chat path.
            pass

        self._last_composer = composer
        return composer.render_text()

    def _build_messages(self) -> list[dict[str, Any]]:
        """Build messages list in API format from session history."""
        messages = []
        for msg in self.session.messages:
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
