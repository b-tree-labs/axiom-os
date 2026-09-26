# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.extensions.builtins.authz.decide.decide()`.

The single decision API. Test coverage:

- Pure decision logic (no DB) — capability, rule engine, novel-action.
- Receipt write end-to-end against Postgres (session_for('authz')) —
  integration; skips when Postgres isn't reachable.
- The no-bypass property: every code path through decide() produces a
  Verdict whose receipt_fragment_id is non-empty.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from axiom.extensions.builtins.authz.decide import (
    DecideContext,
    decide,
)
from axiom.extensions.builtins.authz.rules import Rule
from axiom.governance import (
    ActionEnvelope,
    ActionIntent,
    CapabilityToken,
    Classification,
    Decision,
    IntentPattern,
    NextAction,
    ProvenanceRef,
    ResourcePattern,
    ResourceRef,
)
from axiom.vega.identity.principal import Principal


def _alice() -> Principal:
    return Principal(handle="@alice:test", public_bytes=b"\x00" * 32)


def _env(
    capability: CapabilityToken | None = None,
    intent: str = "notification.send",
    resource: str = "slack://team-rsc/#alerts",
    classification: Classification = Classification.INTERNAL,
    federation_origin: str | None = None,
    dedup_key: str = "test-dedup",
) -> ActionEnvelope:
    alice = _alice()
    return ActionEnvelope(
        actor=alice,
        capability=capability
        if capability is not None
        else CapabilityToken.unscoped_test_token(subject=alice),
        classification=classification,
        context_fragment_id="memory://localhost/test",
        provenance_parent=ProvenanceRef.synthetic("test"),
        federation_origin=federation_origin,
        intent=ActionIntent(intent),
        resource=ResourceRef.parse(resource),
        deadline=None,
        dedup_key=dedup_key,
    )


# ---------------------------------------------------------------------------
# Pure-logic tests (no DB) — fast, deterministic.
# ---------------------------------------------------------------------------


