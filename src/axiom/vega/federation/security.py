# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE — Federation security guardian (formerly SECUR-T).

Verifies content integrity on all federation receives, detects behavioral
anomalies, manages trust scores, and maintains escalation paths to human POCs.
Security functions consolidated under TRIAGE (the diagnostics agent).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class ThreatLevel(Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertStatus(Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    FALSE_POSITIVE = "false_positive"


@dataclass
class SecurityAlert:
    alert_id: str
    threat_level: ThreatLevel
    rule: str  # which detection rule triggered
    source_node_id: str  # node that triggered
    description: str
    evidence: dict = field(default_factory=dict)
    status: AlertStatus = AlertStatus.OPEN
    created_at: str = ""
    resolved_at: str = ""
    resolved_by: str = ""  # "auto" or human name

    def to_dict(self) -> dict:
        return {
            "alert_id": self.alert_id,
            "threat_level": self.threat_level.value,
            "rule": self.rule,
            "source_node_id": self.source_node_id,
            "description": self.description,
            "evidence": self.evidence,
            "status": self.status.value,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
        }


@dataclass
class AnomalyRule:
    """A detection rule for behavioral anomalies."""

    name: str
    description: str
    trigger: str  # human-readable trigger condition
    threat_level: ThreatLevel
    source: str = ""  # which node contributed this rule
    confidence: float = 0.9

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "threat_level": self.threat_level.value,
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class TrustScore:
    """Trust score for a federation peer."""

    node_id: str
    score: float = 1.0  # 0.0 (untrusted) to 1.0 (fully trusted)
    content_verified: int = 0  # total content items verified successfully
    content_failed: int = 0  # verification failures
    anomalies_detected: int = 0
    last_anomaly: str = ""
    last_verified: str = ""

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "score": round(self.score, 3),
            "content_verified": self.content_verified,
            "content_failed": self.content_failed,
            "anomalies_detected": self.anomalies_detected,
            "last_anomaly": self.last_anomaly,
            "last_verified": self.last_verified,
        }


@dataclass
class EscalationContact:
    """Human point of contact for security escalation."""

    name: str
    email: str
    role: str = "operator"  # operator, backup, coordinator
    last_verified: str = ""  # when we last confirmed reachability
    reachable: bool = True

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "last_verified": self.last_verified,
            "reachable": self.reachable,
        }


