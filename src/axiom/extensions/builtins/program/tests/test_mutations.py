# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The mutation surface: add/edit/remove/reassign people, lanes, and items.

Each verb is exercised through its skill ``run`` function against a program
seeded in a test-only state dir. The fixtures are invented-generic (example
people, example lanes, a neutral ``forge`` account system) — the shape mirrors
``axiom.program/0.1``; the vocabulary mirrors nobody's deployment.
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.program.model import load_program
from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import items, lanes, people
from axiom.extensions.builtins.program.skills import sync as sync_skill
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext, SkillRegistry

DEPUTY = "@casey:example-org"


def _seed(tmp_path: Path, program: dict[str, Any]) -> Path:
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(program, indent=1), encoding="utf-8")
    return state


def _ctx(state: Path, *, principal: str = DEPUTY, assured: bool = False) -> SkillContext:
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.mut"),
        user_prompt=None,
        surface="cli",
        principal=PrincipalContext(
            handle=principal, posture=("attested" if assured else "open"), assured=assured
        ),
    )


def _reload(state: Path):
    return load_program(state / "program" / "data.json", require_listed_owners=False)


def _changelog(state: Path) -> Path:
    return state / "program" / "changelog.jsonl"


def _kinds(state: Path) -> list[str]:
    return [e["kind"] for e in cl.read_changelog(_changelog(state))]


# A small, valid program with a tracked forge, two lanes, three members.
BASE: dict[str, Any] = {
    "schema": "axiom.program/0.1",
    "program": {
        "id": "example-program",
        "name": "Example",
        "deputy": DEPUTY,
        "maintainers": ["@mort:example-org"],
        "tracker": {"kind": "forge", "host": "forge.example"},
    },
    "lanes": [
        {"id": "alpha", "name": "Alpha", "lead": "@casey:example-org"},
        {"id": "beta", "name": "Beta"},
    ],
    "people": [
        {"principal": DEPUTY, "name": "Casey", "lanes": ["alpha"], "accounts": {"forge": "casey9"}},
        {"principal": "@dana:example-org", "name": "Dana", "lanes": ["alpha"]},
        {"principal": "@mort:example-org", "name": "Mort", "lanes": ["beta"]},
    ],
    "schedule": [
        {"id": "i1", "label": "Build", "owner": "@dana:example-org", "lane": "alpha", "date": "2026-11-01"},
    ],
}


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return _seed(tmp_path, copy.deepcopy(BASE))


# --- people ----------------------------------------------------------------


class TestPeople:
    def test_add_a_member(self, state):
        r = people.add(
            {"principal": "@rory:example-org", "lane": ["beta"], "role": "eng", "account": ["forge=rory3"]},
            _ctx(state),
        )
        assert r.ok, r.errors
        data = _reload(state)
        person = data.person("@rory:example-org")
        assert person is not None
        assert person["lanes"] == ["beta"]
        assert person["accounts"] == {"forge": "rory3"}
        assert "person_added" in _kinds(state)

    def test_add_duplicate_is_conflict(self, state):
        r = people.add({"principal": "@dana:example-org", "lane": ["alpha"]}, _ctx(state))
        assert not r.ok and r.value["refused"] == "conflict"

    def test_add_unknown_lane_is_bad_request(self, state):
        r = people.add({"principal": "@rory:example-org", "lane": ["ghost"]}, _ctx(state))
        assert not r.ok and r.value["refused"] == "bad_request"

    def test_add_bad_principal_is_bad_request(self, state):
        r = people.add({"principal": "rory", "lane": ["beta"]}, _ctx(state))
        assert not r.ok and r.value["refused"] == "bad_request"

    def test_edit_merges_accounts(self, state):
        r = people.edit(
            {"principal": "@dana:example-org", "name": "Dana X", "account": ["forge=dana7"]}, _ctx(state)
        )
        assert r.ok, r.errors
        person = _reload(state).person("@dana:example-org")
        assert person["name"] == "Dana X"
        assert person["accounts"] == {"forge": "dana7"}
        assert "person_edited" in _kinds(state)

    def test_reassign_changes_lanes_and_role(self, state):
        r = people.reassign({"principal": "@dana:example-org", "lane": ["beta"], "role": "lead"}, _ctx(state))
        assert r.ok, r.errors
        person = _reload(state).person("@dana:example-org")
        assert person["lanes"] == ["beta"] and person["role"] == "lead"
        assert "person_reassigned" in _kinds(state)

    def test_remove_member_who_owns_items_needs_reassign(self, state):
        r = people.remove({"principal": "@dana:example-org"}, _ctx(state))
        assert not r.ok and r.value["refused"] == "conflict"

    def test_remove_with_reassign_moves_items(self, state):
        r = people.remove(
            {"principal": "@dana:example-org", "reassign_to": "@mort:example-org"}, _ctx(state)
        )
        assert r.ok, r.errors
        data = _reload(state)
        assert data.person("@dana:example-org") is None
        assert data.item("i1")["owner"] == "@mort:example-org"
        kinds = _kinds(state)
        assert "person_removed" in kinds and "owner_changed" in kinds


