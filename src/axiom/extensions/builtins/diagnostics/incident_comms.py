# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Human<>Agent incident conversation — the HITL choreography (ADR-074, #540).

TRIAGE posts an incident brief that answers a reviewer's obvious questions up
front (what broke, how often, root cause, the proposed reversible fix), offers
an Approve/Deny affordance, answers follow-up questions in-thread, and — only
on human approval — invokes the remediator (TIDY) and reports the outcome.

Vendor-neutral: it speaks to an ``InteractiveChannel`` (in-memory now, Slack /
Teams later). The remediation itself runs through the agent pipeline's gates
(per ADR-074); here ``remediator`` is that callable injected by the caller, so
this module stays transport- and engine-agnostic and fully testable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..notifications.channels.interactive import (
    ApprovalOption,
    ApprovalOutcome,
    ApprovalRequest,
    ChannelMessage,
    InteractiveChannel,
)
from .safety import Finding

# responder(question, context) -> answer text  (TRIAGE/TIDY answering a human)
Responder = Callable[[str, dict], str]
# remediator(plan) -> outcome dict  (TIDY applying the staged, gated fix)
Remediator = Callable[[dict], dict]

_GiB = 1024**3
_MiB = 1024**2


def _build_brief(finding: Finding) -> str:
    m = finding.metadata
    lines = [f":rotating_light: *{finding.title}*", finding.detail]
    if m.get("restarts") is not None:
        lines.append(f"• restarts: *{m['restarts']}*")
    plan = m.get("remediation_plan")
    if plan:
        old = plan.get("old_limit_bytes")
        new = plan.get("new_limit_bytes")
        if old and new:
            lines.append(
                f"• proposed fix: raise memory limit "
                f"{old / _GiB:.2f} GiB → *{new // _GiB} GiB* "
                f"({'reversible' if plan.get('reversible') else 'NOT reversible'})"
            )
    lines.append(f"• remediation: {finding.remediation}")
    return "\n".join(lines)


class IncidentConversation:
    """One incident's conversation + approval lifecycle on a channel."""

    def __init__(
        self,
        channel: InteractiveChannel,
        *,
        responder: Responder | None = None,
        remediator: Remediator,
        agent: str = "TRIAGE",
        agent_icon: str | None = None,
    ) -> None:
        self._ch = channel
        # The agent persona speaking in the channel. Maps to per-message
        # attribution (Slack `username` + `icon_url`) so the channel shows
        # *which* Axiom agent posted, with a face — not the neutral connector
        # presence. These are AgentCard.name / AgentCard.icon_url, not a
        # connector identity.
        self._agent = agent
        self._agent_icon = agent_icon
        # Default to the standard incident-interview skill so any incident gets
        # good Q&A without a bespoke responder (callers can still inject one,
        # e.g. bound to live host_exec state).
        if responder is None:
            from .incident_interview import make_responder

            responder = make_responder()
        self._responder = responder
        self._remediator = remediator
        self.status = "new"  # new → awaiting_approval → applied | denied
        self.thread_id: str | None = None
        self._finding: Finding | None = None

    def open(self, finding: Finding) -> str:
        self._finding = finding
        self.thread_id = self._ch.post(_build_brief(finding), author=self._agent, icon_url=self._agent_icon)
        self._ch.request_approval(
            ApprovalRequest(
                prompt="Approve the staged remediation? Ask questions in-thread first if needed.",
                options=(
                    ApprovalOption("approve", "Approve & apply", style="primary"),
                    ApprovalOption("deny", "Deny", style="danger"),
                ),
                context={"finding": finding.title},
                thread_id=self.thread_id,
            )
        )
        # Proactively invite the canonical reviewer questions (the agent
        # "knowing the right questions"), so the human isn't guessing.
        from .incident_interview import suggested_questions

        qs = suggested_questions(finding.metadata)
        self._ch.post("You can ask me:\n" + "\n".join(f"• {q}" for q in qs), thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon)

        self._ch.on_message(self._handle_message)
        self._ch.on_action(self._handle_action)
        self.status = "awaiting_approval"
        return self.thread_id

    # --- inbound handlers ---------------------------------------------------
    def _context(self) -> dict[str, Any]:
        return dict(self._finding.metadata) if self._finding else {}

    def _handle_message(self, msg: ChannelMessage) -> None:
        # Conversation is always open: the human can talk to the agent back and
        # forth at any point — before, during, or after the approval. Approval
        # is one affordance within the conversation, never a gate on it. We only
        # skip the agent's own messages (to avoid a feedback loop).
        if msg.is_agent:
            return
        answer = self._responder(msg.text, self._context())
        if answer:
            self._ch.post(answer, thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon)

    def _handle_action(self, outcome: ApprovalOutcome) -> None:
        if self.status != "awaiting_approval":
            return  # ignore double-clicks / late clicks after resolution
        if outcome.action_id == "approve":
            self.status = "applying"
            self._ch.post(
                f"Approved by {outcome.actor} — applying the staged fix…",
                thread_id=self.thread_id,
                author=self._agent,
                icon_url=self._agent_icon,
            )
            plan = self._context().get("remediation_plan", {})
            try:
                result = self._remediator(plan)
            except Exception as exc:  # noqa: BLE001 — surface, don't crash the channel
                self.status = "awaiting_approval"  # allow retry
                self._ch.post(f":x: Remediation failed: {exc}", thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon)
                return
            self.status = "applied"
            self._ch.post(self._format_outcome(result), thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon)
        elif outcome.action_id == "deny":
            self.status = "denied"
            self._ch.post(
                f"Denied by {outcome.actor} — no change applied. Incident left open for manual handling.",
                thread_id=self.thread_id,
                author=self._agent,
                icon_url=self._agent_icon,
            )

    @staticmethod
    def _format_outcome(result: dict) -> str:
        if not result:
            return ":white_check_mark: Remediation applied."
        ok = result.get("ok", True)
        verified = result.get("verified")
        bits = [":white_check_mark:" if ok else ":x:", "Remediation applied."]
        if "new_limit_gib" in result:
            bits.append(f"New limit: {result['new_limit_gib']} GiB.")
        if verified is not None:
            bits.append("Recovery verified." if verified else "Recovery NOT yet verified.")
        return " ".join(bits)


__all__ = ["IncidentConversation"]
