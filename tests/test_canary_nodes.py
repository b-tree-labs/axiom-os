# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for canary node protocol — detect, sandbox, smoke, attest, promote."""

from __future__ import annotations


class TestCanaryDataModels:
    def test_canary_config(self):
        from axiom.vega.federation.canary import CanaryConfig
        cfg = CanaryConfig(smoke_tier=2)
        assert cfg.check_interval == 900
        assert cfg.smoke_tier == 2
        assert "axiom-os-lm" in cfg.packages

    def test_upgrade_policy(self):
        from axiom.vega.federation.canary import UpgradePolicy
        pol = UpgradePolicy()
        assert pol.min_canary_attestations == 3
        assert pol.require_os_diversity is True
        assert pol.channel == "stable"

    def test_attestation_fields(self):
        from axiom.vega.federation.canary import CanaryAttestation
        att = CanaryAttestation(
            node_id="ax-7f3a", canary_name="AXI",
            version="0.8.1", previous_version="0.7.0",
            status="green",
        )
        assert att.status == "green"
        assert att.node_id == "ax-7f3a"

    def test_attestation_signing_payload(self):
        from axiom.vega.federation.canary import CanaryAttestation
        att = CanaryAttestation(
            node_id="test", canary_name="SCAN",
            version="1.0.0", previous_version="0.9.0",
            status="green", nonce="abc123",
        )
        payload = att.signing_payload()
        assert isinstance(payload, bytes)
        assert b"1.0.0" in payload
        assert b"green" in payload


class TestSmokeRegistry:
    def test_importable(self):
        from axiom.vega.federation.canary import SmokeRegistry
        assert SmokeRegistry is not None

    def test_register_and_list(self):
        from axiom.vega.federation.canary import SmokeRegistry
        reg = SmokeRegistry()

        @reg.test(tier=1, name="basic")
        def basic_test():
            return True

        tests = reg.get_tier(1)
        assert len(tests) == 1
        assert tests[0].name == "basic"

    def test_tiers_isolated(self):
        from axiom.vega.federation.canary import SmokeRegistry
        reg = SmokeRegistry()

        @reg.test(tier=1, name="t1")
        def t1(): return True

        @reg.test(tier=2, name="t2")
        def t2(): return True

        assert len(reg.get_tier(1)) == 1
        assert len(reg.get_tier(2)) == 1
        assert len(reg.get_tier(3)) == 0


class TestPromotionEvaluator:
    def _make_attestation(self, status="green", os_family="linux", infra="k3d", py="3.12"):
        from axiom.vega.federation.canary import CanaryAttestation
        return CanaryAttestation(
            node_id=f"node-{id(self)}", canary_name="test",
            version="1.0.0", previous_version="0.9.0",
            status=status, os_family=os_family,
            infra_tier=infra, python_version=py,
        )

    def test_promotes_with_quorum(self):
        from axiom.vega.federation.canary import PromotionEvaluator, UpgradePolicy

        policy = UpgradePolicy(min_canary_attestations=3, require_os_diversity=False)
        evaluator = PromotionEvaluator(policy)

        attestations = [self._make_attestation() for _ in range(3)]
        result = evaluator.evaluate("1.0.0", attestations)
        assert result.promote is True

    def test_rejects_insufficient_quorum(self):
        from axiom.vega.federation.canary import PromotionEvaluator, UpgradePolicy

        policy = UpgradePolicy(min_canary_attestations=3, require_os_diversity=False)
        evaluator = PromotionEvaluator(policy)

        attestations = [self._make_attestation() for _ in range(2)]
        result = evaluator.evaluate("1.0.0", attestations)
        assert result.promote is False
        assert "quorum" in result.reason

    def test_rejects_without_os_diversity(self):
        from axiom.vega.federation.canary import PromotionEvaluator, UpgradePolicy

        policy = UpgradePolicy(min_canary_attestations=3, require_os_diversity=True)
        evaluator = PromotionEvaluator(policy)

        # All Linux — no diversity
        attestations = [self._make_attestation(os_family="linux") for _ in range(3)]
        result = evaluator.evaluate("1.0.0", attestations)
        assert result.promote is False
        assert "diversity" in result.reason

    def test_accepts_with_os_diversity(self):
        from axiom.vega.federation.canary import PromotionEvaluator, UpgradePolicy

        policy = UpgradePolicy(min_canary_attestations=3, require_os_diversity=True)
        evaluator = PromotionEvaluator(policy)

        attestations = [
            self._make_attestation(os_family="linux"),
            self._make_attestation(os_family="darwin"),
            self._make_attestation(os_family="linux"),
        ]
        result = evaluator.evaluate("1.0.0", attestations)
        assert result.promote is True

    def test_rejects_if_red_on_matching_profile(self):
        from axiom.vega.federation.canary import PromotionEvaluator, UpgradePolicy

        policy = UpgradePolicy(
            min_canary_attestations=2, require_os_diversity=False,
            require_matching_profile=True,
        )
        evaluator = PromotionEvaluator(policy, os_family="linux", infra_tier="k3d")

        attestations = [
            self._make_attestation(status="green"),
            self._make_attestation(status="green"),
            self._make_attestation(status="red", os_family="linux", infra="k3d"),
        ]
        result = evaluator.evaluate("1.0.0", attestations)
        assert result.promote is False
        assert "failure" in result.reason

    def test_ignores_red_on_different_profile(self):
        from axiom.vega.federation.canary import PromotionEvaluator, UpgradePolicy

        policy = UpgradePolicy(
            min_canary_attestations=2, require_os_diversity=False,
        )
        evaluator = PromotionEvaluator(policy, os_family="linux", infra_tier="k3d")

        attestations = [
            self._make_attestation(status="green"),
            self._make_attestation(status="green"),
            self._make_attestation(status="red", os_family="darwin", infra="native"),
        ]
        result = evaluator.evaluate("1.0.0", attestations)
        assert result.promote is True  # Red is on darwin, we're linux


class TestGossipSink:
    def test_push_and_list(self):
        import tempfile
        from pathlib import Path

        from axiom.vega.federation.canary import CanaryAttestation, GossipSink

        with tempfile.TemporaryDirectory() as tmp:
            sink = GossipSink(state_dir=Path(tmp))
            att = CanaryAttestation(
                node_id="test", canary_name="SCAN",
                version="1.0.0", previous_version="0.9.0",
                status="green",
            )
            sink.push(att)
            listed = sink.list_attestations("1.0.0")
            assert len(listed) == 1
            assert listed[0].status == "green"