# --- lanes -----------------------------------------------------------------


class TestLanes:
    def test_add_lane(self, state):
        r = lanes.add({"id": "gamma", "name": "Gamma", "lead": "@mort:example-org"}, _ctx(state))
        assert r.ok, r.errors
        assert "gamma" in _reload(state).lane_ids()
        assert "lane_added" in _kinds(state)

    def test_add_duplicate_lane_is_conflict(self, state):
        r = lanes.add({"id": "alpha"}, _ctx(state))
        assert not r.ok and r.value["refused"] == "conflict"

    def test_edit_name_logs_lane_edited(self, state):
        r = lanes.edit({"id": "beta", "name": "Beta Prime"}, _ctx(state))
        assert r.ok, r.errors
        assert _reload(state).lane("beta")["name"] == "Beta Prime"
        assert "lane_edited" in _kinds(state)

    def test_change_lead_logs_lane_owner_changed(self, state):
        r = lanes.edit({"id": "alpha", "lead": "@mort:example-org"}, _ctx(state))
        assert r.ok, r.errors
        assert _reload(state).lane("alpha")["lead"] == "@mort:example-org"
        changelog = cl.read_changelog(_changelog(state))
        transition = [e for e in changelog if e["kind"] == "lane_owner_changed"]
        assert transition and transition[-1]["old"] == "@casey:example-org"
        assert transition[-1]["new"] == "@mort:example-org"
        assert transition[-1]["by"] == DEPUTY  # provenance: who made the change

    def test_remove_lane_that_owns_items_needs_reassign(self, state):
        r = lanes.remove({"id": "alpha"}, _ctx(state))
        assert not r.ok and r.value["refused"] == "conflict"

    def test_remove_lane_with_reassign_relanes_items(self, state):
        r = lanes.remove({"id": "alpha", "reassign_to": "beta"}, _ctx(state))
        assert r.ok, r.errors
        data = _reload(state)
        assert "alpha" not in data.lane_ids()
        assert data.item("i1")["lane"] == "beta"
        # the member whose lane list named alpha no longer references it
        assert "alpha" not in data.person(DEPUTY)["lanes"]
        assert "lane_removed" in _kinds(state)


# --- items -----------------------------------------------------------------


class TestItems:
    def test_add_item(self, state):
        r = items.add(
            {"id": "i2", "label": "Ship", "owner": "@mort:example-org", "lane": "beta", "date": "2026-12-01"},
            _ctx(state),
        )
        assert r.ok, r.errors
        assert _reload(state).item("i2") is not None
        assert "item_added" in _kinds(state)

    def test_add_item_owner_not_a_member_is_bad_request(self, state):
        r = items.add({"id": "i2", "label": "x", "owner": "@ghost:example-org", "date": "2026-12-01"}, _ctx(state))
        assert not r.ok and r.value["refused"] == "bad_request"

    def test_edit_label_logs_item_edited(self, state):
        r = items.edit({"id": "i1", "label": "Build v2"}, _ctx(state))
        assert r.ok, r.errors
        assert _reload(state).item("i1")["label"] == "Build v2"
        assert "item_edited" in _kinds(state)

    def test_edit_status_logs_status_changed(self, state):
        r = items.edit({"id": "i1", "status": "committed"}, _ctx(state))
        assert r.ok, r.errors
        assert "status_changed" in _kinds(state)

    def test_remove_item(self, state):
        r = items.remove({"id": "i1"}, _ctx(state))
        assert r.ok, r.errors
        assert _reload(state).item("i1") is None
        assert "item_removed" in _kinds(state)


