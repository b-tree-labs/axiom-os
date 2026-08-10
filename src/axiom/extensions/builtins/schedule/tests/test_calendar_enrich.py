# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Event enrichment — the carrier fields that turn an event into a
self-describing workflow hub (identity + links + state + reminders), so the
calendar becomes a projection of the source of truth rather than a hand-kept copy."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from axiom.extensions.builtins.schedule.calendar.protocol import (
    EventSpec,
    render_description,
)
from axiom.extensions.builtins.schedule.calendar.vendors import google

T0 = datetime(2026, 6, 8, 9, 0, 0, tzinfo=UTC)


def _rich_spec():
    return EventSpec(
        summary="Campaign ABC-0042",
        start=T0,
        end=T0 + timedelta(hours=1),
        status_line="STATE: Scheduled",
        description="Target in position P-3.",
        links=[
            {"label": "Open record", "url": "https://em.example/c/ABC-0042"},
            {"label": "Plan/predict", "url": "https://em.example/calc/ABC-0042"},
        ],
        reminders_minutes=[120, 15],
        color="5",
        metadata={"campaign_id": "ABC-0042", "authorization": "EA-7", "position": "P-3"},
    )


def test_render_description_is_self_describing():
    desc = render_description(_rich_spec())
    assert desc.startswith("STATE: Scheduled")              # state up top
    assert "Target in position P-3." in desc                # body
    assert "Open record: https://em.example/c/ABC-0042" in desc   # deep link
    # machine-readable block survives export (the air-gap-friendly payload)
    assert "```axiom" in desc and '"campaign_id": "ABC-0042"' in desc


def test_spec_to_body_carries_the_enrichment():
    body = google.spec_to_body(_rich_spec())
    # identity → private extended properties (machine, for reconcile)
    assert body["extendedProperties"]["private"]["campaign_id"] == "ABC-0042"
    assert body["extendedProperties"]["private"]["authorization"] == "EA-7"
    # rich description with links + state
    assert "STATE: Scheduled" in body["description"]
    assert "Plan/predict" in body["description"]
    # built-in reminders (one source, not a manual extra)
    mins = [o["minutes"] for o in body["reminders"]["overrides"]]
    assert mins == [120, 15] and body["reminders"]["useDefault"] is False
    # color (lifecycle/state signal)
    assert body["colorId"] == "5"


def test_round_trip_reads_identity_back():
    # The stamped identity round-trips so sync can re-find + reconcile.
    body = google.spec_to_body(_rich_spec())
    event = {"id": "e1", "summary": body["summary"], "start": body["start"],
             "extendedProperties": body["extendedProperties"]}
    spec = google.event_to_spec(event, "primary")
    assert spec.metadata["campaign_id"] == "ABC-0042"


def test_agent_can_be_named_organizer_with_a_realtime_thread_link():
    # An agent (@AXI) authors the event + links a Slack/Teams thread (async<->sync).
    spec = EventSpec(summary="Irr run", start=T0, organizer="@AXI:axiom",
                     thread_url="https://teams.microsoft.com/l/thread/19:abc")
    desc = render_description(spec)
    assert "Organizer: @AXI:axiom" in desc            # agent named as author
    assert "Live thread: https://teams.microsoft.com/l/thread/19:abc" in desc
