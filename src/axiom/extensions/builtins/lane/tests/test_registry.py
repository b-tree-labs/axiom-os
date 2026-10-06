# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The registry: what it records, and what it refuses to lose.

A registry's failure mode is not crashing. It is handing two sessions the same
port, or recording an isolation a lane does not have, and in both cases the
second session simply loses with nothing to read.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.lane.registry import (
    RESERVED,
    UNMANAGED,
    Lane,
    LaneTaken,
    NoPortsFree,
    Registry,
    allocate_ports,
)


@pytest.fixture
def reg(tmp_path):
    return Registry(tmp_path / "lanes.json")


def _lane(name, front=8800, **kw):
    return Lane(name=name, front=front, api=front + 1, database=f"axiom_lane_{name}", **kw)


# --- round trip -------------------------------------------------------------


def test_a_claim_survives_a_reload(reg):
    reg.claim(_lane("chat", owner="me", branch="feat/chat"))

    got = Registry(reg.path).get("chat")
    assert got is not None
    assert (got.front, got.api, got.owner, got.branch) == (8800, 8801, "me", "feat/chat")


def test_an_absent_registry_reads_as_empty_not_as_an_error(reg):
    assert reg.all() == {}


def test_a_corrupt_registry_refuses_rather_than_reading_as_empty(reg):
    """Empty would re-hand every port that is currently in use."""
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text("{not json")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        reg.all()


def test_the_file_is_written_atomically(reg):
    reg.claim(_lane("chat"))
    data = json.loads(reg.path.read_text())

    assert "chat" in data["lanes"]
    assert not list(reg.path.parent.glob("*.tmp")), "a temp file was left behind"


# --- names ------------------------------------------------------------------


def test_claiming_a_held_name_says_who_holds_it(reg):
    reg.claim(_lane("chat", owner="first"))

    with pytest.raises(LaneTaken, match="first"):
        reg.claim(_lane("chat", owner="second"))


def test_replace_is_explicit(reg):
    reg.claim(_lane("chat", owner="first"))
    reg.claim(_lane("chat", owner="second"), replace=True)

    assert reg.get("chat").owner == "second"


def test_release_returns_what_it_removed_and_is_idempotent(reg):
    reg.claim(_lane("chat"))

    assert reg.release("chat").name == "chat"
    assert reg.release("chat") is None


# --- the three fields a first version would omit ---------------------------


def test_a_lane_records_which_variable_carries_its_isolation(reg):
    """One consumer reads STUDIO_GOLD_DSN, not AXIOM_DB_URL. Recording it as
    the default would record an isolation it does not have."""
    reg.claim(_lane("feed", dsn_var="STUDIO_GOLD_DSN"))

    assert reg.get("feed").dsn_var == "STUDIO_GOLD_DSN"


def test_a_lane_may_honestly_declare_itself_unisolated(reg):
    reg.claim(_lane("preview", dsn_var=UNMANAGED))
    lane = reg.get("preview")

    assert lane.dsn_var == UNMANAGED
    assert lane.isolated is False, "unmanaged must not read as isolated"


def test_an_ordinary_lane_reads_as_isolated(reg):
    reg.claim(_lane("chat"))

    assert reg.get("chat").isolated is True


def test_a_lane_records_the_trees_that_must_move_together(reg):
    """Isolating one of three composed checkouts draws a figure that is wrong
    rather than one that fails."""
    reg.claim(_lane("charts", trees=["axiom-appkit", "axiom-wt-principal", "uts-wt-studio"]))

    assert len(reg.get("charts").trees) == 3


def test_a_lane_records_the_editable_installs_it_depends_on(reg):
    """A landed, clean worktree can still be what a running server imports."""
    reg.claim(_lane("chat", venvs=["/Users/x/.venv:axiom_appkit"]))

    assert reg.get("chat").venvs == ["/Users/x/.venv:axiom_appkit"]


def test_defaults_are_sane_when_a_field_was_never_written(reg):
    """Older entries predate these fields; they must load, not explode."""
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text(
        json.dumps({"lanes": {"old": {"name": "old", "front": 8800, "api": 8801, "database": "d"}}})
    )
    lane = reg.get("old")

    assert lane.dsn_var == "AXIOM_DB_URL"
    assert lane.trees == [] and lane.venvs == []


def test_an_unknown_field_in_the_file_does_not_break_loading(reg):
    """Another tool writes this file too; it may know fields we do not."""
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text(
        json.dumps(
            {
                "lanes": {
                    "x": {
                        "name": "x",
                        "front": 8800,
                        "api": 8801,
                        "database": "d",
                        "invented": "later",
                    }
                }
            }
        )
    )

    assert reg.get("x").name == "x"


# --- port allocation --------------------------------------------------------


def test_a_lane_never_gets_a_reserved_port():
    got = allocate_ports("anything", taken=set())

    assert not (set(got) & set(RESERVED))


def test_allocation_avoids_claimed_pairs():
    front, api = allocate_ports("chat", taken={8800, 8801})

    assert (front, api) != (8800, 8801)
    assert front not in (8800, 8801) and api not in (8800, 8801)


def test_allocation_also_avoids_a_port_that_is_merely_listening():
    """The registry records intent. Intent does not own a socket."""
    first = allocate_ports("chat", taken=set())
    second = allocate_ports("chat", taken=set(), listening=set(first))

    assert second != first


def test_the_same_name_lands_in_the_same_place_when_nothing_is_taken():
    assert allocate_ports("chat", taken=set()) == allocate_ports("chat", taken=set())


def test_a_full_range_says_so_rather_than_returning_something_wrong():
    from axiom.extensions.builtins.lane import naming

    every = set(range(naming.FIRST_LANE_PORT, naming.LAST_LANE_PORT + 1))
    with pytest.raises(NoPortsFree):
        allocate_ports("chat", taken=every)


# --- the lock ---------------------------------------------------------------


def test_a_stale_lock_times_out_with_an_actionable_message(reg):
    reg.lock_path.parent.mkdir(parents=True, exist_ok=True)
    reg.lock_path.write_text("99999")

    with pytest.raises(TimeoutError, match="remove it"):
        with reg.locked(timeout=0.1):
            pass


def test_the_lock_is_released_even_when_the_body_raises(reg):
    with pytest.raises(ValueError):
        with reg.locked():
            raise ValueError("boom")

    assert not reg.lock_path.exists()
    reg.claim(_lane("chat"))  # proves the lock is usable again


def test_an_unmanaged_lane_round_trips_through_the_file(reg):
    """Regression: `claim` omits empty values, so a lane with no database
    could be written and then not read back."""
    reg.claim(Lane(name="preview", front=8800, api=8801, database="", dsn_var=UNMANAGED))

    lane = Registry(reg.path).get("preview")
    assert lane is not None and lane.database == "" and lane.isolated is False