class TestDecidePureLogic:
    def test_novel_action_returns_propose(self):
        """Per §5.3: novel action class returns PROPOSE_TO_HUMAN by default."""
        ctx = DecideContext()
        verdict = decide(_env(), ctx)
        assert verdict.decision is Decision.PROPOSE_TO_HUMAN
        assert verdict.next_action_for_caller is NextAction.ENQUEUE_PROPOSAL
        assert verdict.receipt_fragment_id  # never empty

    def test_explicit_permit_rule(self):
        ctx = DecideContext()
        ctx.add_rule(Rule(
            name="allow_notification",
            intent_pattern=IntentPattern("notification.send"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("slack://*"),
            disposition="permit",
        ))
        verdict = decide(_env(), ctx)
        assert verdict.decision is Decision.PERMIT
        assert verdict.next_action_for_caller is NextAction.PROCEED

    def test_explicit_deny_rule(self):
        ctx = DecideContext()
        ctx.add_rule(Rule(
            name="block_slack_sends",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("slack://*"),
            disposition="deny",
        ))
        verdict = decide(_env(), ctx)
        assert verdict.decision is Decision.DENY
        assert verdict.next_action_for_caller is NextAction.ABORT

    def test_deny_beats_permit_in_same_context(self):
        ctx = DecideContext()
        ctx.add_rule(Rule(
            name="allow",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("*"),
            disposition="permit",
        ))
        ctx.add_rule(Rule(
            name="deny_slack",
            intent_pattern=IntentPattern("*"),
            actor_pattern="*",
            resource_pattern=ResourcePattern("slack://*"),
            disposition="deny",
        ))
        verdict = decide(_env(), ctx)
        assert verdict.decision is Decision.DENY

    def test_expired_capability_yields_expired_decision(self):
        alice = _alice()
        # Build an explicitly-expired token.
        now = datetime.now(timezone.utc)
        expired = CapabilityToken(
            id="expired-token",
            issuer=alice,
            subject=alice,
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.CONTROLLED,
            not_before=now - timedelta(hours=2),
            not_after=now - timedelta(minutes=1),
            delegation_depth=0,
            parent_capability=None,
            signature=b"\xff" * 64,
        )
        ctx = DecideContext()
        verdict = decide(_env(capability=expired), ctx)
        assert verdict.decision is Decision.EXPIRED_CAPABILITY
        assert verdict.next_action_for_caller is NextAction.ABORT

    def test_capability_intent_scope_enforced(self):
        alice = _alice()
        narrow = CapabilityToken(
            id="narrow",
            issuer=alice,
            subject=alice,
            intent_pattern=IntentPattern("vault.read_secret"),  # not notification.send
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.CONTROLLED,
            not_before=datetime.now(timezone.utc) - timedelta(seconds=1),
            not_after=datetime.now(timezone.utc) + timedelta(hours=1),
            delegation_depth=0,
            parent_capability=None,
            signature=b"\xff" * 64,
        )
        ctx = DecideContext()
        verdict = decide(_env(capability=narrow), ctx)
        assert verdict.decision is Decision.DENY
        assert "intent" in verdict.reason.lower()

    def test_classification_ceiling_enforced(self):
        alice = _alice()
        capped = CapabilityToken(
            id="capped",
            issuer=alice,
            subject=alice,
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
            not_before=datetime.now(timezone.utc) - timedelta(seconds=1),
            not_after=datetime.now(timezone.utc) + timedelta(hours=1),
            delegation_depth=0,
            parent_capability=None,
            signature=b"\xff" * 64,
        )
        ctx = DecideContext()
        verdict = decide(
            _env(capability=capped, classification=Classification.REGULATED),
            ctx,
        )
        assert verdict.decision is Decision.DENY


class TestNoBypassProperty:
    """Per spec §9.1: every code path through decide() produces a Verdict."""

    @pytest.mark.parametrize(
        "intent,resource,classification",
        [
            ("notification.send", "slack://team/#alerts", Classification.PUBLIC),
            ("vault.issue_capability", "extension://vault", Classification.INTERNAL),
            ("schedule.fire", "extension://schedule", Classification.REGULATED),
            ("federation.share_fragment", "axiom://peer/frag", Classification.CONTROLLED),
            ("extension.invoke_tool", "extension://expman", Classification.INTERNAL),
        ],
    )
    def test_every_envelope_returns_a_verdict_with_receipt(
        self, intent, resource, classification
    ):
        ctx = DecideContext()
        env = _env(intent=intent, resource=resource, classification=classification)
        verdict = decide(env, ctx)
        assert verdict.receipt_fragment_id  # never empty
        # Decision must be a real Decision enum value (no silent bypass).
        assert verdict.decision in {
            Decision.PERMIT,
            Decision.DENY,
            Decision.PROPOSE_TO_HUMAN,
            Decision.RATE_LIMIT,
            Decision.EXPIRED_CAPABILITY,
        }


# ---------------------------------------------------------------------------
# Integration tests — actually write receipts to Postgres via
# session_for('authz'). Skip if PG unreachable.
# ---------------------------------------------------------------------------


def _pg_available() -> bool:
    try:
        import psycopg2  # type: ignore

        url = os.environ.get(
            "AXIOM_DB_URL", "postgresql://axiom:axiom@localhost:5432/axiom_db"
        )
        conn = psycopg2.connect(url, connect_timeout=2)
        conn.close()
        return True
    except Exception:
        return False


pg_only = pytest.mark.skipif(not _pg_available(), reason="Postgres not reachable")


@pg_only
class TestDecideReceiptPersistence:
    """End-to-end: decide() writes a row to authz.verdicts via session_for."""

    @pytest.fixture(autouse=True)
    def _create_schema_and_tables(self):
        # Create schema + tables ahead of test; tear down after.
        from sqlalchemy import text

        from axiom.extensions.builtins.authz.db_models import Base
        from axiom.infra.db import ensure_schema, get_engine, session_for

        engine = get_engine()
        ensure_schema(engine, "authz")
        # Set search_path then create_all
        with engine.begin() as conn:
            conn.execute(text('SET search_path TO "authz", public'))
            Base.metadata.create_all(conn)
        yield
        # Cleanup: truncate, not drop, so other tests can re-use the schema.
        with session_for("authz") as s:
            for tbl in ("verdicts", "policies", "graduation"):
                s.execute(text(f"TRUNCATE TABLE {tbl} CASCADE"))
            s.commit()

    def test_decide_writes_receipt_row(self):
        from sqlalchemy import text

        from axiom.infra.db import session_for

        def _factory():
            return session_for("authz")

        ctx = DecideContext(session_factory=_factory)
        verdict = decide(_env(dedup_key="receipt-test-1"), ctx)
        # The receipt row should be findable by id.
        with session_for("authz") as s:
            row_count = s.execute(
                text(
                    "SELECT count(*) FROM verdicts WHERE id = :id"
                ),
                {"id": verdict.receipt_fragment_id},
            ).scalar()
            assert row_count == 1

    def test_decide_records_envelope_fields_verbatim(self):
        from sqlalchemy import text

        from axiom.infra.db import session_for

        def _factory():
            return session_for("authz")

        ctx = DecideContext(session_factory=_factory)
        env = _env(dedup_key="verbatim-test-1", intent="notification.deliver")
        decide(env, ctx)
        with session_for("authz") as s:
            row = s.execute(
                text(
                    "SELECT actor, intent, resource, classification, dedup_key "
                    "FROM verdicts WHERE dedup_key = :dk"
                ),
                {"dk": "verbatim-test-1"},
            ).first()
            assert row is not None
            assert row.actor == "@alice:test"
            assert row.intent == "notification.deliver"
            assert row.classification == "internal"
            assert row.dedup_key == "verbatim-test-1"

    def test_graduation_lookup_when_table_empty_returns_propose(self):
        from axiom.infra.db import session_for

        def _factory():
            return session_for("authz")

        ctx = DecideContext(session_factory=_factory)
        verdict = decide(_env(dedup_key="grad-empty-test"), ctx)
        # No graduation row, no rule → propose.
        assert verdict.decision is Decision.PROPOSE_TO_HUMAN