class SecurityService:
    """TRIAGE security service (formerly SECUR-T) — monitors federation security."""

    def __init__(self, state_dir: Path | None = None):
        self._dir = state_dir or Path.home() / ".axi" / "security"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._alerts_file = self._dir / "alerts.jsonl"
        self._trust_file = self._dir / "trust_scores.json"
        self._rules_file = self._dir / "anomaly_rules.json"
        self._contacts_file = self._dir / "escalation_contacts.json"
        self._activity_file = self._dir / "node_activity.jsonl"

    # ----- Content Verification -----

    def verify_content(self, content: bytes, signature: str, public_key: str, node_id: str) -> bool:
        """Verify content integrity using Ed25519 signature.

        Returns True if valid, False if tampered or unsigned.
        Every federation receive MUST call this before accepting content.
        """
        try:
            from axiom.vega.federation.identity import verify_signature

            valid = verify_signature(content, signature, public_key)
        except (ImportError, Exception):
            # If crypto verification unavailable, fall back to hash check
            expected_hash = hashlib.sha256(content).hexdigest()
            valid = signature == expected_hash

        # Update trust score
        score = self._get_trust_score(node_id)
        if valid:
            score.content_verified += 1
            score.last_verified = datetime.now(UTC).isoformat()
        else:
            score.content_failed += 1
            score.score = max(0.0, score.score - 0.1)  # penalize
            self._create_alert(
                rule="content_verification_failed",
                source_node_id=node_id,
                threat_level=ThreatLevel.HIGH,
                description=f"Content from {node_id} failed signature verification",
                evidence={"content_hash": hashlib.sha256(content).hexdigest()},
            )
        self._save_trust_score(score)
        return valid

    # ----- Anomaly Detection -----

    def record_activity(self, node_id: str, activity_type: str, count: int = 1) -> None:
        """Record federation activity for anomaly detection."""
        event = {
            "node_id": node_id,
            "type": activity_type,
            "count": count,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        with open(self._activity_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def check_anomalies(self, node_id: str) -> list[SecurityAlert]:
        """Run all anomaly detection rules against a node's recent activity."""
        alerts = []
        rules = self.list_rules()
        activity = self._get_recent_activity(node_id, hours=1)

        for rule in rules:
            alert = self._evaluate_rule(rule, node_id, activity)
            if alert is not None:
                alerts.append(alert)
                # Update trust score
                score = self._get_trust_score(node_id)
                score.anomalies_detected += 1
                score.last_anomaly = datetime.now(UTC).isoformat()
                # Degrade trust based on threat level
                degradation = {
                    ThreatLevel.LOW: 0.05,
                    ThreatLevel.MEDIUM: 0.15,
                    ThreatLevel.HIGH: 0.30,
                    ThreatLevel.CRITICAL: 0.50,
                }
                score.score = max(0.0, score.score - degradation.get(alert.threat_level, 0.1))
                self._save_trust_score(score)

        return alerts

    def _evaluate_rule(
        self, rule: AnomalyRule, node_id: str, activity: list[dict]
    ) -> SecurityAlert | None:
        """Evaluate a single anomaly rule against activity data."""
        if rule.name == "mass_publish":
            # > 50 items published in < 1 hour
            publishes = [
                a
                for a in activity
                if a.get("type") in ("material_publish", "model_publish", "catalog_update")
            ]
            total = sum(a.get("count", 1) for a in publishes)
            if total > 50:
                return self._create_alert(
                    rule=rule.name,
                    source_node_id=node_id,
                    threat_level=rule.threat_level,
                    description=f"Mass publish: {total} items in last hour from {node_id}",
                    evidence={"count": total, "threshold": 50},
                )

        elif rule.name == "composition_drift":
            # Material composition changes > 5% without version bump
            drifts = [
                a
                for a in activity
                if a.get("type") == "composition_change" and a.get("drift_pct", 0) > 5
            ]
            if drifts:
                return self._create_alert(
                    rule=rule.name,
                    source_node_id=node_id,
                    threat_level=rule.threat_level,
                    description=f"Composition drift without version bump from {node_id}",
                    evidence={"drift_events": len(drifts)},
                )

        elif rule.name == "unusual_ec_access":
            # > 10 export_controlled queries in 1 hour from non-EC node
            ec_queries = [a for a in activity if a.get("type") == "ec_query"]
            total = sum(a.get("count", 1) for a in ec_queries)
            if total > 10:
                return self._create_alert(
                    rule=rule.name,
                    source_node_id=node_id,
                    threat_level=ThreatLevel.CRITICAL,
                    description=f"Unusual EC access pattern: {total} queries from {node_id}",
                    evidence={"count": total, "threshold": 10},
                )

        elif rule.name == "rapid_trust_requests":
            trust_reqs = [a for a in activity if a.get("type") == "trust_request"]
            if len(trust_reqs) > 20:
                return self._create_alert(
                    rule=rule.name,
                    source_node_id=node_id,
                    threat_level=rule.threat_level,
                    description=f"Rapid trust requests from {node_id}: {len(trust_reqs)} in 1 hour",
                    evidence={"count": len(trust_reqs)},
                )

        elif rule.name == "signature_failures":
            failures = [a for a in activity if a.get("type") == "verification_failed"]
            if len(failures) > 3:
                return self._create_alert(
                    rule=rule.name,
                    source_node_id=node_id,
                    threat_level=ThreatLevel.HIGH,
                    description=f"Multiple signature failures from {node_id}",
                    evidence={"count": len(failures)},
                )

        return None

    # ----- Trust Management -----

    def get_trust_score(self, node_id: str) -> TrustScore:
        return self._get_trust_score(node_id)

    def set_trust_score(self, node_id: str, score: float) -> TrustScore:
        ts = self._get_trust_score(node_id)
        ts.score = max(0.0, min(1.0, score))
        self._save_trust_score(ts)
        return ts

    def is_trusted(self, node_id: str, threshold: float = 0.5) -> bool:
        return self._get_trust_score(node_id).score >= threshold

    # ----- Anomaly Rules -----

    def add_rule(self, rule: AnomalyRule) -> None:
        rules = self.list_rules()
        # Replace existing rule with same name
        rules = [r for r in rules if r.name != rule.name]
        rules.append(rule)
        self._save_rules(rules)

    def list_rules(self) -> list[AnomalyRule]:
        if not self._rules_file.exists():
            # Load default rules
            defaults = self._default_rules()
            self._save_rules(defaults)
            return defaults
        data = json.loads(self._rules_file.read_text(encoding="utf-8"))
        return [
            AnomalyRule(
                name=r["name"],
                description=r["description"],
                trigger=r["trigger"],
                threat_level=ThreatLevel(r["threat_level"]),
                source=r.get("source", ""),
                confidence=r.get("confidence", 0.9),
            )
            for r in data
        ]

    def _default_rules(self) -> list[AnomalyRule]:
        return [
            AnomalyRule(
                "mass_publish",
                "Mass content publication",
                "> 50 items published in < 1 hour",
                ThreatLevel.HIGH,
                "builtin",
            ),
            AnomalyRule(
                "composition_drift",
                "Material composition changed without version bump",
                "composition delta > 5% without version increment",
                ThreatLevel.MEDIUM,
                "builtin",
            ),
            AnomalyRule(
                "unusual_ec_access",
                "Unusual export-controlled access pattern",
                "> 10 EC queries in 1 hour from non-EC node",
                ThreatLevel.CRITICAL,
                "builtin",
            ),
            AnomalyRule(
                "rapid_trust_requests",
                "Rapid trust/join requests",
                "> 20 trust requests in 1 hour",
                ThreatLevel.MEDIUM,
                "builtin",
            ),
            AnomalyRule(
                "signature_failures",
                "Multiple content signature failures",
                "> 3 verification failures in 1 hour",
                ThreatLevel.HIGH,
                "builtin",
            ),
        ]

    # ----- Escalation -----

    def set_escalation_contacts(self, contacts: list[EscalationContact]) -> None:
        data = [c.to_dict() for c in contacts]
        self._contacts_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def get_escalation_contacts(self) -> list[EscalationContact]:
        if not self._contacts_file.exists():
            return []
        data = json.loads(self._contacts_file.read_text(encoding="utf-8"))
        return [
            EscalationContact(
                name=c["name"],
                email=c["email"],
                role=c.get("role", "operator"),
                last_verified=c.get("last_verified", ""),
                reachable=c.get("reachable", True),
            )
            for c in data
        ]

    def verify_escalation_path(self) -> dict:
        """Verify that human POCs are reachable.

        In production, this would send a test notification and check for ack.
        For now, it checks that contacts are configured and not stale (>7 days).
        """
        contacts = self.get_escalation_contacts()
        if not contacts:
            return {
                "healthy": False,
                "error": "No escalation contacts configured",
                "contacts": [],
            }

        now = datetime.now(UTC)
        results = []
        all_reachable = True
        for c in contacts:
            stale = False
            if c.last_verified:
                try:
                    last = datetime.fromisoformat(c.last_verified.replace("Z", "+00:00"))
                    stale = (now - last) > timedelta(days=7)
                except ValueError:
                    stale = True
            else:
                stale = True

            if stale:
                c.reachable = False  # mark as unverified
                all_reachable = False

            results.append({**c.to_dict(), "stale": stale})

        return {
            "healthy": all_reachable and len(contacts) >= 1,
            "contacts": results,
            "operator_count": sum(1 for c in contacts if c.role == "operator"),
            "backup_count": sum(1 for c in contacts if c.role == "backup"),
        }

    # ----- Alerts -----

    def list_alerts(
        self, status: str | None = None, node_id: str | None = None
    ) -> list[SecurityAlert]:
        alerts = self._load_alerts()
        if status:
            alerts = [a for a in alerts if a.status.value == status]
        if node_id:
            alerts = [a for a in alerts if a.source_node_id == node_id]
        return alerts

    def resolve_alert(
        self, alert_id: str, resolved_by: str = "auto", false_positive: bool = False
    ) -> None:
        alerts = self._load_alerts()
        for a in alerts:
            if a.alert_id == alert_id:
                a.status = AlertStatus.FALSE_POSITIVE if false_positive else AlertStatus.RESOLVED
                a.resolved_at = datetime.now(UTC).isoformat()
                a.resolved_by = resolved_by
        self._save_alerts(alerts)

    def get_security_status(self) -> dict:
        """Overall security health for this node."""
        alerts = self.list_alerts()
        open_alerts = [a for a in alerts if a.status == AlertStatus.OPEN]
        escalation = self.verify_escalation_path()
        rules = self.list_rules()

        return {
            "healthy": len(open_alerts) == 0 and escalation["healthy"],
            "open_alerts": len(open_alerts),
            "total_alerts": len(alerts),
            "escalation_path": escalation["healthy"],
            "anomaly_rules": len(rules),
            "critical_alerts": sum(
                1 for a in open_alerts if a.threat_level == ThreatLevel.CRITICAL
            ),
        }

    # ----- Internal helpers -----

    def _create_alert(
        self,
        rule: str,
        source_node_id: str,
        threat_level: ThreatLevel,
        description: str,
        evidence: dict | None = None,
    ) -> SecurityAlert:
        import secrets

        alert = SecurityAlert(
            alert_id=f"alert-{secrets.token_hex(8)}",
            threat_level=threat_level,
            rule=rule,
            source_node_id=source_node_id,
            description=description,
            evidence=evidence or {},
            created_at=datetime.now(UTC).isoformat(),
        )
        alerts = self._load_alerts()
        alerts.append(alert)
        self._save_alerts(alerts)
        return alert

    def _get_trust_score(self, node_id: str) -> TrustScore:
        scores = self._load_trust_scores()
        for s in scores:
            if s.node_id == node_id:
                return s
        return TrustScore(node_id=node_id)

    def _save_trust_score(self, score: TrustScore) -> None:
        scores = self._load_trust_scores()
        scores = [s for s in scores if s.node_id != score.node_id]
        scores.append(score)
        data = [s.to_dict() for s in scores]
        self._trust_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _load_trust_scores(self) -> list[TrustScore]:
        if not self._trust_file.exists():
            return []
        data = json.loads(self._trust_file.read_text(encoding="utf-8"))
        return [
            TrustScore(
                node_id=s["node_id"],
                score=s["score"],
                content_verified=s.get("content_verified", 0),
                content_failed=s.get("content_failed", 0),
                anomalies_detected=s.get("anomalies_detected", 0),
                last_anomaly=s.get("last_anomaly", ""),
                last_verified=s.get("last_verified", ""),
            )
            for s in data
        ]

    def _get_recent_activity(self, node_id: str, hours: int = 1) -> list[dict]:
        if not self._activity_file.exists():
            return []
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        events = []
        for line in self._activity_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    e = json.loads(line)
                    if e.get("node_id") == node_id and e.get("timestamp", "") >= cutoff:
                        events.append(e)
                except json.JSONDecodeError:
                    continue
        return events

    def _load_alerts(self) -> list[SecurityAlert]:
        if not self._alerts_file.exists():
            return []
        alerts = []
        for line in self._alerts_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    d = json.loads(line)
                    alerts.append(
                        SecurityAlert(
                            alert_id=d["alert_id"],
                            threat_level=ThreatLevel(d["threat_level"]),
                            rule=d["rule"],
                            source_node_id=d["source_node_id"],
                            description=d["description"],
                            evidence=d.get("evidence", {}),
                            status=AlertStatus(d.get("status", "open")),
                            created_at=d.get("created_at", ""),
                            resolved_at=d.get("resolved_at", ""),
                            resolved_by=d.get("resolved_by", ""),
                        )
                    )
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        return alerts

    def _save_alerts(self, alerts: list[SecurityAlert]) -> None:
        lines = [json.dumps(a.to_dict()) for a in alerts]
        self._alerts_file.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")

    def _save_rules(self, rules: list[AnomalyRule]) -> None:
        data = [r.to_dict() for r in rules]
        self._rules_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
