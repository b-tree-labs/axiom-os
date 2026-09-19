# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Stage 5a — FederationGateway visibility-horizon enforcement.

Per `docs/specs/spec-federation-policy.md §6`, scoped to Stage 5a per
`docs/working/memory-roadmap.md`. Stage 5a enforces visibility horizons
on outbound projection + inbound acceptance only; classification +
nationality filtering is Stage 5b (post-Prague).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from axiom.memory.fragment import MemoryFragment, create_fragment
from axiom.vega.federation.gateway import (
    AcceptDecision,
    FederationGateway,
    SignedProjection,
)
from axiom.vega.federation.policy import (
    ClassificationStamp,
    InboundOverride,
    TrustProfile,
    VisibilityHorizon,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _frag(visibility: VisibilityHorizon, *, principal: str = "alice",
          classification: ClassificationStamp | None = None,
          content_extra: dict | None = None) -> MemoryFragment:
    """Build a semantic fragment with the given visibility."""
    base = create_fragment(
        content={"text": f"frag-{visibility.value}", **(content_extra or {})},
        cognitive_type="semantic",
        principal_id=principal,
        agents=set(),
        resources=set(),
    )
    return dataclasses.replace(
        base,
        visibility=visibility,
        classification=classification or ClassificationStamp.unclassified(),
    )


def _fixed_signer(payload: bytes) -> str:
    """Deterministic mock signer — sha256 hex of payload."""
    import hashlib
    return hashlib.sha256(payload).hexdigest()


def _accept_all_verifier(payload: bytes, signature: str, peer_id: str) -> bool:
    return True


def _reject_all_verifier(payload: bytes, signature: str, peer_id: str) -> bool:
    return False


def _audit_recorder():
    """Return (recorder, calls) where recorder appends calls and returns nothing."""
    calls: list[dict] = []

    def _record(entry_type: str, **kwargs) -> None:
        calls.append({"entry_type": entry_type, **kwargs})

    return _record, calls


# ---------------------------------------------------------------------------
# Outbound — visibility horizon enforcement
# ---------------------------------------------------------------------------


class TestOutboundVisibilityHorizon:
    """C4: SCOPE_INTERNAL fragments never leave; per-peer threshold honored."""

    def test_scope_internal_never_leaves_to_declared_peer(self):
        """A SCOPE_INTERNAL fragment MUST be skipped even for declared peers."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.SCOPE_INTERNAL)
        signed = gateway.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@partner-1",
        )
        assert signed.payload["fragments"] == []

    def test_scope_internal_never_leaves_to_undeclared_peer(self):
        """Default-deny: undeclared peer never receives SCOPE_INTERNAL."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(scope="ne101-prague"),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.SCOPE_INTERNAL)
        signed = gateway.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@some-stranger",
        )
        assert signed.payload["fragments"] == []

    def test_peers_declared_leaves_to_declared_peer(self):
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        signed = gateway.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@partner-1",
        )
        assert len(signed.payload["fragments"]) == 1
        assert signed.payload["fragments"][0]["id"] == f.id

    def test_peers_declared_blocked_from_undeclared_peer(self):
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        signed = gateway.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@stranger",
        )
        assert signed.payload["fragments"] == []

    def test_public_fragment_leaves_to_any_peer(self):
        """PUBLIC content reaches even non-declared peers (the ceiling case)."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(scope="ne101-prague"),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.PUBLIC)
        signed = gateway.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@anywhere",
        )
        assert len(signed.payload["fragments"]) == 1

    def test_horizon_max_reflects_max_horizon_in_payload(self):
        """SignedProjection.horizon_max is the most permissive horizon present."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f1 = _frag(VisibilityHorizon.PEERS_DECLARED)
        f2 = _frag(VisibilityHorizon.PUBLIC)
        signed = gateway.project_for_peer(
            projection={"fragments": [f1.to_dict(), f2.to_dict()]},
            peer_id="@partner-1",
        )
        assert signed.horizon_max is VisibilityHorizon.PUBLIC

    def test_horizon_max_default_when_empty(self):
        """Empty payload → horizon_max defaults to SCOPE_INTERNAL."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(scope="ne101-prague"),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.SCOPE_INTERNAL)
        signed = gateway.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@stranger",
        )
        assert signed.horizon_max is VisibilityHorizon.SCOPE_INTERNAL

    def test_per_peer_outbound_override_honored(self):
        """When a profile says we send only PUBLIC to peer X, PEERS_DECLARED is blocked
        even if peer X is in declared_peers."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
                outbound_per_peer={"@partner-1": VisibilityHorizon.PUBLIC},
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        peers_declared = _frag(VisibilityHorizon.PEERS_DECLARED)
        public = _frag(VisibilityHorizon.PUBLIC)
        signed = gateway.project_for_peer(
            projection={"fragments": [peers_declared.to_dict(), public.to_dict()]},
            peer_id="@partner-1",
        )
        # Only PUBLIC passes the override.
        kept_ids = [f["id"] for f in signed.payload["fragments"]]
        assert kept_ids == [public.id]

    def test_classification_stamp_passed_through_unchanged(self):
        """Stage 5a does not enforce classification; the stamp rides through.

        Stage 5b will compose classification.allowed_outflow_level() with
        visibility per spec §6 step 1. Stage 5a passes the stamp untouched
        so 5b can opt-in without rewriting outbound serialization.
        """
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        # CUI fragment with optimistic PEERS_DECLARED visibility.
        # In Stage 5b, the gateway would clamp via classification; in 5a
        # the visibility alone gates and the stamp passes through.
        cui = _frag(
            VisibilityHorizon.PEERS_DECLARED,
            classification=ClassificationStamp(level="cui"),
        )
        signed = gateway.project_for_peer(
            projection={"fragments": [cui.to_dict()]},
            peer_id="@partner-1",
        )
        assert len(signed.payload["fragments"]) == 1
        assert signed.payload["fragments"][0]["classification"]["level"] == "cui"

    def test_signature_is_set_and_signs_canonical_payload(self):
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        signed = gateway.project_for_peer(
            projection={"fragments": [f.to_dict()]},
            peer_id="@partner-1",
        )
        assert signed.signature
        assert signed.origin_scope == "ne101-prague"
        assert signed.target_peer == "@partner-1"
        assert isinstance(signed.signed_at, datetime)

    def test_max_hops_above_one_raises_not_implemented(self):
        """Stage 5a is hop=1 only. max_hops > 1 is Stage 5b territory."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(scope="ne101-prague"),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        with pytest.raises(NotImplementedError, match="Stage 5b"):
            gateway.project_for_peer(
                projection={"fragments": []},
                peer_id="@partner-1",
                max_hops=2,
            )


# ---------------------------------------------------------------------------
# Inbound — visibility-horizon acceptance
# ---------------------------------------------------------------------------


class TestInboundVisibilityHorizon:
    """C4 inbound: signature + declared peer + horizon-allowlist gating."""

    def test_invalid_signature_rejects_all(self):
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
                inbound_horizons=frozenset(
                    {VisibilityHorizon.SCOPE_INTERNAL, VisibilityHorizon.PEERS_DECLARED}
                ),
            ),
            signer=_fixed_signer,
            verifier=_reject_all_verifier,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        incoming = SignedProjection(
            payload={"fragments": [f.to_dict()]},
            origin_scope="@partner-1",
            target_peer="ne101-prague",
            signature="deadbeef",
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PEERS_DECLARED,
        )
        decision = gateway.accept_from_peer(incoming, peer_id="@partner-1")
        assert decision.accepted is False
        assert decision.reason == "signature_invalid"
        assert decision.fragments_accepted == 0
        assert decision.fragments_rejected == 1

    def test_undeclared_peer_rejects_all(self):
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                inbound_horizons=frozenset(
                    {VisibilityHorizon.SCOPE_INTERNAL, VisibilityHorizon.PEERS_DECLARED}
                ),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        incoming = SignedProjection(
            payload={"fragments": [f.to_dict()]},
            origin_scope="@stranger",
            target_peer="ne101-prague",
            signature="deadbeef",
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PEERS_DECLARED,
        )
        decision = gateway.accept_from_peer(incoming, peer_id="@stranger")
        assert decision.accepted is False
        assert decision.reason == "peer_not_declared"

    def test_per_fragment_horizon_filter(self):
        """Fragments outside `inbound_horizons` are individually rejected;
        others accepted."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
                inbound_horizons=frozenset(
                    {VisibilityHorizon.PEERS_DECLARED}
                ),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        ok = _frag(VisibilityHorizon.PEERS_DECLARED)
        # PUBLIC is not in our inbound_horizons → individually rejected.
        rejected = _frag(VisibilityHorizon.PUBLIC)
        incoming = SignedProjection(
            payload={"fragments": [ok.to_dict(), rejected.to_dict()]},
            origin_scope="@partner-1",
            target_peer="ne101-prague",
            signature="deadbeef",
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PUBLIC,
        )
        decision = gateway.accept_from_peer(incoming, peer_id="@partner-1")
        assert decision.accepted is True
        assert decision.fragments_accepted == 1
        assert decision.fragments_rejected == 1

    def test_per_peer_override_more_restrictive(self):
        """A per-peer override narrows what we accept from one specific peer."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1", "@suspicious"}),
                inbound_horizons=frozenset(
                    {VisibilityHorizon.PEERS_DECLARED, VisibilityHorizon.PUBLIC}
                ),
                inbound_per_peer={
                    "@suspicious": InboundOverride(
                        accepted_horizons=frozenset(
                            {VisibilityHorizon.SCOPE_INTERNAL}
                        ),
                    ),
                },
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        incoming = SignedProjection(
            payload={"fragments": [f.to_dict()]},
            origin_scope="@suspicious",
            target_peer="ne101-prague",
            signature="deadbeef",
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PEERS_DECLARED,
        )
        decision = gateway.accept_from_peer(incoming, peer_id="@suspicious")
        # Override blocks PEERS_DECLARED for this peer.
        assert decision.fragments_rejected == 1
        assert decision.fragments_accepted == 0

    def test_pure_mode_returns_fragments_no_writer(self):
        """No writer injected → pure-mode returns AcceptDecision with no side
        effect."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
                inbound_horizons=frozenset({VisibilityHorizon.PEERS_DECLARED}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        incoming = SignedProjection(
            payload={"fragments": [f.to_dict()]},
            origin_scope="@partner-1",
            target_peer="ne101-prague",
            signature="deadbeef",
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PEERS_DECLARED,
        )
        decision = gateway.accept_from_peer(incoming, peer_id="@partner-1")
        assert decision.accepted is True
        assert decision.fragments_accepted == 1
        # accepted_fragments is exposed for caller-driven write-down.
        assert len(decision.accepted_fragments) == 1
        assert decision.accepted_fragments[0].id == f.id

    def test_writer_invoked_for_accepted_fragments_only(self):
        """Inject a writer; it's called once per accepted fragment, never for
        rejected."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
                inbound_horizons=frozenset({VisibilityHorizon.PEERS_DECLARED}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        accepted = _frag(VisibilityHorizon.PEERS_DECLARED)
        rejected = _frag(VisibilityHorizon.PUBLIC)
        incoming = SignedProjection(
            payload={"fragments": [accepted.to_dict(), rejected.to_dict()]},
            origin_scope="@partner-1",
            target_peer="ne101-prague",
            signature="deadbeef",
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PUBLIC,
        )
        written: list[MemoryFragment] = []
        decision = gateway.accept_from_peer(
            incoming, peer_id="@partner-1", writer=written.append,
        )
        assert decision.fragments_accepted == 1
        assert decision.fragments_rejected == 1
        assert [w.id for w in written] == [accepted.id]


# ---------------------------------------------------------------------------
# Audit — FragmentAccepted event
# ---------------------------------------------------------------------------


class TestAuditEvent:
    def test_fragment_accepted_event_records_peer_count_signature_hash(self):
        record, calls = _audit_recorder()
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
                inbound_horizons=frozenset({VisibilityHorizon.PEERS_DECLARED}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
            audit_recorder=record,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        incoming = SignedProjection(
            payload={"fragments": [f.to_dict()]},
            origin_scope="@partner-1",
            target_peer="ne101-prague",
            signature="deadbeef" * 8,
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PEERS_DECLARED,
        )
        gateway.accept_from_peer(incoming, peer_id="@partner-1")
        accepted_events = [c for c in calls if c["entry_type"] == "FragmentAccepted"]
        assert len(accepted_events) == 1
        ev = accepted_events[0]
        assert ev["peer_id"] == "@partner-1"
        assert ev["fragments_accepted"] == 1
        assert ev["fragments_rejected"] == 0
        # Stored as a hash, not the full signature, so the audit log can
        # reference an L1-bound projection without leaking sig material.
        assert ev["signature_hash"]
        assert len(ev["signature_hash"]) == 64  # sha256 hex

    def test_no_audit_when_recorder_not_configured(self):
        """Audit recorder is optional; gateway still works without one."""
        gateway = FederationGateway(
            scope_id="ne101-prague",
            trust_profile=TrustProfile(
                scope="ne101-prague",
                declared_peers=frozenset({"@partner-1"}),
                inbound_horizons=frozenset({VisibilityHorizon.PEERS_DECLARED}),
            ),
            signer=_fixed_signer,
            verifier=_accept_all_verifier,
        )
        f = _frag(VisibilityHorizon.PEERS_DECLARED)
        incoming = SignedProjection(
            payload={"fragments": [f.to_dict()]},
            origin_scope="@partner-1",
            target_peer="ne101-prague",
            signature="deadbeef",
            signed_at=datetime.now(UTC),
            horizon_max=VisibilityHorizon.PEERS_DECLARED,
        )
        # Just verifying no exception.
        decision = gateway.accept_from_peer(incoming, peer_id="@partner-1")
        assert decision.accepted is True


# ---------------------------------------------------------------------------
# AcceptDecision shape
# ---------------------------------------------------------------------------


class TestAcceptDecisionShape:
    def test_decision_is_frozen_dataclass(self):
        decision = AcceptDecision(
            accepted=True,
            reason="ok",
            fragments_accepted=1,
            fragments_rejected=0,
            accepted_fragments=(),
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            decision.accepted = False  # type: ignore[misc]
