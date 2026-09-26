# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skill tests for the AUTHZ-2 verbs (chain / causes / graduation).

Same in-memory SQLite fixture pattern as ``test_audit_skills.py``;
the seed graph wires up provenance parents and graduation rows so we
can exercise the walkers + filter combinatorics without Postgres.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from axiom.extensions.builtins.authz.db_models import (
    Base,
    Graduation,
    Verdict,
)
from axiom.extensions.builtins.authz.skills import (
    causes_verdicts,
    chain_verdicts,
    graduation as graduation_mod,
)
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture()
def session_cm():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, future=True, expire_on_commit=False)

    @contextlib.contextmanager
    def _cm():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    now = datetime.now(UTC)

    # Provenance graph:
    #   ext-root  ← parent of v-1
    #   v-1       ← parent of v-2 + v-3
    #   v-2       ← leaf
    #   v-3       ← parent of v-4
    #   v-4       ← leaf
    with _cm() as s:
        s.add_all([
            Verdict(
                id="v-1", decided_at=now - timedelta(hours=3),
                actor="@alice:test", intent="data.ingest.box",
                resource="connector://box-corpus", classification="internal",
                capability_id="cap-1", context_fragment_id="ctx-1",
                provenance_parent="ext-root", federation_origin=None,
                dedup_key="dk-1", decision="permit",
                reason="rule R-1", matched_rules=["R-1"],
            ),
            Verdict(
                id="v-2", decided_at=now - timedelta(hours=2),
                actor="@alice:test", intent="notification.send.email",
                resource="channel://email/alice@x", classification="internal",
                capability_id="cap-2", context_fragment_id="ctx-2",
                provenance_parent="v-1", federation_origin=None,
                dedup_key="dk-2", decision="permit",
                reason="downstream of ingest", matched_rules=["R-2"],
            ),
            Verdict(
                id="v-3", decided_at=now - timedelta(hours=2),
                actor="@alice:test", intent="rag.retrieve",
                resource="rag://corpus/box", classification="internal",
                capability_id="cap-3", context_fragment_id="ctx-3",
                provenance_parent="v-1", federation_origin=None,
                dedup_key="dk-3", decision="permit",
                reason="downstream of ingest", matched_rules=["R-3"],
            ),
            Verdict(
                id="v-4", decided_at=now - timedelta(hours=1),
                actor="@bob:test", intent="notification.send.slack",
                resource="channel://slack/team/#alerts",
                classification="internal", capability_id="cap-4",
                context_fragment_id="ctx-4", provenance_parent="v-3",
                federation_origin=None, dedup_key="dk-4",
                decision="deny", reason="lacks role", matched_rules=["R-4"],
            ),
        ])
        s.add_all([
            Graduation(
                id="g-1", actor="@alice:test",
                intent_class="data.ingest",
                resource_pattern="connector://*",
                approvals=5, threshold=5, graduated=True,
                last_update=now,
            ),
            Graduation(
                id="g-2", actor="@alice:test",
                intent_class="notification.send",
                resource_pattern="channel://email/*",
                approvals=2, threshold=5, graduated=False,
                last_update=now,
            ),
            Graduation(
                id="g-3", actor="@bob:test",
                intent_class="notification.send",
                resource_pattern="channel://slack/*",
                approvals=0, threshold=5, graduated=False,
                last_update=now,
            ),
        ])
        s.commit()

    return _cm


@pytest.fixture()
def ctx():
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("test.audit.walk"),
        user_prompt=None,
    )


# ---------------------------------------------------------------------------
# chain
# ---------------------------------------------------------------------------


class TestChainVerdicts:
    def test_walks_up_through_intermediate(self, session_cm, ctx):
        # v-4 → v-3 → v-1 → ext-root
        result = chain_verdicts.run(
            {"receipt_id": "v-4", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        ids = [r["id"] for r in result.value["items"]]
        assert ids == ["v-4", "v-3", "v-1", "ext-root"]
        # Last entry is the synthetic external-root marker.
        assert result.value["items"][-1]["kind"] == "external_root"

    def test_root_only_when_starting_at_v1(self, session_cm, ctx):
        result = chain_verdicts.run(
            {"receipt_id": "v-1", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        ids = [r["id"] for r in result.value["items"]]
        assert ids == ["v-1", "ext-root"]

    def test_missing_id_clean_error(self, session_cm, ctx):
        result = chain_verdicts.run(
            {"receipt_id": "no-such-id", "_session_cm": session_cm}, ctx
        )
        # The first iteration sees no verdict row → appends external_root,
        # breaks. So chain has 1 item; treated as ok=True with depth=1.
        # Caller can tell this is bogus because kind=external_root and
        # depth=1.
        assert result.ok
        assert result.value["depth"] == 1
        assert result.value["items"][0]["kind"] == "external_root"

    def test_empty_id_rejected(self, session_cm, ctx):
        result = chain_verdicts.run({"_session_cm": session_cm}, ctx)
        assert not result.ok
        assert any("receipt_id is required" in e for e in result.errors)


# ---------------------------------------------------------------------------
# causes
# ---------------------------------------------------------------------------


class TestCausesVerdicts:
    def test_returns_direct_downstream(self, session_cm, ctx):
        # v-1's direct causes are v-2 and v-3 (not v-4 — that's two hops down).
        result = causes_verdicts.run(
            {"fragment_id": "v-1", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"v-2", "v-3"}

    def test_no_downstream_returns_empty(self, session_cm, ctx):
        # v-2 has no children.
        result = causes_verdicts.run(
            {"fragment_id": "v-2", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        assert result.value["count"] == 0

    def test_external_fragment_finds_v1(self, session_cm, ctx):
        # ext-root is v-1's parent.
        result = causes_verdicts.run(
            {"fragment_id": "ext-root", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"v-1"}

    def test_empty_id_rejected(self, session_cm, ctx):
        result = causes_verdicts.run({"_session_cm": session_cm}, ctx)
        assert not result.ok

    def test_bad_limit_rejected(self, session_cm, ctx):
        result = causes_verdicts.run(
            {"fragment_id": "v-1", "limit": 0, "_session_cm": session_cm}, ctx
        )
        assert not result.ok


# ---------------------------------------------------------------------------
# graduation
# ---------------------------------------------------------------------------


class TestGraduation:
    def test_lists_all_when_unfiltered(self, session_cm, ctx):
        result = graduation_mod.run({"_session_cm": session_cm}, ctx)
        assert result.ok
        assert result.value["count"] == 3

    def test_actor_filter(self, session_cm, ctx):
        result = graduation_mod.run(
            {"actor": "@alice:test", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"g-1", "g-2"}

    def test_intent_class_filter(self, session_cm, ctx):
        result = graduation_mod.run(
            {"intent_class": "notification.send", "_session_cm": session_cm},
            ctx,
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"g-2", "g-3"}

    def test_only_graduated(self, session_cm, ctx):
        result = graduation_mod.run(
            {"only_graduated": True, "_session_cm": session_cm}, ctx
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"g-1"}

    def test_only_proposing(self, session_cm, ctx):
        result = graduation_mod.run(
            {"only_proposing": True, "_session_cm": session_cm}, ctx
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"g-2", "g-3"}

    def test_only_graduated_and_only_proposing_mutually_exclusive(
        self, session_cm, ctx
    ):
        result = graduation_mod.run(
            {"only_graduated": True, "only_proposing": True,
             "_session_cm": session_cm},
            ctx,
        )
        assert not result.ok
        assert any("mutually exclusive" in e for e in result.errors)