class TestProxyAssignee:
    """ADR-166: reassigning to an owner with no account on the tracker system
    records item.assignment (naming the real owner + the proxy), never posting."""

    def test_reassign_to_owner_with_account_clears_assignment(self, state):
        # Casey has a forge account, so no proxy is needed.
        r = items.reassign({"id": "i1", "owner": DEPUTY}, _ctx(state))
        assert r.ok, r.errors
        assert _reload(state).item("i1").get("assignment") is None
        assert "owner_changed" in _kinds(state)

    def test_reassign_to_account_less_owner_records_proxy(self, tmp_path):
        prog = copy.deepcopy(BASE)
        # alpha's lead has a forge account; dana (owner) does not → proxy = lead.
        prog["lanes"][0]["lead"] = "@lead:example-org"
        prog["people"].append(
            {"principal": "@lead:example-org", "lanes": ["alpha"], "accounts": {"forge": "lead1"}}
        )
        state = _seed(tmp_path, prog)
        # i1 is owned by dana (no forge account); reassign to mort (also no
        # account) → the owner stays the named principal and a proxy is computed.
        r = items.reassign({"id": "i1", "owner": "@mort:example-org"}, _ctx(state))
        assert r.ok, r.errors
        assignment = _reload(state).item("i1")["assignment"]
        assert assignment["owner"] == "@mort:example-org"  # the real owner, named regardless
        assert assignment["proxy"] == "@lead:example-org"  # the lane lead, who has an account
        assert assignment["via"] == "lane_lead"


# --- authorization ----------------------------------------------------------


class TestAuthorization:
    def test_deputy_may_edit(self, state):
        assert lanes.add({"id": "g", "name": "G"}, _ctx(state, principal=DEPUTY)).ok

    def test_maintainer_may_edit(self, state):
        assert lanes.add({"id": "g", "name": "G"}, _ctx(state, principal="@mort:example-org")).ok

    def test_open_posture_operator_may_edit(self, state):
        # unproven local operator on an open-posture node — the solo/dev case
        assert lanes.add({"id": "g", "name": "G"}, _ctx(state, principal="@nobody:local")).ok

    def test_strict_posture_non_deputy_is_forbidden(self, state, monkeypatch):
        monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "sso")
        r = lanes.add({"id": "g", "name": "G"}, _ctx(state, principal="@stranger:example-org", assured=True))
        assert not r.ok and r.value["refused"] == "forbidden"

    def test_strict_posture_deputy_still_allowed(self, state, monkeypatch):
        monkeypatch.setenv("AXIOM_IDENTITY_POSTURE", "sso")
        r = lanes.add({"id": "g", "name": "G"}, _ctx(state, principal=DEPUTY, assured=True))
        assert r.ok, r.errors


# --- idempotency against sync ----------------------------------------------


class TestNoDoubleLog:
    def test_a_mutation_advances_the_snapshot_so_sync_does_not_relog(self, state):
        before = len(cl.read_changelog(_changelog(state)))
        lanes.add({"id": "gamma", "name": "Gamma"}, _ctx(state))
        after_edit = cl.read_changelog(_changelog(state))
        assert len(after_edit) > before  # the edit logged
        # a self-reconcile now sees the snapshot the mutation already advanced
        result = sync_skill.run({}, _ctx(state))
        assert result.ok, result.errors
        assert result.value["count"] == 0  # nothing re-logged
        assert len(cl.read_changelog(_changelog(state))) == len(after_edit)
