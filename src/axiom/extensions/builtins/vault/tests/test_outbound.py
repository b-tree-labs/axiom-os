# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `outbound_call` — the plaintext-credential chokepoint.

Per spec §2.3 + §9.2: the ONLY plaintext-credential site. Tests verify
the credential never leaves this scope, the receipt is written, the
capability is enforced.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from axiom.extensions.builtins.vault.capability_store import (
    VaultContext,
    issue_capability,
    revoke_capability,
)
from axiom.extensions.builtins.vault.outbound import (
    HttpRequest,
    HttpResponse,
    outbound_call,
)
from axiom.governance import (
    Classification,
    IntentPattern,
    ResourcePattern,
)
from axiom.vega.identity.principal import Principal


def _alice() -> Principal:
    return Principal(handle="@alice:test", public_bytes=b"\x00" * 32)


def _stub_transport(captured: list[HttpRequest]):
    """A fake transport that records the outbound request + returns 200."""

    def _transport(request: HttpRequest) -> HttpResponse:
        captured.append(request)
        return HttpResponse(status_code=200, headers={}, body=b"ok")

    return _transport


# ---------------------------------------------------------------------------
# Behavioral tests with all injection (no DB, no real HTTP).
# ---------------------------------------------------------------------------


class TestOutboundCallBehavior:
    def test_attaches_credential_as_bearer(self):
        ctx = VaultContext()
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
            secret_ref="test-secret",
        )
        captured: list[HttpRequest] = []
        outbound_call(
            cap,
            HttpRequest(method="GET", url="https://example.com/x", headers={}),
            ctx,
            transport=_stub_transport(captured),
            credential_resolver=lambda _: "secret-token-123",
        )
        assert len(captured) == 1
        # The transport saw the bearer header; the caller's original
        # request did not contain it.
        assert captured[0].headers.get("Authorization") == "Bearer secret-token-123"

    def test_no_credential_no_auth_header(self):
        ctx = VaultContext()
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
            secret_ref="test-secret",
        )
        captured: list[HttpRequest] = []
        outbound_call(
            cap,
            HttpRequest(method="GET", url="https://example.com/x", headers={}),
            ctx,
            transport=_stub_transport(captured),
            credential_resolver=lambda _: None,
        )
        assert "Authorization" not in captured[0].headers

    def test_expired_capability_rejected(self):
        alice = _alice()
        # Build an explicitly-expired capability.
        ctx = VaultContext()
        cap_valid = issue_capability(
            ctx,
            subject=alice,
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
        )
        # Replace not_after with past time via dataclass replace.
        from dataclasses import replace as dc_replace

        cap_expired = dc_replace(
            cap_valid,
            not_after=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        with pytest.raises(ValueError, match="not valid"):
            outbound_call(
                cap_expired,
                HttpRequest(method="GET", url="https://example.com/x", headers={}),
                ctx,
                transport=_stub_transport([]),
                credential_resolver=lambda _: "secret",
            )

    def test_caller_request_headers_preserved(self):
        ctx = VaultContext()
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
            secret_ref="test-secret",
        )
        captured: list[HttpRequest] = []
        outbound_call(
            cap,
            HttpRequest(
                method="POST",
                url="https://example.com/x",
                headers={"X-Custom": "preserved", "Content-Type": "application/json"},
                body=b'{"key":"value"}',
            ),
            ctx,
            transport=_stub_transport(captured),
            credential_resolver=lambda _: "secret",
        )
        # Caller's headers preserved + auth attached.
        assert captured[0].headers["X-Custom"] == "preserved"
        assert captured[0].headers["Content-Type"] == "application/json"
        assert captured[0].headers["Authorization"] == "Bearer secret"
        assert captured[0].body == b'{"key":"value"}'

    def test_response_passthrough(self):
        ctx = VaultContext()
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
            secret_ref="test-secret",
        )

        def transport(req: HttpRequest) -> HttpResponse:
            return HttpResponse(
                status_code=201,
                headers={"X-Server": "test"},
                body=b'{"created":true}',
            )

        resp = outbound_call(
            cap,
            HttpRequest(method="POST", url="https://example.com/x", headers={}),
            ctx,
            transport=transport,
            credential_resolver=lambda _: "secret",
        )
        assert resp.status_code == 201
        assert resp.headers["X-Server"] == "test"
        assert resp.body == b'{"created":true}'

    def test_no_credential_leak_in_caller_request(self):
        """The credential MUST NOT appear in the caller's request object after the call."""
        ctx = VaultContext()
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
            secret_ref="test-secret",
        )
        original = HttpRequest(
            method="GET", url="https://example.com/x", headers={}
        )
        outbound_call(
            cap,
            original,
            ctx,
            transport=_stub_transport([]),
            credential_resolver=lambda _: "secret-token-must-not-leak",
        )
        # The caller's original request is frozen (dataclass(frozen=True))
        # and was never mutated. Its headers dict still doesn't have auth.
        assert "Authorization" not in original.headers


# ---------------------------------------------------------------------------
# Integration tests — receipts persist to Postgres.
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
class TestOutboundReceiptPersistence:
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

    def test_successful_call_writes_receipt(self):
        from sqlalchemy import text

        from axiom.infra.db import session_for

        ctx = VaultContext(session_factory=lambda: session_for("vault"))
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
            secret_ref="test-secret",
        )
        outbound_call(
            cap,
            HttpRequest(method="GET", url="https://example.com/x", headers={}),
            ctx,
            transport=_stub_transport([]),
            credential_resolver=lambda _: "s",
        )
        with session_for("vault") as s:
            count = s.execute(
                text(
                    "SELECT count(*) FROM outbound_receipts "
                    "WHERE capability_id = :cap AND outcome = 'succeeded'"
                ),
                {"cap": cap.id},
            ).scalar()
            assert count == 1

    def test_revoked_capability_raises_and_writes_receipt(self):
        from sqlalchemy import text

        from axiom.infra.db import session_for

        ctx = VaultContext(session_factory=lambda: session_for("vault"))
        cap = issue_capability(
            ctx,
            subject=_alice(),
            intent_pattern=IntentPattern("*"),
            resource_pattern=ResourcePattern("*"),
            classification_ceiling=Classification.INTERNAL,
            secret_ref="test-secret",
        )
        revoke_capability(ctx, cap.id, reason="test")
        with pytest.raises(ValueError, match="revoked"):
            outbound_call(
                cap,
                HttpRequest(method="GET", url="https://example.com/x", headers={}),
                ctx,
                transport=_stub_transport([]),
                credential_resolver=lambda _: "s",
            )
        with session_for("vault") as s:
            count = s.execute(
                text(
                    "SELECT count(*) FROM outbound_receipts "
                    "WHERE capability_id = :cap AND outcome = 'capability_invalid'"
                ),
                {"cap": cap.id},
            ).scalar()
            assert count == 1
