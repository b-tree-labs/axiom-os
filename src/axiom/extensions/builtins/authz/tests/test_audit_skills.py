# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skill tests for ``audit.list`` + ``audit.show`` with an in-memory
SQLite session so they run without Postgres."""

from __future__ import annotations

import contextlib
import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from axiom.extensions.builtins.authz.db_models import Base, Verdict
from axiom.extensions.builtins.authz.skills import list_verdicts, show_verdict
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillRegistry


# ---------------------------------------------------------------------------
# In-memory session fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def session_cm():
    """Return a `() -> contextmanager[Session]` against in-memory SQLite.

    The skill consumes a context-manager factory through
    ``params['_session_cm']`` so we can swap the real ``session_for('authz')``
    for sqlite without monkey-patching the import. Note: the Verdict.matched_rules
    JSON column round-trips natively on sqlite via SQLAlchemy.
    """
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

    # Seed a handful of verdicts.
    now = datetime.now(UTC)
    with _cm() as s:
        s.add_all([
            Verdict(
                id="v-1",
                decided_at=now - timedelta(hours=1),
                actor="@alice:test",
                intent="notification.send.email",
                resource="channel://email/alice@x",
                classification="internal",
                capability_id="cap-1",
                context_fragment_id="ctx-1",
                provenance_parent="prov-1",
                federation_origin=None,
                dedup_key="dk-1",
                decision="permit",
                reason="explicit rule R-1 matched",
                matched_rules=["R-1"],
            ),
            Verdict(
                id="v-2",
                decided_at=now - timedelta(days=2),
                actor="@bob:test",
                intent="notification.send.slack",
                resource="channel://slack/team-x/#alerts",
                classification="internal",
                capability_id="cap-2",
                context_fragment_id="ctx-2",
                provenance_parent="prov-2",
                federation_origin=None,
                dedup_key="dk-2",
                decision="deny",
                reason="actor lacks role",
                matched_rules=["R-2"],
            ),
            Verdict(
                id="v-3",
                decided_at=now - timedelta(days=10),
                actor="@alice:test",
                intent="data.ingest.box",
                resource="connector://box-corpus",
                classification="internal",
                capability_id="cap-3",
                context_fragment_id="ctx-3",
                provenance_parent="prov-3",
                federation_origin=None,
                dedup_key="dk-3",
                decision="permit",
                reason="graduated",
                matched_rules=[],
            ),
            Verdict(
                id="v-4",
                decided_at=now - timedelta(hours=2),
                actor="@peer:cohort-y",
                intent="federation.sample.share",
                resource="axiom://cohort-y/sample/SR-007",
                classification="internal",
                capability_id="cap-4",
                context_fragment_id="ctx-4",
                provenance_parent="prov-4",
                federation_origin="cohort-y",
                dedup_key="dk-4",
                decision="propose_to_human",
                reason="novel federation action",
                matched_rules=[],
            ),
        ])
        s.commit()

    return _cm


@pytest.fixture()
def ctx():
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("test.audit"),
        user_prompt=None,
    )


# ---------------------------------------------------------------------------
# audit.list
# ---------------------------------------------------------------------------


class TestListVerdicts:
    def test_returns_recent_first_within_default_window(self, session_cm, ctx):
        result = list_verdicts.run(
            {"since": "7d", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        items = result.value["items"]
        # v-3 is 10 days old, must be excluded by the 7d window.
        ids = [r["id"] for r in items]
        assert "v-3" not in ids
        # Newest first.
        assert ids[0] == "v-1"

    def test_primitive_prefix_match(self, session_cm, ctx):
        result = list_verdicts.run(
            {"since": "30d", "primitive": "notification", "_session_cm": session_cm},
            ctx,
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"v-1", "v-2"}

    def test_actor_filter(self, session_cm, ctx):
        result = list_verdicts.run(
            {"since": "30d", "actor": "@alice:test", "_session_cm": session_cm},
            ctx,
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"v-1", "v-3"}

    def test_decision_filter(self, session_cm, ctx):
        result = list_verdicts.run(
            {"since": "30d", "decision": "deny", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"v-2"}

    def test_federation_origin_filter(self, session_cm, ctx):
        result = list_verdicts.run(
            {"since": "30d", "federation_origin": "cohort-y",
             "_session_cm": session_cm},
            ctx,
        )
        assert result.ok
        ids = {r["id"] for r in result.value["items"]}
        assert ids == {"v-4"}

    def test_invalid_since_returns_clean_error(self, session_cm, ctx):
        result = list_verdicts.run(
            {"since": "garbage", "_session_cm": session_cm}, ctx
        )
        assert not result.ok
        assert any("--since" in e for e in result.errors)

    def test_invalid_limit_rejected(self, session_cm, ctx):
        result = list_verdicts.run(
            {"since": "7d", "limit": 0, "_session_cm": session_cm}, ctx
        )
        assert not result.ok
        assert any("limit" in e.lower() for e in result.errors)

    def test_empty_result_is_ok(self, session_cm, ctx):
        result = list_verdicts.run(
            {"since": "7d", "actor": "@nobody:nowhere",
             "_session_cm": session_cm},
            ctx,
        )
        assert result.ok
        assert result.value["count"] == 0
        assert result.value["items"] == []


# ---------------------------------------------------------------------------
# audit.show
# ---------------------------------------------------------------------------


class TestShowVerdict:
    def test_returns_full_record_with_audit_columns(self, session_cm, ctx):
        result = show_verdict.run(
            {"receipt_id": "v-1", "_session_cm": session_cm}, ctx
        )
        assert result.ok
        item = result.value["item"]
        # The list view's columns…
        assert item["actor"] == "@alice:test"
        assert item["decision"] == "permit"
        # …plus the audit-extra columns ``show`` adds.
        assert item["capability_id"] == "cap-1"
        assert item["provenance_parent"] == "prov-1"
        assert item["matched_rules"] == ["R-1"]

    def test_missing_id_is_clean_error(self, session_cm, ctx):
        result = show_verdict.run(
            {"receipt_id": "does-not-exist", "_session_cm": session_cm}, ctx
        )
        assert not result.ok
        assert any("no verdict found" in e for e in result.errors)

    def test_empty_id_is_clean_error(self, session_cm, ctx):
        result = show_verdict.run({"_session_cm": session_cm}, ctx)
        assert not result.ok
        assert any("receipt_id is required" in e for e in result.errors)
