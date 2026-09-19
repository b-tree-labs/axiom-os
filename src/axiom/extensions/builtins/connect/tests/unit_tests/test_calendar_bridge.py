# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""D2 — calendar ⇄ DT-gate loop: as-sink writes the predicted window,
as-trigger opens the verification gate when it fires."""

from __future__ import annotations

from datetime import datetime, timedelta

from axiom.extensions.builtins.connect.calendar_bridge import (
    open_gate_for_event,
    write_predicted_window,
)
from axiom.extensions.builtins.notifications.channels.interactive import InMemoryInteractiveChannel
from axiom.extensions.builtins.schedule.calendar.vendors.fake import FakeCalendarProvider

_T0 = datetime(2026, 6, 17, 9, 0)


def test_as_sink_writes_window_with_prediction_metadata():
    cal = FakeCalendarProvider()
    write_predicted_window(
        cal, title="maintenance window", start=_T0, end=_T0 + timedelta(hours=1),
        predicted_value=2.1, unit="units", tolerance=0.3, attendees=("op@example.org",), slot_id="S1",
    )
    spec = cal.find_event(private_key="slot_id", private_value="S1")
    assert spec is not None
    assert spec.metadata["predicted_value"] == 2.1 and spec.metadata["slot_id"] == "S1"


def test_as_trigger_opens_gate_and_verifies():
    # Simulate the loop: write the window (sink), then the start event fires →
    # open the gate (trigger) from the event's metadata; a measured reply verifies.
    cal = FakeCalendarProvider()
    write_predicted_window(
        cal, title="end-of-window reading", start=_T0, end=_T0 + timedelta(hours=1),
        predicted_value=2.1, unit="units", tolerance=0.3,
    )
    spec = cal.find_event(private_key="dt_prediction", private_value="1")
    assert spec is not None

    channel = InMemoryInteractiveChannel()
    out = []
    open_gate_for_event(channel, title=spec.summary, metadata=spec.metadata,
                        on_verified=out.append, agent="Ben's Axi")
    # the operator replies with the measured value
    channel.inject_message("2.2", author="@op")
    assert out and out[0].measured == 2.2 and out[0].in_tolerance is True
    assert any("Twin prediction" in t for t in channel.texts())
