# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ports and databases are contended by servers. Files are contended by us.

A lane already keeps two sessions off each other's ports and out of each
other's database. Neither of those is what actually went wrong. What went
wrong was two sessions editing `navDefaults.ts`, two ADRs both numbered 137,
and two components both called Derivation — each found in review, after both
sides had been written.

So a lane can hold files, and the rule is REPORTED, NOT REFUSED. Two
sessions genuinely do need the same file sometimes, and a registry that
blocked it would be routed around within the hour. What neither side can
recover from is not knowing.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.lane.registry import Lane, Registry


@pytest.fixture
def reg(tmp_path):
    return Registry(tmp_path / "lanes.json")


def lane(name, **over):
    base = dict(name=name, front=8800, api=8801, root=f"/w/{name}")
    base.update(over)
    return Lane(**base)


def two(reg):
    reg.claim(lane("chat", front=8800, api=8801))
    reg.claim(lane("charts", front=8802, api=8803))


# --- holding -----------------------------------------------------------------


def test_a_lane_starts_holding_nothing():
    assert lane("chat").holds == []


def test_a_held_path_survives_the_round_trip(reg):
    reg.claim(lane("chat"))
    reg.hold("chat", ["frontend/src/navDefaults.ts"])

    assert reg.get("chat").holds == ["frontend/src/navDefaults.ts"]


def test_paths_are_stored_exactly_as_given(reg):
    """Two sessions typing the same repo-relative path must produce the same
    string. Resolving against each caller's worktree would make one
    reservation look like two, which is the failure this prevents."""
    reg.claim(lane("chat"))
    reg.hold("chat", ["frontend/src/index.ts"])

    assert reg.get("chat").holds == ["frontend/src/index.ts"]
    assert not any(h.startswith("/") for h in reg.get("chat").holds)


def test_holding_the_same_path_twice_does_not_duplicate_it(reg):
    reg.claim(lane("chat"))
    reg.hold("chat", ["a.ts"])
    reg.hold("chat", ["a.ts"])

    assert reg.get("chat").holds == ["a.ts"]


def test_holds_keep_the_order_they_were_taken(reg):
    reg.claim(lane("chat"))
    reg.hold("chat", ["b.ts", "a.ts"])
    reg.hold("chat", ["c.ts"])

    assert reg.get("chat").holds == ["b.ts", "a.ts", "c.ts"]


def test_holding_on_a_lane_that_does_not_exist_says_so(reg):
    with pytest.raises(KeyError):
        reg.hold("ghost", ["a.ts"])


# --- the answer arrives with the write, not at doctor time -------------------


def test_hold_reports_who_else_holds_it_immediately(reg):
    """Finding out at doctor time is finding out too late."""
    two(reg)
    reg.hold("charts", ["frontend/src/tokens.css"])

    clash = reg.hold("chat", ["frontend/src/tokens.css"])

    assert clash == {"frontend/src/tokens.css": ["charts"]}


def test_an_uncontested_hold_reports_nothing(reg):
    two(reg)
    assert reg.hold("chat", ["frontend/src/chat/ChatPanel.tsx"]) == {}


def test_a_lane_does_not_report_itself_as_a_clash(reg):
    reg.claim(lane("chat"))
    reg.hold("chat", ["a.ts"])

    assert reg.hold("chat", ["a.ts"]) == {}


def test_the_hold_is_taken_even_when_contested(reg):
    """Reported, not refused — a registry that blocked this gets routed
    around within the hour, and then nobody knows anything."""
    two(reg)
    reg.hold("charts", ["shared.ts"])
    reg.hold("chat", ["shared.ts"])

    assert "shared.ts" in reg.get("chat").holds
    assert "shared.ts" in reg.get("charts").holds


# --- dropping ----------------------------------------------------------------


def test_dropping_removes_only_what_was_named(reg):
    reg.claim(lane("chat"))
    reg.hold("chat", ["a.ts", "b.ts"])
    dropped = reg.drop("chat", ["a.ts"])

    assert dropped == ["a.ts"]
    assert reg.get("chat").holds == ["b.ts"]


def test_dropping_something_never_held_is_not_an_error(reg):
    reg.claim(lane("chat"))
    assert reg.drop("chat", ["never.ts"]) == []


def test_releasing_a_lane_takes_its_holds_with_it(reg):
    two(reg)
    reg.hold("chat", ["a.ts"])
    reg.release("chat")

    assert reg.contested() == {}
    assert reg.get("chat") is None


# --- what doctor sees --------------------------------------------------------


def test_contested_names_every_lane_holding_a_path(reg):
    two(reg)
    reg.claim(lane("lane3", front=8804, api=8805))
    for name in ("chat", "charts", "lane3"):
        reg.hold(name, ["frontend/src/tokens.css"])

    assert reg.contested() == {"frontend/src/tokens.css": ["charts", "chat", "lane3"]}


def test_a_path_held_once_is_not_contested(reg):
    two(reg)
    reg.hold("chat", ["mine.ts"])
    reg.hold("charts", ["theirs.ts"])

    assert reg.contested() == {}


def test_nothing_held_anywhere_is_not_a_finding(reg):
    two(reg)
    assert reg.contested() == {}


# --- retaking a lane ---------------------------------------------------------


def test_a_retake_keeps_the_files_the_lane_was_holding(reg):
    """`--force` is for moving a lane to a new port or branch. Silently
    dropping its files would hand them to another session mid-edit."""
    reg.claim(lane("chat"))
    reg.hold("chat", ["frontend/src/AccountMenu.tsx"])

    reg.claim(lane("chat", front=8810, api=8811, branch="feat/next"), replace=True)

    after = reg.get("chat")
    assert after.holds == ["frontend/src/AccountMenu.tsx"]
    assert after.front == 8810 and after.branch == "feat/next"


def test_a_retake_that_names_holds_of_its_own_keeps_both(reg):
    reg.claim(lane("chat"))
    reg.hold("chat", ["old.ts"])

    reg.claim(lane("chat", holds=["new.ts"]), replace=True)

    assert reg.get("chat").holds == ["old.ts", "new.ts"]
