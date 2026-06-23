# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for capability lifecycle — issuance, retrieval, revocation."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from axiom.extensions.builtins.vault.capability_store import (
    VaultContext,
    get_capability_by_id,
    is_revoked,
    issue_capability,
    revoke_capability,
)
from axiom.governance import (
    ActionIntent,
    Classification,
    IntentPattern,
    ResourcePattern,
)
from axiom.vega.identity.principal import Principal


def _alice() -> Principal:
    return Principal(handle="@alice:test", public_bytes=b"\x00" * 32)


class TestInMemoryIssuance:
    """Capability lifecycle without persistence (session_factory=None)."""

    def test_issue_returns_valid_token(self):
        ctx = VaultContext()
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("notification.send"),
            resource_pattern=ResourcePattern("slack://*"),
            classification_ceiling=Classification.INTERNAL,
        )
        assert cap.subject == _alice()
        assert cap.is_valid_at(datetime.now(timezone.utc))
        assert cap.permits_intent(ActionIntent("notification.send"))

    def test_issue_cached(self):
        ctx = VaultContext()
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
        )
        cached = get_capability_by_id(ctx, cap.id)
        assert cached is cap

    def test_revoke_invalidates_cache(self):
        ctx = VaultContext()
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
        )
        revoke_capability(ctx, cap.id, reason="test")
        # In-memory store has no persistence, so without a session is_revoked
        # returns False — but the cache is invalidated.
        assert cap.id not in ctx.cache


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
class TestPersistedCapabilities:
    @pytest.fixture(autouse=True)
    def _setup_schema(self):
        from sqlalchemy import text

        from axiom.extensions.builtins.vault.db_models import Base
        from axiom.infra.db import ensure_schema, get_engine, session_for

        engine = get_engine()
        ensure_schema(engine, "vault")
        with engine.begin() as conn:
            conn.execute(text('SET search_path TO "vault", public'))
            Base.metadata.create_all(conn)
        yield
        with session_for("vault") as s:
            for tbl in ("capabilities", "revocations", "secret_refs", "outbound_receipts"):
                s.execute(text(f"TRUNCATE TABLE {tbl} CASCADE"))
            s.commit()

    def test_issued_capability_persists(self):
        from axiom.infra.db import session_for

        ctx = VaultContext(session_factory=lambda: session_for("vault"))
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("notification.send"),
            resource_pattern=ResourcePattern("slack://*"),
            classification_ceiling=Classification.INTERNAL,
            secret_ref="slack",
        )
        # Clear cache to force a load from DB.
        ctx.cache.clear()
        loaded = get_capability_by_id(ctx, cap.id)
        assert loaded is not None
        assert loaded.subject.handle == "@alice:test"
        assert loaded.intent_pattern.value == "notification.send"

    def test_revoke_persists_and_blocks_load(self):
        from axiom.infra.db import session_for

        ctx = VaultContext(session_factory=lambda: session_for("vault"))
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
        )
        revoke_capability(ctx, cap.id, reason="rotation drill")
        ctx.cache.clear()
        assert get_capability_by_id(ctx, cap.id) is None
        assert is_revoked(ctx, cap.id)
