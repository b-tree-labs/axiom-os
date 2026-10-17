# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The headline guarantee: the program is editable with NO external tracker or
harness present.

The full lifecycle — add a lane, add people (including one with no accounts),
add items, reassign, invite + redeem, remove — runs with no GitLab, no GitHub,
no Claude Code, no directory provider, and no tracker configured, and the change
log records every step. External trackers are optional feeders; a harness is
just one MCP client; neither is required to edit the program through its own
tool.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.program.model import load_program
from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import (
    invitation,
    items,
    lanes,
    ownership,
    people,
)
from axiom.extensions.builtins.program.skills import sync as sync_skill
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

DEPUTY = "@lead:example-org"

# A program with NO tracker, NO feeders, NO accounts anywhere.
MINIMAL: dict[str, Any] = {
    "schema": "axiom.program/0.1",
    "program": {"id": "example-program", "name": "Example", "deputy": DEPUTY},
    "lanes": [],
    "people": [{"principal": DEPUTY, "lanes": []}],
    "schedule": [],
}


@pytest.fixture(autouse=True)
def _no_external_world(monkeypatch):
    """Remove every hook into an external tracker/harness and the directory."""
    for var in (
        "GITLAB_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "CLAUDECODE",
        "CLAUDE_CODE",
        "AXIOM_GATE_API_KEYS_FILE",
        "AXIOM_GATE_INVITATIONS_FILE",
        "AXIOM_IDENTITY_POSTURE",
    ):
        monkeypatch.delenv(var, raising=False)
    # No directory provider → a supplied @name:context is accepted as given.
    monkeypatch.setenv("AXIOM_DIRECTORY_PROVIDER", "none")


@pytest.fixture
def state(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(MINIMAL, indent=1), encoding="utf-8")
    return state


def _ctx(state: Path) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.nodep"),
        user_prompt=None,
        surface="cli",
        principal=PrincipalContext(handle=DEPUTY, posture="open", assured=False),
    )


def test_full_lifecycle_with_no_tracker_or_harness(state):
    ctx = _ctx(state)

    def ok(result):
        assert result.ok, result.errors
        return result

    # 1. a lane
    ok(lanes.add({"id": "alpha", "name": "Alpha", "lead": DEPUTY}, ctx))

    # 2. people — one WITH no accounts at all (the partner/student/agent case)
    added = ok(people.add({"principal": "@dana:example-org", "lane": ["alpha"], "role": "eng"}, ctx))
    # resolved as-given because no directory provider is configured (DRY seam)
    assert added.value["identity"]["resolved_via"] == "as-given"
    ok(people.add({"principal": "@rowan:example-org", "lane": ["alpha"]}, ctx))

    # 3. items
    ok(items.add({"id": "i1", "label": "Build", "owner": "@dana:example-org", "lane": "alpha", "date": "2026-11-01"}, ctx))
    ok(items.add({"id": "i2", "label": "Ship", "owner": "@rowan:example-org", "lane": "alpha", "start": "2026-11-02", "end": "2026-11-09"}, ctx))

    # 4. reassign (no tracker → no account anywhere; the item still reassigns)
    ok(items.reassign({"id": "i1", "owner": "@rowan:example-org"}, ctx))

    # 5. invite + redeem — the gate primitive runs entirely offline
    code = ok(invitation.invite({"principal": "@partner:example-org", "lane": "alpha", "role": "guest"}, ctx)).value["code"]
    ok(invitation.redeem({"code": code}, ctx))

    # 6. remove (dana owns nothing now that i1 moved to rowan)
    ok(people.remove({"principal": "@dana:example-org"}, ctx))

    # --- the data file reflects every change ---
    data = load_program(state / "program" / "data.json", require_listed_owners=False)
    assert "alpha" in data.lane_ids()
    assert data.person("@dana:example-org") is None
    assert data.person("@partner:example-org") is not None  # redeemed membership recorded
    assert data.item("i1")["owner"] == "@rowan:example-org"

    # --- the change log records the whole lifecycle ---
    kinds = {e["kind"] for e in cl.read_changelog(state / "program" / "changelog.jsonl")}
    for expected in (
        "lane_added",
        "person_added",
        "item_added",
        "owner_changed",
        "invited",
        "redeemed",
        "person_removed",
    ):
        assert expected in kinds, (expected, kinds)

    # --- ownership-over-time reads back with no external anything ---
    hist = ownership.run({"scope": "item", "key": "i1"}, ctx)
    assert hist.ok and hist.value["current"] == "@rowan:example-org"


def test_self_reconcile_needs_no_feeder_and_double_logs_nothing(state):
    """`program sync` with no feeders configured is a safe local no-op, and an
    edit already advanced the snapshot so the reconcile re-logs nothing."""
    ctx = _ctx(state)
    assert lanes.add({"id": "alpha", "name": "Alpha"}, ctx).ok
    before = len(cl.read_changelog(state / "program" / "changelog.jsonl"))
    result = sync_skill.run({}, ctx)  # no --source-kind, no program.feeders
    assert result.ok, result.errors
    assert result.value["count"] == 0
    assert len(cl.read_changelog(state / "program" / "changelog.jsonl")) == before
