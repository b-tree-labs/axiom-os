# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The fixture has to hold the properties it exists to demonstrate."""

from __future__ import annotations

from datetime import UTC, datetime

from axiom.extensions.builtins.receipts.simulation import (
    instrument_fleet_at_scale,
    robot_fleet_at_scale,
)

NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)


def test_the_fleet_is_unverified_dominant():
    """The mix is the whole point. In a fleet of AI endpoints the dominant
    state is neither broken nor healthy — it is UNVERIFIED: working,
    reporting fine, and never checked by anything but itself.

    A fixture where everything is either fine or failing is a fixture for a
    monitoring tool, and would quietly train us to build one."""
    mix = robot_fleet_at_scale(now=NOW).counts_by_status()
    assert mix["unproven"] > sum(v for k, v in mix.items() if k != "unproven"), mix
    # ...and it still has some of each, or the surface cannot be exercised.
    assert mix["failed"] >= 1 and mix["stale"] >= 1 and mix["green"] >= 1


def test_it_is_deterministic():
    """A brief composed from this is asserted on by word, so the same seed
    must give the same fleet and the same history."""
    a = robot_fleet_at_scale(now=NOW)
    b = robot_fleet_at_scale(now=NOW)
    assert a.items == b.items
    assert a.history == b.history
    assert robot_fleet_at_scale(now=NOW, seed=8).items != a.items


def test_the_record_carries_what_calibration_will_need():
    """Every decision has an outcome observed independently of whoever
    decided — the column a ticket history does not have, and the one
    `calibrate` is computed from."""
    fleet = robot_fleet_at_scale(now=NOW)
    assert fleet.history, "no record to calibrate on"
    assert all(isinstance(d.cleared, bool) for d in fleet.history)
    assert {d.decider_kind for d in fleet.history} == {"human", "agent"}


def test_the_two_deciders_are_distinguishable_from_the_record_alone():
    """The ground truth is that the agent clears less often than the human.
    Nothing in the surface reads the rates that generated this — a
    calibration measurement has to RECOVER the difference from the record,
    which is how we will know it works rather than merely runs."""
    fleet = robot_fleet_at_scale(now=NOW)
    human_cleared, human_total = fleet.calibration_of("@sam:dc-3")
    agent_cleared, agent_total = fleet.calibration_of("@fleet-warden:dc-3")
    assert human_total and agent_total
    assert (human_cleared / human_total) > (agent_cleared / agent_total)


def test_the_contrasting_fleet_needs_no_second_generator():
    """The abstraction check. The instrument fleet is the same construct
    with a different vocabulary — an agent asserting about DATA rather than
    acting on the world. If it needed its own generator, this was about
    robots after all."""
    instruments = instrument_fleet_at_scale(now=NOW)
    mix = instruments.counts_by_status()
    assert mix["unproven"] > 0 and mix["failed"] > 0
    assert {i.entity_kind for i in instruments.items} == {"instrument"}
    assert instruments.history
