# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.changes`` — what changed since this principal last looked.

Two things are proven here. The per-consumer watermark: two principals each
get only their own unseen deltas, independent of one another. And the
read-vs-advance rule: reporting is a read; advancing is a write that is the
default only on the operator's CLI surface, peek by default when served, and
always explicit otherwise — so the projected tool can stay a read.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.program.skills import changes, sync
from axiom.infra.skills import SkillContext, SkillRegistry


def _ctx(state, surface):
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.changes"),
        user_prompt=None,
        surface=surface,
    )


@pytest.fixture
def synced(tmp_path, data_dict):
    """A node whose change log has been populated by one baseline sync."""
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    sync.run({}, _ctx(state, "cli"))
    return state


ALICE = "@alice:example-org"
BOBBY = "@bobby:example-org"


class TestWindow:
    def test_a_fresh_principal_sees_the_whole_log(self, synced):
        result = changes.run({"principal": ALICE, "peek": True}, _ctx(synced, "cli"))
        assert result.ok
        assert result.value["count"] == 9  # 4 added + 2 lanes + 3 drift
        assert result.value["principal"] == ALICE

    def test_deltas_carry_the_status_link_field_shape(self, synced):
        result = changes.run({"principal": ALICE, "peek": True}, _ctx(synced, "cli"))
        item_changes = [c for c in result.value["changes"] if c["subject_kind"] == "item"]
        one = next(c for c in item_changes if c["subject"] == "i-one")
        assert one["item"] is not None
        # same shape status serves: owner / dates / status / pct / links
        for field in ("owner", "dates", "status", "pct", "links"):
            assert field in one["item"]
        assert any(link["kind"] == "tracker" for link in one["item"]["links"])


class TestPerConsumerWatermark:
    def test_each_principal_gets_only_their_own_unseen_deltas(self, synced):
        ctx = _ctx(synced, "cli")
        # alice advances (default on cli) → she is now caught up
        first = changes.run({"principal": ALICE}, ctx)
        assert first.value["advanced"] is True
        assert first.value["count"] == 9

        # a NEW change lands after alice looked
        data_path = Path(synced) / "program" / "data.json"
        doc = json.loads(data_path.read_text(encoding="utf-8"))
        doc["schedule"][0]["status"] = "committed"
        data_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        sync.run({}, ctx)

        # alice now sees only the one new delta
        alice_again = changes.run({"principal": ALICE, "peek": True}, ctx)
        assert alice_again.value["count"] == 1
        assert alice_again.value["changes"][0]["kind"] == "status_changed"

        # bobby, who never looked, still sees everything (9 + 1)
        bobby = changes.run({"principal": BOBBY, "peek": True}, ctx)
        assert bobby.value["count"] == 10


class TestReadVsAdvance:
    def test_peek_does_not_advance(self, synced):
        ctx = _ctx(synced, "cli")
        changes.run({"principal": ALICE, "peek": True}, ctx)
        # still sees everything — the watermark never moved
        assert changes.run({"principal": ALICE, "peek": True}, ctx).value["count"] == 9

    def test_cli_default_advances(self, synced):
        ctx = _ctx(synced, "cli")
        first = changes.run({"principal": ALICE}, ctx)
        assert first.value["advanced"] is True
        # a second default read now sees nothing
        assert changes.run({"principal": ALICE, "peek": True}, ctx).value["count"] == 0

    def test_served_surface_default_is_peek(self, synced):
        """On a served surface (mcp) the default must NOT advance — so the
        projected tool is a read. Two served reads both see everything."""
        ctx = _ctx(synced, "mcp")
        first = changes.run({"principal": ALICE}, ctx)
        assert first.value["advanced"] is False
        second = changes.run({"principal": ALICE}, ctx)
        assert first.value["count"] == second.value["count"] == 9

    def test_served_surface_advances_only_on_explicit_request(self, synced):
        ctx = _ctx(synced, "mcp")
        advanced = changes.run({"principal": ALICE, "advance": True}, ctx)
        assert advanced.value["advanced"] is True
        # now caught up
        assert changes.run({"principal": ALICE}, ctx).value["count"] == 0

    def test_peek_wins_over_advance(self, synced):
        ctx = _ctx(synced, "cli")
        result = changes.run({"principal": ALICE, "peek": True, "advance": True}, ctx)
        assert result.value["advanced"] is False
        assert changes.run({"principal": ALICE, "peek": True}, ctx).value["count"] == 9


class TestSince:
    def test_since_an_iso_timestamp_bounds_the_window(self, synced):
        ctx = _ctx(synced, "cli")
        # everything was logged "now"; a far-future since yields nothing
        future = changes.run(
            {"principal": ALICE, "since": "2999-01-01T00:00:00Z", "peek": True}, ctx
        )
        assert future.value["count"] == 0
        # a far-past since yields everything, ignoring the (empty) watermark
        past = changes.run({"principal": ALICE, "since": "2000-01-01T00:00:00Z", "peek": True}, ctx)
        assert past.value["count"] == 9
        assert past.value["since"]["mode"] == "explicit"

    def test_a_bad_since_is_a_typed_refusal(self, synced):
        result = changes.run({"principal": ALICE, "since": "not-a-date"}, _ctx(synced, "cli"))
        assert not result.ok
        assert result.value["refused"] == "bad_request"


class TestPrincipalDefaulting:
    def test_a_bad_principal_is_refused(self, synced):
        result = changes.run({"principal": "not-a-principal"}, _ctx(synced, "cli"))
        assert not result.ok
        assert result.value["refused"] == "bad_request"

    def test_principal_defaults_to_the_actor(self, synced):
        # No principal param → the skill uses ctx.principal.handle; the read
        # still succeeds and names whichever principal that resolved to.
        result = changes.run({"peek": True}, _ctx(synced, "cli"))
        assert result.ok
        assert result.value["principal"].startswith("@")


class TestNoLogYet:
    def test_changes_without_a_log_is_empty_not_an_error(self, tmp_path):
        ctx = _ctx(tmp_path / "state", "cli")
        result = changes.run({"principal": ALICE, "peek": True}, ctx)
        assert result.ok
        assert result.value["count"] == 0
        assert result.value["advanced"] is False
