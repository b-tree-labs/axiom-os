# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Silent render provider: the chat engine with no terminal attached.

A serving worker, a scheduled run or any other surface without a console
implements the same render contract as the terminal front ends, and paints
nothing. Every ``render_*`` method here is a no-op: this provider writes
nothing to stdout and nothing to stderr, so terminal chrome never lands in a
process whose output is a pipe or a log.

Two methods do more than nothing, because their contract is not painting:

``stream_text`` still drains the chunk iterator and returns the accumulated
text. The agent tees the model stream through the renderer and rebuilds the
turn, tool calls included, from the chunks the renderer pulled, so a provider
that stops early would silently drop tool calls.

``render_approval_prompt`` never prompts. It returns the decision of the
approval policy this provider was built with, which refuses anything a site
has not allowlisted by name. See ``approval_policy``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from ..approval_policy import ApprovalPolicy, NonInteractiveApprovalPolicy
from .base import RenderProvider

if TYPE_CHECKING:
    from axiom.infra.gateway import StreamChunk
    from axiom.infra.orchestrator.actions import Action


class NullRenderProvider(RenderProvider):
    """A render provider that emits nothing and decides approvals by policy."""

    def __init__(self, approval_policy: ApprovalPolicy | None = None) -> None:
        self._approval_policy = approval_policy or NonInteractiveApprovalPolicy()

    @property
    def approval_policy(self) -> ApprovalPolicy:
        """The policy consulted instead of prompting an operator."""
        return self._approval_policy

    def stream_text(self, chunks: Iterator[StreamChunk]) -> str:
        """Drain the stream without painting it; return the accumulated text."""
        accumulated = ""
        for chunk in chunks:
            if chunk.type == "text":
                accumulated += chunk.text
        return accumulated

    def render_welcome(
        self,
        gateway: Any = None,
        show_banner: bool = False,
        workspace_context: str = "",
    ) -> None:
        """No banner: this surface has no console to greet."""

    def render_tool_start(self, name: str, params: dict[str, Any]) -> None:
        """No progress line."""

    def render_tool_result(self, name: str, result: dict[str, Any], elapsed: float) -> None:
        """No result line; the caller reads the result from the turn."""

    def render_approval_prompt(self, action: Action) -> str:
        """Answer from the declared policy; never prompt, never read stdin."""
        return self._approval_policy.decide(action)

    def render_action_result(self, action: Action) -> None:
        """No outcome line; the action itself carries status and error."""

    def render_status(
        self,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
    ) -> None:
        """No status line; usage is available from the tracker."""

    def render_thinking(self, text: str, collapsed: bool = True) -> None:
        """No thinking block."""

    def render_message(self, role: str, content: str) -> None:
        """No message echo; the caller already holds the message."""

    def render_session_list(self, sessions: list[dict[str, Any]]) -> None:
        """No session table."""


__all__ = ["NullRenderProvider"]
