# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Digital-twin verification gate — the channel-agnostic HITL touchpoint where
the twin's value adds up (ADR-074, B2).

A digital twin's worth is realized at the **prediction → verification** moment:
the twin predicts a quantity (activity, dose, k-eff, …); a human confirms it
matches reality, or enters the measured value. This conversation surfaces that
prediction on *any* ``InteractiveChannel`` and captures the human's confirm /
measured-value / reject, then hands the result to an injected ``on_verified``
sink (a consumer such as Expman binds that to the schedule seam's
``record_actual`` — predicted-vs-measured == planned-vs-actual).

Vendor- and consumer-neutral by construction: it knows only "a prediction, a
confirmation, an optional measured value". It mirrors ``IncidentConversation``'s
choreography so the same shape proves out across In-Memory → Slack → SMS → Email.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .channels.interactive import (
    ApprovalOption,
    ApprovalOutcome,
    ApprovalRequest,
    ChannelMessage,
    InteractiveChannel,
)

# responder(question, prediction_context) -> answer text (free-form twin Q&A)
Responder = Callable[[str, dict], str]


@dataclass
class Prediction:
    """A digital-twin prediction awaiting human verification."""

    title: str
    predicted_value: float | str | None = None
    unit: str = ""
    tolerance: float | None = None  # absolute; if set + numeric, drives in_tolerance
    detail: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class VerificationOutcome:
    """The human's verdict on a prediction."""

    prediction: Prediction
    actor: str
    measured: float | str | None = None
    confirmed: bool = False          # human asserted the prediction matches
    rejected: bool = False
    in_tolerance: bool | None = None  # None when not numerically checkable


VerificationSink = Callable[[VerificationOutcome], Any]

_CONFIRM, _MEASURE, _REJECT = "confirm", "measure", "reject"


def _fmt_value(v: Any, unit: str) -> str:
    return f"{v}{(' ' + unit) if unit else ''}" if v is not None else "(unspecified)"


def _build_brief(p: Prediction) -> str:
    lines = [f":microscope: *Twin prediction: {p.title}*"]
    if p.predicted_value is not None:
        tol = f" (±{p.tolerance})" if p.tolerance is not None else ""
        lines.append(f"• predicted: *{_fmt_value(p.predicted_value, p.unit)}*{tol}")
    if p.detail:
        lines.append(p.detail)
    lines.append("Confirm it matches, enter the measured value, or reject.")
    return "\n".join(lines)


def _default_responder(question: str, ctx: dict) -> str:
    pv = ctx.get("predicted_value")
    unit = ctx.get("unit", "")
    if pv is not None and any(w in question.lower() for w in ("predict", "value", "expect", "twin", "what")):
        return f"The twin predicts {_fmt_value(pv, unit)}. Confirm if it matches, or reply with the measured value."
    return "I can tell you the predicted value, or you can confirm / enter the measured value to verify."


class DTVerificationGate:
    """One prediction's verification conversation + lifecycle on a channel."""

    def __init__(
        self,
        channel: InteractiveChannel,
        *,
        on_verified: VerificationSink,
        responder: Responder | None = None,
        agent: str = "Axi",
        agent_icon: str | None = None,
    ) -> None:
        self._ch = channel
        self._on_verified = on_verified
        self._responder = responder or _default_responder
        self._agent = agent
        self._agent_icon = agent_icon
        self.status = "new"  # new → awaiting → awaiting_measure → verified | rejected
        self.thread_id: str | None = None
        self._prediction: Prediction | None = None

    def open(self, prediction: Prediction) -> str:
        self._prediction = prediction
        self.thread_id = self._ch.post(_build_brief(prediction), author=self._agent, icon_url=self._agent_icon)
        self._ch.request_approval(
            ApprovalRequest(
                prompt="Does the measurement match the twin's prediction?",
                options=(
                    ApprovalOption(_CONFIRM, "Confirm match", style="primary"),
                    ApprovalOption(_MEASURE, "Enter measured value"),
                    ApprovalOption(_REJECT, "Reject", style="danger"),
                ),
                context={"prediction": prediction.title},
                thread_id=self.thread_id,
            )
        )
        self._ch.on_message(self._handle_message)
        self._ch.on_action(self._handle_action)
        self.status = "awaiting"
        return self.thread_id

    # --- context for the responder -----------------------------------------
    def _context(self) -> dict[str, Any]:
        p = self._prediction
        if p is None:
            return {}
        return {"title": p.title, "predicted_value": p.predicted_value, "unit": p.unit,
                "tolerance": p.tolerance, **dict(p.metadata)}

    def _in_tolerance(self, measured: float) -> bool | None:
        p = self._prediction
        if p is None or p.tolerance is None or not isinstance(p.predicted_value, (int, float)):
            return None
        return abs(measured - float(p.predicted_value)) <= p.tolerance

    def _finish(self, outcome: VerificationOutcome) -> None:
        self.status = "rejected" if outcome.rejected else "verified"
        self._on_verified(outcome)

    # --- inbound handlers ---------------------------------------------------
    def _handle_action(self, outcome: ApprovalOutcome) -> None:
        if self.status not in ("awaiting", "awaiting_measure"):
            return
        if outcome.action_id == _CONFIRM:
            p = self._prediction
            self._ch.post(
                f"Confirmed by {outcome.actor} — recording the twin value as measured.",
                thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon,
            )
            self._finish(VerificationOutcome(
                prediction=p, actor=outcome.actor,
                measured=p.predicted_value if p else None, confirmed=True, in_tolerance=True,
            ))
        elif outcome.action_id == _MEASURE:
            self.status = "awaiting_measure"
            self._ch.post(
                "Reply with the measured value (a number).",
                thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon,
            )
        elif outcome.action_id == _REJECT:
            self._ch.post(
                f"Rejected by {outcome.actor} — no value recorded; prediction left open.",
                thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon,
            )
            self._finish(VerificationOutcome(
                prediction=self._prediction, actor=outcome.actor, rejected=True,
            ))

    def _handle_message(self, msg: ChannelMessage) -> None:
        if msg.is_agent or self.status not in ("awaiting", "awaiting_measure"):
            return
        # A bare number is a measured value at any point while awaiting — so SMS
        # (no buttons) works: "reply with the measured value" needs no prior tap.
        measured = self._parse_measured(msg.text)
        if measured is not None:
            ok = self._in_tolerance(measured)
            flag = "" if ok is None else (" :white_check_mark: within tolerance" if ok
                                          else " :warning: OUT of tolerance")
            self._ch.post(f"Recorded measured = {measured}{flag}.",
                          thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon)
            self._finish(VerificationOutcome(
                prediction=self._prediction, actor=msg.author, measured=measured,
                confirmed=False, in_tolerance=ok,
            ))
            return
        if self.status == "awaiting_measure":
            self._ch.post("That doesn't look like a number — reply with the measured value.",
                          thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon)
            return
        # Free-form twin Q&A before resolution.
        answer = self._responder(msg.text, self._context())
        if answer:
            self._ch.post(answer, thread_id=self.thread_id, author=self._agent, icon_url=self._agent_icon)

    @staticmethod
    def _parse_measured(text: str) -> float | None:
        tok = text.strip().split()
        for piece in (text.strip(), tok[0] if tok else ""):
            try:
                return float(piece)
            except ValueError:
                continue
        return None


__all__ = ["Prediction", "VerificationOutcome", "VerificationSink", "DTVerificationGate"]
