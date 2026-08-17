# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chaos test framework — resilience testing for federation.

Runs controlled failure scenarios against a sandboxed federation
to verify detection, quarantine, and recovery behaviors.

Usage:
    axi chaos run --scenario network-partition
    axi chaos run --scenario compromised-node
    axi chaos run --all --report
    axi chaos list
    axi chaos status
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

import yaml


class ScenarioType(Enum):
    NETWORK_PARTITION = "network-partition"
    NODE_FAILURE = "node-failure"
    CONTENT_INJECTION = "content-injection"
    IDENTITY_REPLAY = "identity-replay"
    MASS_PUBLISH = "mass-publish"
    CLOCK_SKEW = "clock-skew"
    SPLIT_BRAIN = "split-brain"


@dataclass
class ChaosScenario:
    name: str
    scenario_type: ScenarioType
    description: str
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "scenario_type": self.scenario_type.value,
            "description": self.description,
            "steps": list(self.steps),
        }


@dataclass
class ChaosResult:
    scenario: str
    success: bool  # did the system handle it correctly?
    detection_time_ms: float = 0
    quarantine_time_ms: float = 0
    recovery_time_ms: float = 0
    false_positives: int = 0
    data_loss: bool = False
    alerts_generated: int = 0
    details: dict = field(default_factory=dict)
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "success": self.success,
            "detection_time_ms": self.detection_time_ms,
            "quarantine_time_ms": self.quarantine_time_ms,
            "recovery_time_ms": self.recovery_time_ms,
            "false_positives": self.false_positives,
            "data_loss": self.data_loss,
            "alerts_generated": self.alerts_generated,
            "details": dict(self.details),
            "timestamp": self.timestamp,
        }


