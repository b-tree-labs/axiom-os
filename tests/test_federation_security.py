# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for SECUR-T federation security guardian."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from axiom.vega.federation.security import (
    AlertStatus,
    AnomalyRule,
    EscalationContact,
    SecurityService,
    ThreatLevel,
)


@pytest.fixture
def svc(tmp_path: Path) -> SecurityService:
    return SecurityService(state_dir=tmp_path / "security")


# ── Content Verification ──────────────────────────────────────────────


class TestContentVerification:
    def test_valid_signature_passes(self, svc: SecurityService):
        content = b"hello world"
        sig = hashlib.sha256(content).hexdigest()
        assert svc.verify_content(content, sig, "", "node-a") is True

    def test_invalid_signature_fails(self, svc: SecurityService):
        content = b"hello world"
        assert svc.verify_content(content, "bad-sig", "", "node-a") is False

    def test_trust_degrades_on_failure(self, svc: SecurityService):
        content = b"data"
        svc.verify_content(content, "wrong", "", "node-b")
        ts = svc.get_trust_score("node-b")
        assert ts.score < 1.0
        assert ts.content_failed == 1

    def test_trust_tracks_verified_count(self, svc: SecurityService):
        content = b"ok"
        sig = hashlib.sha256(content).hexdigest()
        svc.verify_content(content, sig, "", "node-c")
        svc.verify_content(content, sig, "", "node-c")
        ts = svc.get_trust_score("node-c")
        assert ts.content_verified == 2

    def test_failure_creates_alert(self, svc: SecurityService):
        svc.verify_content(b"x", "bad", "", "node-d")
        alerts = svc.list_alerts(node_id="node-d")
        assert len(alerts) == 1
        assert alerts[0].threat_level == ThreatLevel.HIGH
        assert alerts[0].rule == "content_verification_failed"


# ── Anomaly Detection ─────────────────────────────────────────────────


