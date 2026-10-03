# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET names the lane a merge belonged to. It infers nothing."""

from __future__ import annotations

from axiom.extensions.builtins.lane.registry import Lane
from axiom.extensions.builtins.release import lane_signal


LANES = {
    "chat": Lane(name="chat", front=8802, api=8803, database="axiom_lane_chat",
                 branch="feat/chat-surface-parity"),
    "steer": Lane(name="steer", front=8800, api=8801, database="axiom_lane_steer",
                  branch="feat/steer-surface"),
}


def test_a_branch_finds_its_lane():
    assert lane_signal.lane_for_branch("feat/steer-surface", LANES).name == "steer"


def test_an_unclaimed_branch_finds_nothing():
    assert lane_signal.lane_for_branch("feat/something-else", LANES) is None


def test_matching_is_exact_not_fuzzy():
    """A near-match is worse than none: acting on the wrong lane is the
    failure this is meant to prevent."""
    assert lane_signal.lane_for_branch("feat/steer", LANES) is None
    assert lane_signal.lane_for_branch("feat/steer-surface-r2", LANES) is None


def test_an_empty_branch_is_not_a_match():
    assert lane_signal.lane_for_branch("", LANES) is None


def test_an_event_gains_the_lane_it_belonged_to():
    out = lane_signal.annotate({"event": "rivet.pr_merged", "branch": "feat/chat-surface-parity"}, LANES)

    assert out["lane"] == "chat"
    assert out["lane_database"] == "axiom_lane_chat"
    assert out["event"] == "rivet.pr_merged", "the original event survives"


def test_an_event_with_no_lane_gains_no_empty_keys():
    """So a consumer can tell 'no lane' from 'lane unknown'."""
    out = lane_signal.annotate({"event": "rivet.pr_merged", "branch": "feat/other"}, LANES)

    assert "lane" not in out
    assert out == {"event": "rivet.pr_merged", "branch": "feat/other"}


def test_annotate_does_not_mutate_its_input():
    event = {"event": "rivet.pr_merged", "branch": "feat/chat-surface-parity"}
    lane_signal.annotate(event, LANES)

    assert "lane" not in event
