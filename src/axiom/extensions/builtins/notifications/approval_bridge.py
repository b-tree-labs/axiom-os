# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ringing the doorbell for a held action, and taking the answer back.

``ApprovalGate`` holds a write action until a human answers. Durably, now. But a
durable queue nobody is told about is a queue nobody looks at, and the person
who needs to answer is not sitting at the terminal that asked.

This is the seam between the two halves, and the shape of it is the whole
point:

    the gate is the record; the channel is the doorbell.

``InteractiveChannel`` already does the hard part — post a prompt with buttons
to Slack, Teams, SMS or an in-memory fake, and fire a handler when somebody
clicks. What it must not become is a second place where the answer lives. An
``ApprovalOutcome`` is a transport event, not a verdict on the record, so this
module reduces it into the gate and lets the gate stay the single answer to
"was this approved, and by whom".

That is the same argument that keeps a foreign runtime's checkpointer out of
the platform, applied one layer down. Two records of what an agent was allowed
to do is one record too many, and a safety case cannot cite both.

Carrying the id
---------------

``ApprovalOutcome`` has exactly three fields — ``action_id``, ``actor``,
``thread_id`` — and no context dict, so there is nowhere to put the gate's id
except inside the option id the human clicks. Hence ``approve:<id>``, parsed on
the way back. It looks like a small encoding decision and it is the only one
available; a handler that instead kept its own map from message id to action id
would be state that dies with the process, which is the bug this whole branch
exists to fix.

Classification is not optional here
-----------------------------------

An approval prompt has to say what it is approving, and what it is approving is
``action.params``. Those params can be controlled.

The first version of this module took a channel and posted to it, with a
docstring saying the caller owned classification routing. That was a punt, and
it was wrong: it meant a controlled payload could be rendered into whatever
channel it was handed, bypassing the ceiling comparison that
``ChannelAdapterRegistry.admitted_for`` exists to make.

The fix is better than a refusal, because two different facts are being carried
and they usually have different classifications. *That an approval is waiting*
is almost always INTERNAL. *What it is approving* may not be. So when the
channel's ceiling does not admit the action's classification, the prompt still
goes out with the params withheld and a pointer to a terminal. The human learns
they are needed, and the payload does not cross. Silence would be worse for
both: nobody answers, and nobody knows why.

:func:`notify_pending` therefore takes ``classification`` and
``channel_ceiling`` with no defaults. Both are required so neither can be
skipped by forgetting it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from axiom.governance import Classification, classification_lte
from axiom.infra.orchestrator.actions import Action
from axiom.infra.orchestrator.approval import ApprovalGate

from .channels.interactive import (
    ApprovalOption,
    ApprovalOutcome,
    ApprovalRequest,
    InteractiveChannel,
)

log = logging.getLogger(__name__)

__all__ = [
    "APPROVE_PREFIX",
    "REJECT_PREFIX",
    "WITHHELD",
    "bind_gate_to_channel",
    "notify_pending",
    "parse_option_id",
    "summarize_for",
]

#: What a prompt says instead of the params when the channel cannot carry them.
WITHHELD = "details withheld"

APPROVE_PREFIX = "approve:"
REJECT_PREFIX = "reject:"


def _summarize(action: Action) -> str:
    """One line a human can answer from, without reading JSON.

    Params are rendered, not hidden: "approve action a1b2c3" tells the reader
    nothing they can decide on, and a prompt that cannot be decided on gets
    approved reflexively, which is worse than no prompt at all.
    """
    if not action.params:
        return action.name
    shown = ", ".join(f"{k}={v!r}" for k, v in sorted(action.params.items()))
    return f"{action.name}({shown})"


def summarize_for(
    action: Action,
    *,
    classification: Classification,
    channel_ceiling: Classification,
) -> tuple[str, bool]:
    """The line to post, and whether the params had to be withheld.

    Returned rather than posted so a caller can log or test the decision
    without a channel, and so the two facts stay separable: what to say, and
    whether saying it cost anything.
    """
    if classification_lte(classification, channel_ceiling):
        return _summarize(action), False
    return (
        f"{action.name} ({WITHHELD}: {classification.value}). "
        f"Answer at a terminal: axi approve ok {action.action_id}"
    ), True


