# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""WARDEN — Vega's federation-governance agent (skeleton + first feature).

WARDEN's first wired feature is `validate_transition`: a deterministic
gatekeeper for peer-state transitions. The legality matrix matches
`axiom.vega.federation.discovery.NodeState`; promotion never skips
stages and demotion always passes through QUARANTINED. Each verdict
appends to `~/.axi/agents/warden/verdicts.jsonl` for replay.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.paths import get_user_state_dir

# Canonical states matching axiom.vega.federation.discovery.NodeState
# (kept as strings here so the module loads without importing vega).
_STATES: frozenset[str] = frozenset(
    {
        "unknown",
        "discovered",
        "verified",
        "trusted",
        "federated",
        "unreachable",
        "leaving",
        "evicted",
        "quarantined",
        "revoked",
    }
)

# Linear promotion path. Skipping stages is a stage_skip verdict.
_PROMOTION_PATH: tuple[str, ...] = (
    "unknown",
    "discovered",
    "verified",
    "trusted",
    "federated",
)


@dataclass(frozen=True)
class WardenVerdict:
    approved: bool
    reason_code: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _verdict_log_path() -> Path:
    return get_user_state_dir() / "agents" / "warden" / "verdicts.jsonl"


class Warden:
    """Vega's federation-governance agent (deterministic v0)."""

    def validate_transition(
        self,
        *,
        from_state: str,
        to_state: str,
        evidence: dict[str, Any] | None = None,
        node_id: str | None = None,
    ) -> WardenVerdict:
        """Adjudicate a peer-state transition.

        Returns a `WardenVerdict` with a structured `reason_code` so
        callers can branch on the outcome without parsing prose. The
        verdict is also appended to the audit log on disk.
        """
        evidence = evidence or {}

        if from_state not in _STATES or to_state not in _STATES:
            verdict = WardenVerdict(
                approved=False,
                reason_code="unknown_state",
                detail=f"unknown state in transition {from_state!r} → {to_state!r}",
            )
            self._record(verdict, from_state, to_state, evidence, node_id)
            return verdict

        verdict = self._evaluate(from_state, to_state, evidence)
        self._record(verdict, from_state, to_state, evidence, node_id)
        return verdict

    def _evaluate(
        self, from_state: str, to_state: str, evidence: dict[str, Any]
    ) -> WardenVerdict:
        # Quarantine is legal from any active state
        if to_state == "quarantined" and from_state in (
            "discovered", "verified", "trusted", "federated"
        ):
            return WardenVerdict(approved=True, reason_code="quarantine_legal")

        # Revoke only from quarantine
        if to_state == "revoked":
            if from_state == "quarantined":
                return WardenVerdict(approved=True, reason_code="revoke_legal")
            return WardenVerdict(
                approved=False,
                reason_code="must_quarantine_first",
                detail="revoke requires the peer to be quarantined first",
            )

        # Recovery: quarantined → verified (with ceremony evidence)
        if from_state == "quarantined" and to_state == "verified":
            if not evidence.get("recovery_ceremony_id"):
                return WardenVerdict(
                    approved=False,
                    reason_code="missing_recovery_ceremony",
                    detail="recovery requires a ceremony id",
                )
            return WardenVerdict(approved=True, reason_code="recovery_legal")

        # Linear promotion: must move exactly one step along the path.
        if from_state in _PROMOTION_PATH and to_state in _PROMOTION_PATH:
            i = _PROMOTION_PATH.index(from_state)
            j = _PROMOTION_PATH.index(to_state)
            if j != i + 1:
                return WardenVerdict(
                    approved=False,
                    reason_code="stage_skip",
                    detail=f"{from_state} → {to_state} skips intermediate stages",
                )
            return self._check_promotion_predicate(to_state, evidence)

        return WardenVerdict(
            approved=False,
            reason_code="transition_not_allowed",
            detail=f"{from_state} → {to_state} is not a defined transition",
        )

    def _check_promotion_predicate(
        self, to_state: str, evidence: dict[str, Any]
    ) -> WardenVerdict:
        if to_state == "verified":
            if not evidence.get("public_key"):
                return WardenVerdict(
                    approved=False,
                    reason_code="missing_public_key",
                    detail="verified requires a fetched public key",
                )
            return WardenVerdict(approved=True, reason_code="transition_legal")

        if to_state == "trusted":
            score = evidence.get("trust_score")
            if score is None:
                return WardenVerdict(
                    approved=False,
                    reason_code="missing_trust_score",
                    detail="trusted requires a trust_score in evidence",
                )
            threshold = evidence.get("trust_threshold", 0.5)
            if score < threshold:
                return WardenVerdict(
                    approved=False,
                    reason_code="trust_below_threshold",
                    detail=f"trust_score {score} < threshold {threshold}",
                )
            return WardenVerdict(approved=True, reason_code="transition_legal")

        if to_state == "federated":
            if not evidence.get("cohort_membership_id"):
                return WardenVerdict(
                    approved=False,
                    reason_code="missing_cohort_membership",
                    detail="federated requires a cohort_membership_id",
                )
            return WardenVerdict(approved=True, reason_code="transition_legal")

        if to_state == "discovered":
            return WardenVerdict(approved=True, reason_code="transition_legal")

        return WardenVerdict(
            approved=False,
            reason_code="transition_not_allowed",
            detail=f"no predicate registered for promotion to {to_state}",
        )

    def _record(
        self,
        verdict: WardenVerdict,
        from_state: str,
        to_state: str,
        evidence: dict[str, Any],
        node_id: str | None,
    ) -> None:
        path = _verdict_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "node_id": node_id,
            "from_state": from_state,
            "to_state": to_state,
            "evidence_keys": sorted(evidence.keys()),
            **verdict.to_dict(),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
