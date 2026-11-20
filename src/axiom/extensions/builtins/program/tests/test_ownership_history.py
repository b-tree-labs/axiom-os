# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Ownership over time: both current and historical owners, reported accurately.

``data.json`` carries the current owner; the change log carries the history.
The ``program ownership`` read reconstructs the timeline from the immutable
``lane_owner_changed`` / ``owner_changed`` transitions, so a principal who held
ownership in the past is reported correctly even after they are removed from the
roster — a removal cannot rewrite who owned the work at a past instant.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.program.skills import items, lanes, ownership, people
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

DEPUTY = "@casey:example-org"

BASE: dict[str, Any] = {
    "schema": "axiom.program/0.1",
    "program": {"id": "example-program", "name": "Example", "deputy": DEPUTY},
    "lanes": [],
    "people": [
        {"principal": DEPUTY, "lanes": []},
        {"principal": "@owner0:example-org", "lanes": []},
        {"principal": "@owner1:example-org", "lanes": []},
        {"principal": "@owner2:example-org", "lanes": []},
    ],
    "schedule": [],
}


def _seed(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(copy.deepcopy(BASE), indent=1), encoding="utf-8")
    return state


def _ctx(state: Path) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.ownership"),
        user_prompt=None,
        surface="cli",
        principal=PrincipalContext(handle=DEPUTY, posture="open", assured=False),
    )


def _assert_chain(history: list[dict[str, Any]]) -> None:
    """Each span hands off to the next, and the last is open."""
    for earlier, later in zip(history, history[1:]):
        assert earlier["to"] == later["from"]
    assert history[-1]["to"] is None


def test_lane_ownership_survives_the_first_owner_leaving(tmp_path):
    state = _seed(tmp_path)
    ctx = _ctx(state)

    # a lane created WITH an initial lead, then reassigned twice
    assert lanes.add({"id": "alpha", "name": "Alpha", "lead": "@owner0:example-org"}, ctx).ok
    assert lanes.edit({"id": "alpha", "lead": "@owner1:example-org"}, ctx).ok
    assert lanes.edit({"id": "alpha", "lead": "@owner2:example-org"}, ctx).ok

    # the first owner leaves the roster entirely
    assert people.remove({"principal": "@owner0:example-org"}, ctx).ok

    r = ownership.run({"scope": "lane", "key": "alpha"}, ctx)
    assert r.ok, r.errors
    history = r.value["history"]
    principals = [span["principal"] for span in history]

    # all three owners are reported, in order — including the one now gone
    assert principals == ["@owner0:example-org", "@owner1:example-org", "@owner2:example-org"]
    assert r.value["current"] == "@owner2:example-org"
    # the removed principal still appears accurately in history
    assert "@owner0:example-org" in principals
    _assert_chain(history)
    # provenance: every transition names who made it
    assert all(span["set_by"] == DEPUTY for span in history[1:])


def test_item_ownership_timeline(tmp_path):
    state = _seed(tmp_path)
    ctx = _ctx(state)

    assert lanes.add({"id": "alpha", "name": "Alpha"}, ctx).ok
    assert items.add(
        {"id": "i1", "label": "Build", "owner": "@owner0:example-org", "lane": "alpha", "date": "2026-11-01"},
        ctx,
    ).ok
    assert items.reassign({"id": "i1", "owner": "@owner1:example-org"}, ctx).ok
    assert items.reassign({"id": "i1", "owner": "@owner2:example-org"}, ctx).ok

    # remove the first owner — they no longer own anything, so removal is clean
    assert people.remove({"principal": "@owner0:example-org"}, ctx).ok

    r = ownership.run({"scope": "item", "key": "i1"}, ctx)
    assert r.ok, r.errors
    principals = [span["principal"] for span in r.value["history"]]
    assert principals == ["@owner0:example-org", "@owner1:example-org", "@owner2:example-org"]
    assert r.value["current"] == "@owner2:example-org"
    _assert_chain(r.value["history"])


def test_single_owner_is_one_open_span(tmp_path):
    state = _seed(tmp_path)
    ctx = _ctx(state)
    assert lanes.add({"id": "alpha", "name": "Alpha", "lead": "@owner0:example-org"}, ctx).ok
    r = ownership.run({"scope": "lane", "key": "alpha"}, ctx)
    assert r.ok, r.errors
    assert [s["principal"] for s in r.value["history"]] == ["@owner0:example-org"]
    assert r.value["history"][0]["to"] is None


def test_unknown_subject_is_absent(tmp_path):
    state = _seed(tmp_path)
    r = ownership.run({"scope": "item", "key": "nope"}, _ctx(state))
    assert not r.ok and r.value["refused"] == "absent"
