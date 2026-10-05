# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TDD tests for WARDEN — Vega's federation-governance agent.

WARDEN's first wired feature is peer-state transition validation:
`validate_transition(from_state, to_state, evidence) -> WardenVerdict`.
The legality matrix matches `axiom.vega.federation.discovery.NodeState`:
  UNKNOWN → DISCOVERED       (transport contact)
  DISCOVERED → VERIFIED      (identity-fetch succeeded)
  VERIFIED → TRUSTED         (trust-graph score ≥ threshold)
  TRUSTED → FEDERATED        (cohort membership ratified)
  * → QUARANTINED            (any state can quarantine)
  QUARANTINED → REVOKED      (explicit eviction)
  QUARANTINED → VERIFIED     (recovery ceremony)

WARDEN refuses any transition not in this matrix and emits a structured
verdict explaining the refusal.
"""

from __future__ import annotations


class TestValidateTransition:
    def test_legal_discovered_to_verified_with_evidence(self):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        v = Warden()
        verdict = v.validate_transition(
            from_state="discovered",
            to_state="verified",
            evidence={"identity_verified_at": "2026-04-28T00:00:00Z",
                      "public_key": "deadbeef"},
        )
        assert verdict.approved is True
        assert verdict.reason_code == "transition_legal"

    def test_skipping_stages_is_rejected(self):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        v = Warden()
        verdict = v.validate_transition(
            from_state="discovered",
            to_state="trusted",
            evidence={"public_key": "deadbeef"},
        )
        assert verdict.approved is False
        assert verdict.reason_code == "stage_skip"

    def test_verified_to_trusted_requires_trust_score(self):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        v = Warden()
        # Missing trust_score
        v1 = v.validate_transition(
            from_state="verified",
            to_state="trusted",
            evidence={},
        )
        assert v1.approved is False
        assert v1.reason_code == "missing_trust_score"

        # Below threshold
        v2 = v.validate_transition(
            from_state="verified",
            to_state="trusted",
            evidence={"trust_score": 0.2, "trust_threshold": 0.5},
        )
        assert v2.approved is False
        assert v2.reason_code == "trust_below_threshold"

        # At/above threshold
        v3 = v.validate_transition(
            from_state="verified",
            to_state="trusted",
            evidence={"trust_score": 0.6, "trust_threshold": 0.5},
        )
        assert v3.approved is True

    def test_trusted_to_federated_requires_cohort(self):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        v = Warden()
        v_no = v.validate_transition(
            from_state="trusted",
            to_state="federated",
            evidence={},
        )
        assert v_no.approved is False
        assert v_no.reason_code == "missing_cohort_membership"

        v_yes = v.validate_transition(
            from_state="trusted",
            to_state="federated",
            evidence={"cohort_membership_id": "cohort-abc"},
        )
        assert v_yes.approved is True

    def test_quarantine_legal_from_any_active_state(self):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        v = Warden()
        for from_state in ("discovered", "verified", "trusted", "federated"):
            verdict = v.validate_transition(
                from_state=from_state,
                to_state="quarantined",
                evidence={"reason": "anomaly detected"},
            )
            assert verdict.approved is True, f"quarantine from {from_state}"

    def test_revoke_only_from_quarantine(self):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        v = Warden()
        v_legal = v.validate_transition(
            from_state="quarantined",
            to_state="revoked",
            evidence={},
        )
        assert v_legal.approved is True

        v_illegal = v.validate_transition(
            from_state="trusted",
            to_state="revoked",
            evidence={},
        )
        assert v_illegal.approved is False
        assert v_illegal.reason_code == "must_quarantine_first"

    def test_recovery_quarantine_to_verified(self):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        v = Warden()
        verdict = v.validate_transition(
            from_state="quarantined",
            to_state="verified",
            evidence={"recovery_ceremony_id": "recover-123",
                      "operator_signature": "sig"},
        )
        assert verdict.approved is True

    def test_unknown_state_strings_rejected(self):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        v = Warden()
        verdict = v.validate_transition(
            from_state="trusted",
            to_state="some_bogus_state",
            evidence={},
        )
        assert verdict.approved is False
        assert verdict.reason_code == "unknown_state"


class TestVerdictPersistence:
    def test_verdicts_appended_to_audit_log(self, tmp_path, monkeypatch):
        from axiom.extensions.builtins.federation.agents.warden import Warden

        monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))

        v = Warden()
        v.validate_transition(
            from_state="discovered",
            to_state="verified",
            evidence={"public_key": "k"},
            node_id="node-1",
        )
        v.validate_transition(
            from_state="discovered",
            to_state="trusted",  # illegal
            evidence={},
            node_id="node-2",
        )

        log = tmp_path / "state" / "agents" / "warden" / "verdicts.jsonl"
        assert log.exists()
        lines = log.read_text().splitlines()
        assert len(lines) == 2

        import json
        first = json.loads(lines[0])
        assert first["node_id"] == "node-1"
        assert first["approved"] is True
        second = json.loads(lines[1])
        assert second["approved"] is False
        assert second["reason_code"] == "stage_skip"
