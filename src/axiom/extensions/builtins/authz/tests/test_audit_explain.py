# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``audit.explain`` is load-bearing per PRD §5.4: it must cover every
``Verdict.decision`` class. These tests exercise each one against an
in-memory SQLite fixture wired with rule + graduation rows that
reproduce the original decision conditions."""

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
    Policy,
    Verdict,
)
from axiom.extensions.builtins.authz.skills import explain as explain_mod
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillRegistry


def _mk_verdict(**overrides) -> Verdict:
    defaults = dict(
        id="v",
        decided_at=datetime.now(UTC) - timedelta(hours=1),
        actor="@alice:test",
        intent="notification.send.email",
        resource="channel://email/alice@x",
        classification="internal",
        capability_id="cap",
        context_fragment_id="ctx",
        provenance_parent="root",
        federation_origin=None,
        dedup_key="dk",
        decision="permit",
        reason="seeded",
        matched_rules=None,
    )
    defaults.update(overrides)
    return Verdict(**defaults)


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

    return _cm


@pytest.fixture()
def ctx():
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("test.audit.explain"),
        user_prompt=None,
    )


class TestExplainCoversEveryDecision:
    """PRD §5.4 Acceptance: ``explain`` covers every Verdict.decision."""

    def test_permit_with_matching_rule(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-permit",
                decision="permit",
                matched_rules=["R-permit"],
                reason="rule R-permit matched",
            ))
            s.add(Policy(
                id="p-1", name="R-permit",
                intent_pattern="notification.send.*",
                actor_pattern="@alice:test",
                resource_pattern="channel://email/*",
                classification="internal",
                federation_origin_pattern=None,
                disposition="permit", priority=5,
            ))
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-permit", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        assert r.value["trace"]["winning_rule"] == "R-permit"
        assert "permit" in r.value["narrative"]
        assert "R-permit" in r.value["narrative"]

    def test_deny_precedence_over_permit(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-deny",
                decision="deny",
                matched_rules=["R-deny", "R-permit"],
                reason="deny beats permit",
            ))
            s.add_all([
                Policy(id="p-d", name="R-deny",
                       intent_pattern="notification.send.*",
                       actor_pattern="@alice:test",
                       resource_pattern="channel://email/*",
                       classification="internal",
                       federation_origin_pattern=None,
                       disposition="deny", priority=1),
                Policy(id="p-p", name="R-permit",
                       intent_pattern="notification.send.*",
                       actor_pattern="@alice:test",
                       resource_pattern="channel://email/*",
                       classification="internal",
                       federation_origin_pattern=None,
                       disposition="permit", priority=10),
            ])
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-deny", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        # deny wins regardless of priority.
        assert r.value["trace"]["winning_rule"] == "R-deny"
        assert "deny" in r.value["narrative"].lower()

    def test_propose_to_human_with_graduation_in_progress(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-prop",
                decision="propose_to_human",
                matched_rules=None,
                reason="novel action, awaiting graduation",
            ))
            s.add(Graduation(
                id="g-1", actor="@alice:test",
                intent_class="notification.send",
                resource_pattern="channel://email/*",
                approvals=2, threshold=5, graduated=False,
                last_update=datetime.now(UTC),
            ))
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-prop", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        assert r.value["trace"]["graduation"]["approvals"] == 2
        assert "2/5" in r.value["narrative"] or "2" in r.value["narrative"]
        assert "propose" in r.value["narrative"].lower()

    def test_propose_when_no_graduation_row_exists(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-prop2",
                decision="propose_to_human",
                matched_rules=None,
                reason="brand-new action class",
            ))
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-prop2", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        assert r.value["trace"]["graduation"] is None
        assert "novel" in r.value["narrative"].lower()

    def test_permit_via_graduation_no_rule(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-grad-permit",
                decision="permit",
                matched_rules=None,
                reason="graduated to autonomous",
            ))
            s.add(Graduation(
                id="g-2", actor="@alice:test",
                intent_class="notification.send",
                resource_pattern="channel://email/*",
                approvals=5, threshold=5, graduated=True,
                last_update=datetime.now(UTC),
            ))
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-grad-permit", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        assert r.value["trace"]["graduation"]["graduated"] is True
        assert "graduated" in r.value["narrative"].lower() or \
               "autonomous" in r.value["narrative"].lower()

    def test_rate_limit_narrative(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-rl",
                decision="rate_limit",
                matched_rules=None,
                reason="capability rate-limit exceeded",
            ))
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-rl", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        assert "rate" in r.value["narrative"].lower()

    def test_expired_capability_narrative(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-exp",
                decision="expired_capability",
                matched_rules=None,
                capability_id="cap-expired",
                reason="capability TTL expired",
            ))
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-exp", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        assert "expired" in r.value["narrative"].lower() or \
               "ttl" in r.value["narrative"].lower()
        assert "cap-expired" in r.value["narrative"]

    def test_deny_no_rule_fallback(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-deny-fallback",
                decision="deny",
                matched_rules=None,
                reason="fail-closed default",
            ))
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-deny-fallback", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        assert "fail-closed" in r.value["narrative"].lower() or \
               "deny" in r.value["narrative"].lower()

    def test_federation_origin_surfaced(self, session_cm, ctx):
        with session_cm() as s:
            s.add(_mk_verdict(
                id="v-fed",
                decision="permit",
                matched_rules=["R-fed"],
                federation_origin="cohort-y",
                reason="peer-forwarded action",
            ))
            s.add(Policy(
                id="p-fed", name="R-fed",
                intent_pattern="notification.send.*",
                actor_pattern="@alice:test",
                resource_pattern="channel://email/*",
                classification="internal",
                federation_origin_pattern="cohort-y",
                disposition="permit", priority=5,
            ))
            s.commit()

        r = explain_mod.run(
            {"receipt_id": "v-fed", "_session_cm": session_cm}, ctx
        )
        assert r.ok
        assert r.value["trace"]["federation_origin"] == "cohort-y"
        assert "cohort-y" in r.value["narrative"]

    def test_missing_verdict_clean_error(self, session_cm, ctx):
        r = explain_mod.run(
            {"receipt_id": "does-not-exist", "_session_cm": session_cm}, ctx
        )
        assert not r.ok
        assert any("no verdict" in e for e in r.errors)

    def test_empty_id_rejected(self, session_cm, ctx):
        r = explain_mod.run({"_session_cm": session_cm}, ctx)
        assert not r.ok
        assert any("receipt_id is required" in e for e in r.errors)