def notify_pending(
    action: Action,
    channel: InteractiveChannel,
    *,
    classification: Classification,
    channel_ceiling: Classification,
    recipient_hint: str = "",
    thread_id: str | None = None,
) -> str:
    """Post one held action for a human to answer. Returns the message id.

    Args:
        classification: the action's own tier, from whoever proposed it. No
            default: a convenient one is how a controlled payload reaches a
            channel that should never have seen it.
        channel_ceiling: the highest tier ``channel`` may carry. Compared here
            rather than assumed, and when it is lower the params are withheld
            rather than the prompt suppressed.
    """
    summary, withheld = summarize_for(
        action, classification=classification, channel_ceiling=channel_ceiling
    )
    if withheld:
        log.info(
            "approval prompt for %s posted with params withheld: action is %s, "
            "channel ceiling is %s",
            action.action_id,
            classification.value,
            channel_ceiling.value,
        )
    prompt = f"Approval needed: {summary}"
    if recipient_hint:
        prompt = f"{prompt}\n{recipient_hint}"

    return channel.request_approval(
        ApprovalRequest(
            prompt=prompt,
            options=(
                ApprovalOption(
                    action_id=f"{APPROVE_PREFIX}{action.action_id}",
                    label="Approve",
                    style="primary",
                ),
                ApprovalOption(
                    action_id=f"{REJECT_PREFIX}{action.action_id}",
                    label="Reject",
                    style="danger",
                ),
            ),
            context={
                "gate_action_id": action.action_id,
                "action_name": action.name,
                "created_at": action.created_at,
            },
            thread_id=thread_id,
        )
    )


def parse_option_id(option_id: str) -> tuple[str, str] | None:
    """``"approve:ab12"`` becomes ``("approve", "ab12")``; anything else, None.

    Returning None rather than raising is deliberate. One channel carries the
    buttons of every conversation using it, so this handler sees clicks that
    were never ours — an incident conversation's Acknowledge, a verification
    gate's Confirm. Those are not malformed input, they are somebody else's
    traffic, and raising on them would make one bound gate break every other
    handler on the channel.
    """
    for prefix, verb in ((APPROVE_PREFIX, "approve"), (REJECT_PREFIX, "reject")):
        if option_id.startswith(prefix):
            rest = option_id[len(prefix) :]
            return (verb, rest) if rest else None
    return None


def bind_gate_to_channel(
    gate: ApprovalGate,
    channel: InteractiveChannel,
    *,
    on_decided: Callable[[Action], None] | None = None,
) -> None:
    """Route this channel's approve/reject clicks into ``gate``.

    ``on_decided`` fires only for a click that actually changed something, so a
    resume hook does not run twice when two people click Approve on the same
    prompt. The gate settles that race, because its ``approve`` is a no-op on
    an action that is no longer pending.
    """

    def _handle(outcome: ApprovalOutcome) -> None:
        parsed = parse_option_id(outcome.action_id)
        if parsed is None:
            return  # somebody else's button on a shared channel
        verb, gate_action_id = parsed

        before = gate.get(gate_action_id)
        if before is None:
            log.warning(
                "approval click for unknown action %s by %s; it may have expired "
                "or been purged",
                gate_action_id,
                outcome.actor,
            )
            return
        if before.status.value != "pending":
            log.info(
                "approval click for %s by %s ignored: already %s by %s",
                gate_action_id,
                outcome.actor,
                before.status.value,
                before.decided_by,
            )
            return

        # The clicker is the decider. Attribution comes from the channel's
        # identity of who clicked, never from whatever process is running this
        # handler, which is usually a service account.
        if verb == "approve":
            after = gate.approve(gate_action_id, decided_by=outcome.actor)
        else:
            after = gate.reject(
                gate_action_id, "rejected from channel", decided_by=outcome.actor
            )

        if after is not None and on_decided is not None:
            on_decided(after)

    channel.on_action(_handle)


def pending_summary(gate: ApprovalGate) -> list[dict[str, Any]]:
    """What is waiting, in the shape a digest notification wants."""
    return [
        {
            "action_id": a.action_id,
            "summary": _summarize(a),
            "created_at": a.created_at,
        }
        for a in gate.pending()
    ]
