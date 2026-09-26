# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SSE render provider — the chat agent driving a browser over Server-Sent Events.

Implements the same :class:`RenderProvider` contract as the terminal front ends,
but instead of painting it emits typed frames through an ``emit`` callback the
SSE endpoint drains onto the wire. Those frames ARE the appkit ChatFrame protocol
— ``chunk`` / ``tool_call`` / ``tool_result`` / ``action_result`` /
``clarification`` — so this is the single place that maps what ``ChatAgent``
produces onto what the web UI consumes, mirroring appkit's ``framesFromSSEData``
on the client. (``conversation_id`` and ``error`` are framed by the endpoint, not
here — they are transport facts, not agent events.)

Approvals: a browser turn cannot block mid-stream for an operator, so approvals
are answered by the provider's :class:`ApprovalPolicy` — as the null provider
does, refusing anything a site has not allowlisted — and the pending action is
surfaced as a ``clarification`` frame so the UI can tell the user it was held.
The agent records the refused action on the durable ApprovalGate; approving it is
a separate, out-of-band step (the HITL record), never a synchronous stream wait.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any

from ..approval_policy import ApprovalPolicy, NonInteractiveApprovalPolicy
from .base import RenderProvider, citation_entries

if TYPE_CHECKING:
    from axiom.infra.gateway import StreamChunk
    from axiom.infra.orchestrator.actions import Action

Frame = dict[str, Any]


def _action_frame(action: Action) -> dict:
    """A JSON-safe summary of an Action for a ui frame — never the raw object."""
    status = getattr(action, "status", None)
    return {
        "action_id": getattr(action, "action_id", ""),
        "name": getattr(action, "name", ""),
        "status": getattr(status, "value", str(status)) if status is not None else None,
        "error": getattr(action, "error", None),
    }


class SseRenderProvider(RenderProvider):
    """Emits appkit ChatFrames via ``emit`` instead of painting a terminal."""

    def __init__(
        self, emit: Callable[[Frame], None], approval_policy: ApprovalPolicy | None = None
    ) -> None:
        self._emit = emit
        self._approval_policy = approval_policy or NonInteractiveApprovalPolicy()

    @property
    def approval_policy(self) -> ApprovalPolicy:
        return self._approval_policy

    def stream_text(self, chunks: Iterator[StreamChunk]) -> str:
        """Emit each text delta as a ``chunk`` frame; return accumulated text.

        Drains the whole iterator (the agent rebuilds the turn — tool calls
        included — from the chunks the renderer pulled, so stopping early would
        silently drop tool calls, exactly as the null provider warns)."""
        acc = ""
        for chunk in chunks:
            if getattr(chunk, "type", None) == "text" and getattr(chunk, "text", ""):
                acc += chunk.text
                self._emit({"chunk": chunk.text})
        return acc

    def render_tool_start(self, name: str, params: dict[str, Any]) -> None:
        self._emit({"tool_call": name})

    def render_tool_result(self, name: str, result: dict[str, Any], elapsed: float) -> None:
        """Report the OUTCOME of a tool call, not what it returned.

        The browser previously got ``{"tool_result": name}`` and could not tell
        a success from a failure. This carries what the terminal shows — the
        name, ok or failed, the elapsed time, and the error text when there is
        one.

        The result dict itself stays on the node. The terminal does not print
        it either, and a served surface answers people who are not the
        operator, so shipping a raw tool result to a browser would be a
        disclosure that parity does not require.
        """
        error = result.get("error") if isinstance(result, dict) else None
        outcome: dict[str, Any] = {
            "name": name,
            "ok": error is None,
            "elapsed": elapsed,
        }
        if error is not None:
            outcome["error"] = str(error)
        # ``tool_result`` keeps its original shape — the bare NAME — so a client
        # pinned to an older build keeps working when a node is upgraded ahead
        # of it, which is the ordinary case where a site pins its own web
        # version. The outcome rides alongside in its own key, so this frame is
        # purely additive like every other change in this series.
        self._emit({"tool_result": name, "tool_outcome": outcome})

    def render_approval_prompt(self, action: Action) -> str:
        # Cannot block a browser turn for an operator: answer by policy and
        # surface the pending action so the UI can say it was held. The durable
        # ApprovalGate is where a human approves it, out of band.
        self._emit({"clarification": {"kind": "approval", "action": _action_frame(action)}})
        return self._approval_policy.decide(action)

    def render_action_result(self, action: Action) -> None:
        self._emit({"action_result": _action_frame(action)})

    def render_message(self, role: str, content: str) -> None:
        # Non-stream fallback: some paths render the whole assistant message
        # rather than streaming it. Put it on the wire as one chunk.
        if role == "assistant" and content:
            self._emit({"chunk": content})

    # Terminal-only chrome — nothing belongs on the wire.
    def render_welcome(self, gateway: Any = None, show_banner: bool = False, workspace_context: str = "") -> None: ...
    def render_status(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
        tier: str | None = None,
    ) -> None:
        """Put the turn's model, tier, tokens and cost on the wire."""
        if not model and not tokens_in and not tokens_out:
            return
        self._emit({
            "status": {
                "model": model,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost": cost,
                "tier": tier,
            }
        })
    def render_thinking(self, text: str, collapsed: bool = True) -> None:
        """Put the turn's reasoning on the wire, with the collapse preference.

        The FULL text travels, not the three-line preview the terminal prints.
        A console truncates because it cannot do better; a browser can build a
        real disclosure control, and it can only do that if it has the whole
        thing. ``collapsed`` carries the intent so the surface knows to start
        closed.

        Reasoning that is empty or only whitespace emits nothing — an empty
        disclosure widget is worse than no widget.
        """
        if not text or not text.strip():
            return
        self._emit({"thinking": {"text": text, "collapsed": collapsed}})
    def render_session_list(self, sessions: list[dict[str, Any]]) -> None: ...

    def render_citations(self, chunks: Sequence[Any]) -> None:
        """Put the answer's sources on the wire, deduped to one per document."""
        entries = citation_entries(chunks)
        if not entries:
            return
        self._emit({"citations": entries})


__all__ = ["SseRenderProvider"]