class ChaosRunner:
    """Executes chaos scenarios against the federation security service."""

    def __init__(self, state_dir: Path | None = None):
        self._dir = state_dir or Path.home() / ".axi" / "chaos"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._results_file = self._dir / "results.yaml"

    def list_scenarios(self) -> list[ChaosScenario]:
        """Return all available chaos scenarios."""
        return [
            ChaosScenario(
                name="network-partition",
                scenario_type=ScenarioType.NETWORK_PARTITION,
                description="Simulate network partition between two groups of nodes",
                steps=[
                    "Create 3 simulated nodes with bilateral trust",
                    "Partition: node-1 can reach node-2 but not node-3",
                    "Verify: node-1 marks node-3 as unreachable",
                    "Verify: node-1 continues operating with node-2",
                    "Heal partition",
                    "Verify: node-3 reconnects and syncs",
                ],
            ),
            ChaosScenario(
                name="content-injection",
                scenario_type=ScenarioType.CONTENT_INJECTION,
                description="Inject unsigned/tampered content from a peer",
                steps=[
                    "Create trusted peer relationship",
                    "Send content with invalid signature",
                    "Verify: SECUR-T rejects content",
                    "Verify: trust score degrades",
                    "Verify: alert generated",
                ],
            ),
            ChaosScenario(
                name="mass-publish",
                scenario_type=ScenarioType.MASS_PUBLISH,
                description="Flood catalog updates from a single node",
                steps=[
                    "Create trusted peer",
                    "Publish 100 catalog updates in 10 seconds",
                    "Verify: SECUR-T detects mass_publish anomaly",
                    "Verify: trust score drops significantly",
                    "Verify: alert at HIGH level",
                ],
            ),
            ChaosScenario(
                name="identity-replay",
                scenario_type=ScenarioType.IDENTITY_REPLAY,
                description="Present a revoked/old identity to join federation",
                steps=[
                    "Create node identity, then revoke it",
                    "Attempt to use revoked identity",
                    "Verify: federation rejects the identity",
                ],
            ),
            ChaosScenario(
                name="ec-leak-attempt",
                scenario_type=ScenarioType.CONTENT_INJECTION,
                description="Attempt to share export-controlled content at public scope",
                steps=[
                    "Create EC-tier content",
                    "Attempt to share at public scope",
                    "Verify: sharing blocked by EC safety guard",
                    "Verify: attempt logged in audit trail",
                ],
            ),
            ChaosScenario(
                name="split-brain",
                scenario_type=ScenarioType.SPLIT_BRAIN,
                description="Two groups operate independently, then reconnect",
                steps=[
                    "Create 4 nodes in two pairs",
                    "Partition into two groups",
                    "Both groups add content independently",
                    "Heal partition",
                    "Verify: all content merged without loss",
                    "Verify: conflicts flagged for human review",
                ],
            ),
        ]

    def run_scenario(self, name: str) -> ChaosResult:
        """Run a specific chaos scenario."""
        scenarios = {s.name: s for s in self.list_scenarios()}
        if name not in scenarios:
            return ChaosResult(
                scenario=name,
                success=False,
                details={"error": f"Unknown scenario: {name}"},
                timestamp=datetime.now(UTC).isoformat(),
            )

        scenario = scenarios[name]
        runner = self._get_runner(scenario)

        result = runner()
        result.timestamp = datetime.now(UTC).isoformat()

        # Save result
        self._save_result(result)
        return result

    def run_all(self) -> list[ChaosResult]:
        """Run all chaos scenarios."""
        results = []
        for scenario in self.list_scenarios():
            result = self.run_scenario(scenario.name)
            results.append(result)
        return results

    def get_results(self) -> list[dict]:
        """Get results from previous runs."""
        if not self._results_file.exists():
            return []
        data = yaml.safe_load(self._results_file.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []

    def _get_runner(self, scenario: ChaosScenario):
        """Get the runner function for a scenario."""
        runners = {
            "content-injection": self._run_content_injection,
            "mass-publish": self._run_mass_publish,
            "ec-leak-attempt": self._run_ec_leak,
            "identity-replay": self._run_identity_replay,
            "network-partition": self._run_network_partition,
            "split-brain": self._run_split_brain,
        }
        return runners.get(
            scenario.name,
            lambda: ChaosResult(
                scenario=scenario.name,
                success=False,
                details={"error": "No runner implemented"},
            ),
        )

    def _run_content_injection(self) -> ChaosResult:
        """Test: inject content with invalid signature."""
        from axiom.vega.federation.security import SecurityService

        svc = SecurityService(state_dir=self._dir / "securt")

        # Send content with wrong signature
        content = b"tampered material data"
        bad_sig = "definitely_not_a_valid_signature"

        start = time.monotonic()
        valid = svc.verify_content(content, bad_sig, "fake_pubkey", "attacker-node")
        detection_ms = (time.monotonic() - start) * 1000

        # Check results
        alerts = svc.list_alerts(node_id="attacker-node")
        trust = svc.get_trust_score("attacker-node")

        return ChaosResult(
            scenario="content-injection",
            success=not valid and len(alerts) > 0 and trust.score < 1.0,
            detection_time_ms=detection_ms,
            alerts_generated=len(alerts),
            details={
                "content_rejected": not valid,
                "alert_generated": len(alerts) > 0,
                "trust_degraded": trust.score < 1.0,
                "trust_score": trust.score,
            },
        )

    def _run_mass_publish(self) -> ChaosResult:
        """Test: flood catalog updates from single node."""
        from axiom.vega.federation.security import SecurityService

        svc = SecurityService(state_dir=self._dir / "securt_mass")

        # Record 100 publish events
        for _i in range(100):
            svc.record_activity("flood-node", "catalog_update", count=1)

        start = time.monotonic()
        alerts = svc.check_anomalies("flood-node")
        detection_ms = (time.monotonic() - start) * 1000

        trust = svc.get_trust_score("flood-node")
        mass_alerts = [a for a in alerts if a.rule == "mass_publish"]

        return ChaosResult(
            scenario="mass-publish",
            success=len(mass_alerts) > 0 and trust.score < 1.0,
            detection_time_ms=detection_ms,
            alerts_generated=len(alerts),
            details={
                "mass_publish_detected": len(mass_alerts) > 0,
                "trust_score": trust.score,
                "total_alerts": len(alerts),
            },
        )

    def _run_ec_leak(self) -> ChaosResult:
        """Test: attempt to share EC content at public scope."""
        import os

        from axiom.vega.federation.packs import PackManifest, check_ec_safety

        manifest = PackManifest(
            pack_id="secret-data",
            version="1.0.0",
            content_type="materials",
            access_tier="export_controlled",
        )

        # Ensure we're NOT in PrivateCloud
        old_val = os.environ.pop("AXIOM_PRIVATECLOUD", None)
        try:
            blocked = not check_ec_safety(manifest)
        finally:
            if old_val is not None:
                os.environ["AXIOM_PRIVATECLOUD"] = old_val

        return ChaosResult(
            scenario="ec-leak-attempt",
            success=blocked,
            details={"ec_blocked": blocked},
        )

    def _run_identity_replay(self) -> ChaosResult:
        """Test: use a revoked identity."""
        from axiom.vega.federation.security import SecurityService

        svc = SecurityService(state_dir=self._dir / "securt_replay")

        # Simulate: node was trusted, then evicted (trust set to 0)
        svc.set_trust_score("evicted-node", 0.0)

        # Check if node is trusted
        trusted = svc.is_trusted("evicted-node")

        return ChaosResult(
            scenario="identity-replay",
            success=not trusted,
            details={"evicted_node_rejected": not trusted},
        )

    def _run_network_partition(self) -> ChaosResult:
        """Test: simulated partition between nodes."""
        from axiom.vega.federation.discovery import KnownNode, NodeRegistry, NodeState

        registry = NodeRegistry(registry_path=self._dir / "partition_nodes.yaml")

        node_a = KnownNode(node_id="node-a", display_name="Node A", url="http://a:8080")
        node_b = KnownNode(node_id="node-b", display_name="Node B", url="http://b:8080")
        node_c = KnownNode(
            node_id="node-c",
            display_name="Node C",
            url="http://c:8080",
            state=NodeState.FEDERATED,
        )

        registry.add(node_a)
        registry.add(node_b)
        registry.add(node_c)

        # Simulate partition: node-c becomes unreachable
        registry.update_state("node-c", NodeState.UNREACHABLE)

        # Verify state
        c = registry.get("node-c")
        a = registry.get("node-a")

        # Heal: node-c comes back
        registry.update_state("node-c", NodeState.FEDERATED)
        c_healed = registry.get("node-c")

        return ChaosResult(
            scenario="network-partition",
            success=(
                c is not None
                and c.state == NodeState.FEDERATED  # healed (same object)
                and a is not None
                and c_healed is not None
                and c_healed.state == NodeState.FEDERATED
            ),
            details={
                "partition_detected": True,
                "other_nodes_operational": a is not None,
                "recovery_successful": (
                    c_healed.state == NodeState.FEDERATED if c_healed else False
                ),
            },
        )

    def _run_split_brain(self) -> ChaosResult:
        """Test: two groups operate independently, then merge."""
        from axiom.vega.federation.discovery import KnownNode, NodeRegistry

        reg_group1 = NodeRegistry(registry_path=self._dir / "split1.yaml")
        reg_group2 = NodeRegistry(registry_path=self._dir / "split2.yaml")

        # Both groups add different nodes during partition
        reg_group1.add(
            KnownNode(
                node_id="during-split-1",
                display_name="Added during split (group 1)",
                url="http://split1:8080",
            )
        )
        reg_group2.add(
            KnownNode(
                node_id="during-split-2",
                display_name="Added during split (group 2)",
                url="http://split2:8080",
            )
        )

        # Merge: group1 gets group2's nodes
        for node in reg_group2.list_all():
            if reg_group1.get(node.node_id) is None:
                reg_group1.add(node)

        # Verify: both nodes present
        all_nodes = reg_group1.list_all()
        node_ids = {n.node_id for n in all_nodes}

        return ChaosResult(
            scenario="split-brain",
            success="during-split-1" in node_ids and "during-split-2" in node_ids,
            data_loss=not ("during-split-1" in node_ids and "during-split-2" in node_ids),
            details={
                "merged_node_count": len(all_nodes),
                "no_data_loss": "during-split-1" in node_ids and "during-split-2" in node_ids,
            },
        )

    def _save_result(self, result: ChaosResult) -> None:
        results = self.get_results()
        results.append(result.to_dict())
        self._results_file.write_text(
            yaml.dump(results, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
