# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Calendar ⇄ DT-gate composition (D2 as-sink / as-trigger).

Lives in ``connect`` (the integration layer) so neither ``schedule`` (calendars)
nor ``notifications`` (the gate) depends on the other — the binding is here.

- **as-sink**: a DT-predicted maintenance window is written to the operator's
  calendar (``write_predicted_window``), carrying the prediction in event
  metadata so the start event can reconstruct it.
- **as-trigger**: when that event fires (its start/reminder), open a
  ``DTVerificationGate`` on the owner's channel (``open_gate_for_event``) —
  closing the loop: predicted schedule → reminder → verify predicted-vs-measured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def write_predicted_window(
    provider: Any,
    *,
    title: str,
    start: datetime,
    end: datetime,
    predicted_value: float | str | None = None,
    unit: str = "",
    tolerance: float | None = None,
    attendees: tuple[str, ...] = (),
    slot_id: str | None = None,
) -> Any:
    """as-sink: write the twin-predicted window to the calendar, stamping the
    prediction into event metadata so the trigger can rebuild it."""
    from axiom.extensions.builtins.schedule.calendar import EventSpec

    meta: dict[str, Any] = {"dt_prediction": "1"}
    if predicted_value is not None:
        meta["predicted_value"] = predicted_value
    if unit:
        meta["unit"] = unit
    if tolerance is not None:
        meta["tolerance"] = tolerance
    if slot_id:
        meta["slot_id"] = slot_id
    return provider.create_event(
        EventSpec(summary=title, start=start, end=end, attendees=list(attendees), metadata=meta)
    )


def prediction_from_event_metadata(title: str, metadata: dict) -> Any:
    """Rebuild a Prediction from a calendar event's stamped metadata."""
    from axiom.extensions.builtins.notifications.verification_gate import Prediction

    pv = metadata.get("predicted_value")
    tol = metadata.get("tolerance")
    return Prediction(
        title=title,
        predicted_value=float(pv) if isinstance(pv, str) and _is_num(pv) else pv,
        unit=str(metadata.get("unit", "")),
        tolerance=float(tol) if isinstance(tol, str) and _is_num(tol) else tol,
    )


def open_gate_for_event(
    channel: Any,
    *,
    title: str,
    metadata: dict,
    on_verified: Any,
    agent: str = "Axi",
    agent_icon: str | None = None,
) -> Any:
    """as-trigger: a fired calendar event opens a DT verification gate on the
    owner's channel. Returns the live gate."""
    from axiom.extensions.builtins.notifications.verification_gate import DTVerificationGate

    gate = DTVerificationGate(channel, on_verified=on_verified, agent=agent, agent_icon=agent_icon)
    gate.open(prediction_from_event_metadata(title, metadata))
    return gate


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


__all__ = ["write_predicted_window", "prediction_from_event_metadata", "open_gate_for_event"]
