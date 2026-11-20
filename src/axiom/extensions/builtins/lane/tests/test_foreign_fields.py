# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A writer must not erase what it does not understand.

Two tools shared this registry during the consolidation, and this one
serialised its own dataclass over the stored entry — so every key the other
tool owned vanished on the next write from here. That tool subscripted one
of them and raised for every caller.

It is the failure nobody notices: the file still parses, still looks right,
and the missing field only surfaces in whatever reads it.

The property outlives the transition. A registry two tools share needs it,
and a registry one tool owns still wants it the first time somebody edits
the JSON by hand.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.lane.registry import Lane, Registry


@pytest.fixture
def reg(tmp_path):
    return Registry(tmp_path / "lanes.json")


def written(reg, name="standing"):
    return json.loads(reg.path.read_text())["lanes"][name]


def foreign(reg, **extra):
    """An entry as another writer left it."""
    body = {"name": "standing", "front": 8770, "api": 8771, "owner": "them"}
    body.update(extra)
    reg.path.write_text(json.dumps({"lanes": {"standing": body}}))


def test_a_field_this_dataclass_does_not_know_survives_a_write(reg):
    foreign(reg, custom_note="keep me", schema_version=3)
    lane = reg.get("standing")
    lane.trees = ["appkit-service/src"]
    reg.claim(lane, replace=True)

    after = written(reg)
    assert after["custom_note"] == "keep me"
    assert after["schema_version"] == 3


def test_the_fields_it_does_own_are_still_authoritative(reg):
    foreign(reg, custom_note="keep me")
    lane = reg.get("standing")
    lane.owner = "me"
    reg.claim(lane, replace=True)

    after = written(reg)
    assert after["owner"] == "me"
    assert after["custom_note"] == "keep me"


def test_clearing_an_owned_field_still_removes_it(reg):
    """Round-tripping must not resurrect a value the caller cleared — that
    is how an unmanaged lane keeps an empty database out of the file."""
    foreign(reg, database="axiom_lane_old")
    lane = reg.get("standing")
    lane.database = ""
    reg.claim(lane, replace=True)

    assert "database" not in written(reg)


def test_holding_files_does_not_disturb_foreign_keys(reg):
    foreign(reg, custom_note="keep me")
    reg.hold("standing", ["frontend/src/tokens.css"])

    after = written(reg)
    assert after["custom_note"] == "keep me"
    assert after["holds"] == ["frontend/src/tokens.css"]


def test_a_foreign_key_survives_many_writes(reg):
    foreign(reg, custom_note="keep me")
    for i in range(4):
        lane = reg.get("standing")
        lane.note = f"pass {i}"
        reg.claim(lane, replace=True)

    assert written(reg)["custom_note"] == "keep me"


def test_a_brand_new_lane_carries_nothing_extra(reg):
    reg.claim(Lane(name="chat", front=8802, api=8803))

    assert set(written(reg, "chat")) <= set(Lane.__dataclass_fields__)