class TestAnomalyDetection:
    def test_mass_publish_triggers(self, svc: SecurityService):
        for _ in range(55):
            svc.record_activity("node-x", "material_publish")
        alerts = svc.check_anomalies("node-x")
        rules_triggered = [a.rule for a in alerts]
        assert "mass_publish" in rules_triggered

    def test_composition_drift_triggers(self, svc: SecurityService):
        # Write activity with drift_pct directly
        event = {
            "node_id": "node-y",
            "type": "composition_change",
            "drift_pct": 10,
            "count": 1,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        with open(svc._activity_file, "a") as f:
            f.write(json.dumps(event) + "\n")
        alerts = svc.check_anomalies("node-y")
        assert any(a.rule == "composition_drift" for a in alerts)

    def test_unusual_ec_access_critical(self, svc: SecurityService):
        for _ in range(12):
            svc.record_activity("node-z", "ec_query")
        alerts = svc.check_anomalies("node-z")
        ec_alerts = [a for a in alerts if a.rule == "unusual_ec_access"]
        assert len(ec_alerts) == 1
        assert ec_alerts[0].threat_level == ThreatLevel.CRITICAL

    def test_rapid_trust_requests_triggers(self, svc: SecurityService):
        for _ in range(25):
            svc.record_activity("node-r", "trust_request")
        alerts = svc.check_anomalies("node-r")
        assert any(a.rule == "rapid_trust_requests" for a in alerts)

    def test_signature_failures_triggers(self, svc: SecurityService):
        for _ in range(5):
            svc.record_activity("node-s", "verification_failed")
        alerts = svc.check_anomalies("node-s")
        assert any(a.rule == "signature_failures" for a in alerts)

    def test_below_threshold_no_alert(self, svc: SecurityService):
        """Normal activity below thresholds should not trigger alerts."""
        for _ in range(10):
            svc.record_activity("node-ok", "material_publish")
        svc.record_activity("node-ok", "ec_query", count=2)
        alerts = svc.check_anomalies("node-ok")
        assert alerts == []


# ── Trust Scoring ─────────────────────────────────────────────────────


class TestTrustScoring:
    def test_starts_at_one(self, svc: SecurityService):
        ts = svc.get_trust_score("new-node")
        assert ts.score == 1.0

    def test_degrades_on_anomaly(self, svc: SecurityService):
        for _ in range(55):
            svc.record_activity("bad-node", "material_publish")
        svc.check_anomalies("bad-node")
        ts = svc.get_trust_score("bad-node")
        assert ts.score < 1.0
        assert ts.anomalies_detected > 0

    def test_manual_set(self, svc: SecurityService):
        svc.set_trust_score("node-m", 0.75)
        assert svc.get_trust_score("node-m").score == 0.75

    def test_clamps_to_range(self, svc: SecurityService):
        svc.set_trust_score("node-c", 5.0)
        assert svc.get_trust_score("node-c").score == 1.0
        svc.set_trust_score("node-c", -1.0)
        assert svc.get_trust_score("node-c").score == 0.0

    def test_is_trusted_threshold(self, svc: SecurityService):
        svc.set_trust_score("node-t", 0.6)
        assert svc.is_trusted("node-t", threshold=0.5) is True
        assert svc.is_trusted("node-t", threshold=0.7) is False


# ── Alert Lifecycle ───────────────────────────────────────────────────


class TestAlertLifecycle:
    def test_create_list_resolve(self, svc: SecurityService):
        svc.verify_content(b"x", "bad", "", "node-a")
        alerts = svc.list_alerts()
        assert len(alerts) == 1
        assert alerts[0].status == AlertStatus.OPEN

        svc.resolve_alert(alerts[0].alert_id, resolved_by="admin")
        resolved = svc.list_alerts(status="resolved")
        assert len(resolved) == 1
        assert resolved[0].resolved_by == "admin"

    def test_filter_by_status(self, svc: SecurityService):
        svc.verify_content(b"a", "bad", "", "n1")
        svc.verify_content(b"b", "bad", "", "n2")
        alerts = svc.list_alerts()
        svc.resolve_alert(alerts[0].alert_id)
        assert len(svc.list_alerts(status="open")) == 1
        assert len(svc.list_alerts(status="resolved")) == 1

    def test_filter_by_node(self, svc: SecurityService):
        svc.verify_content(b"a", "bad", "", "n1")
        svc.verify_content(b"b", "bad", "", "n2")
        assert len(svc.list_alerts(node_id="n1")) == 1
        assert len(svc.list_alerts(node_id="n2")) == 1

    def test_false_positive(self, svc: SecurityService):
        svc.verify_content(b"x", "bad", "", "n1")
        aid = svc.list_alerts()[0].alert_id
        svc.resolve_alert(aid, false_positive=True)
        a = svc.list_alerts(status="false_positive")
        assert len(a) == 1


# ── Escalation Path ──────────────────────────────────────────────────


class TestEscalationPath:
    def test_no_contacts_unhealthy(self, svc: SecurityService):
        result = svc.verify_escalation_path()
        assert result["healthy"] is False
        assert "No escalation contacts" in result.get("error", "")

    def test_configured_contacts_healthy(self, svc: SecurityService):
        now = datetime.now(UTC).isoformat()
        svc.set_escalation_contacts([
            EscalationContact("Alice", "alice@example.com", "operator", last_verified=now),
        ])
        result = svc.verify_escalation_path()
        assert result["healthy"] is True
        assert result["operator_count"] == 1

    def test_stale_contacts_unhealthy(self, svc: SecurityService):
        old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
        svc.set_escalation_contacts([
            EscalationContact("Bob", "bob@example.com", "operator", last_verified=old),
        ])
        result = svc.verify_escalation_path()
        assert result["healthy"] is False
        assert result["contacts"][0]["stale"] is True


# ── Default Anomaly Rules ────────────────────────────────────────────


class TestAnomalyRules:
    def test_five_builtin_rules(self, svc: SecurityService):
        rules = svc.list_rules()
        assert len(rules) == 5
        names = {r.name for r in rules}
        assert "mass_publish" in names
        assert "unusual_ec_access" in names

    def test_add_custom_rule(self, svc: SecurityService):
        svc.add_rule(AnomalyRule("custom_rule", "test", "always", ThreatLevel.LOW))
        assert len(svc.list_rules()) == 6

    def test_replace_same_name(self, svc: SecurityService):
        svc.add_rule(AnomalyRule("mass_publish", "updated", "new trigger", ThreatLevel.LOW))
        rules = svc.list_rules()
        assert len(rules) == 5  # same count, replaced
        mp = [r for r in rules if r.name == "mass_publish"][0]
        assert mp.description == "updated"


# ── Security Status ──────────────────────────────────────────────────


class TestSecurityStatus:
    def test_healthy_no_alerts_valid_escalation(self, svc: SecurityService):
        now = datetime.now(UTC).isoformat()
        svc.set_escalation_contacts([
            EscalationContact("Op", "op@example.com", "operator", last_verified=now),
        ])
        status = svc.get_security_status()
        assert status["healthy"] is True
        assert status["open_alerts"] == 0

    def test_unhealthy_with_open_alerts(self, svc: SecurityService):
        now = datetime.now(UTC).isoformat()
        svc.set_escalation_contacts([
            EscalationContact("Op", "op@example.com", "operator", last_verified=now),
        ])
        svc.verify_content(b"x", "bad", "", "evil-node")
        status = svc.get_security_status()
        assert status["healthy"] is False
        assert status["open_alerts"] == 1


# ── Federated Rule Sharing ───────────────────────────────────────────


class TestFederatedRuleSharing:
    def test_remote_rule_with_source(self, svc: SecurityService):
        svc.add_rule(AnomalyRule(
            "remote_check", "from partner", "some trigger",
            ThreatLevel.MEDIUM, source="partner-node-1",
        ))
        rules = svc.list_rules()
        remote = [r for r in rules if r.name == "remote_check"][0]
        assert remote.source == "partner-node-1"

    def test_rules_persist_across_restarts(self, tmp_path: Path):
        state = tmp_path / "sec"
        svc1 = SecurityService(state_dir=state)
        svc1.add_rule(AnomalyRule("persist_test", "d", "t", ThreatLevel.LOW, "remote"))
        svc2 = SecurityService(state_dir=state)
        names = {r.name for r in svc2.list_rules()}
        assert "persist_test" in names
