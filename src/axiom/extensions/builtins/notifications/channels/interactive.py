# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Interactive (bidirectional) channel adapters — the inbound half HERALD's
outbound `ChannelAdapter` family was missing (ADR-074, agents-in-channels).

A vendor-neutral protocol so the incident/HITL workflow speaks to one
interface and Slack (Socket Mode) / Teams / an in-memory test channel are
each just a provider. Outbound posting + inbound messages + interactive
approvals (buttons), with threading. Registered as an AEOS
`channel_adapter` with `direction = bidirectional`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ChannelMessage:
    """A message observed in the channel (human or agent)."""

    text: str
    author: str
    thread_id: str | None = None
    is_agent: bool = False


@dataclass(frozen=True)
class ApprovalOption:
    action_id: str          # stable id the action handler matches on
    label: str              # human-facing button label
    style: str = "default"  # default | primary | danger


@dataclass(frozen=True)
class ApprovalRequest:
    prompt: str
    options: tuple[ApprovalOption, ...]
    context: dict = field(default_factory=dict)
    thread_id: str | None = None


@dataclass(frozen=True)
class ApprovalOutcome:
    action_id: str
    actor: str
    thread_id: str | None = None


MessageHandler = Callable[[ChannelMessage], None]
ActionHandler = Callable[[ApprovalOutcome], None]


@runtime_checkable
class InteractiveChannel(Protocol):
    """Bidirectional channel. Real providers add `run()` to start the inbound
    loop (Socket Mode for Slack); the in-memory one injects events directly."""

    def post(
        self,
        text: str,
        *,
        thread_id: str | None = None,
        author: str = "agent",
        icon_url: str | None = None,
    ) -> str: ...
    def request_approval(self, request: ApprovalRequest) -> str: ...
    def on_message(self, handler: MessageHandler) -> None: ...
    def on_action(self, handler: ActionHandler) -> None: ...


@dataclass
class PostedMessage:
    text: str
    thread_id: str | None
    author: str
    kind: str = "message"  # message | approval
    icon_url: str | None = None  # per-agent avatar (A2A AgentCard.icon_url)


class InMemoryInteractiveChannel:
    """Vendor-free channel for tests, dry-runs, and the outage simulation.

    Captures everything posted and lets a caller inject human messages and
    approval clicks, which fire the registered handlers — so the entire
    incident/HITL choreography runs with zero credentials. Slack/Teams
    providers implement the same protocol; the workflow never changes.
    """

    def __init__(self) -> None:
        self.posts: list[PostedMessage] = []
        self._msg_handlers: list[MessageHandler] = []
        self._action_handlers: list[ActionHandler] = []
        self._seq = 0

    def _next_thread(self) -> str:
        self._seq += 1
        return f"thread-{self._seq}"

    def post(
        self,
        text: str,
        *,
        thread_id: str | None = None,
        author: str = "agent",
        icon_url: str | None = None,
    ) -> str:
        tid = thread_id or self._next_thread()
        self.posts.append(PostedMessage(text=text, thread_id=tid, author=author, icon_url=icon_url))
        return tid

    def request_approval(self, request: ApprovalRequest) -> str:
        tid = request.thread_id or self._next_thread()
        labels = " | ".join(f"[{o.label}]" for o in request.options)
        self.posts.append(
            PostedMessage(text=f"{request.prompt}\n{labels}", thread_id=tid, author="agent", kind="approval")
        )
        return tid

    def on_message(self, handler: MessageHandler) -> None:
        self._msg_handlers.append(handler)

    def on_action(self, handler: ActionHandler) -> None:
        self._action_handlers.append(handler)

    # --- test/sim injection -------------------------------------------------
    def inject_message(self, text: str, author: str = "human", thread_id: str | None = None) -> None:
        msg = ChannelMessage(text=text, author=author, thread_id=thread_id, is_agent=False)
        for h in list(self._msg_handlers):
            h(msg)

    def inject_action(self, action_id: str, actor: str = "human", thread_id: str | None = None) -> None:
        outcome = ApprovalOutcome(action_id=action_id, actor=actor, thread_id=thread_id)
        for h in list(self._action_handlers):
            h(outcome)

    # convenience for assertions
    def texts(self) -> list[str]:
        return [p.text for p in self.posts]


__all__ = [
    "ChannelMessage",
    "ApprovalOption",
    "ApprovalRequest",
    "ApprovalOutcome",
    "InteractiveChannel",
    "InMemoryInteractiveChannel",
    "PostedMessage",
]
