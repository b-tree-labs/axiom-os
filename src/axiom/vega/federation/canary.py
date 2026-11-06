# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Canary node protocol — detect, sandbox, smoke, attest, promote.

Canary nodes test new releases before the fleet upgrades. They:
1. Detect new versions on PyPI/pack server
2. Install in a sandbox venv
3. Run tiered smoke tests
4. Upgrade the main install (or rollback)
5. Sign and push attestations to federation peers

Fleet nodes collect attestations and evaluate them against their
local upgrade policy (quorum, OS diversity, profile matching).
"""

from __future__ import annotations

import json
import logging
import platform
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class CanaryConfig:
    name: str = ""
    check_interval: int = 900
    smoke_tier: int = 1
    packages: list[str] = field(default_factory=lambda: ["axiom-os-lm"])
    report_sinks: list[str] = field(default_factory=lambda: ["gossip"])
    rollback_on_failure: bool = True


@dataclass
class UpgradePolicy:
    channel: str = "stable"
    auto_upgrade: bool = True
    min_canary_attestations: int = 3
    require_os_diversity: bool = True
    require_python_diversity: bool = False
    require_matching_profile: bool = False
    silence_timeout_hours: int = 4
    max_edge_age_hours: int = 72


@dataclass
class CanaryAttestation:
    node_id: str
    canary_name: str
    version: str
    previous_version: str
    status: str  # "green", "red", "rollback"
    failure_reason: str = ""
    smoke_results: dict = field(default_factory=dict)
    upgrade_duration_seconds: int = 0
    os_family: str = ""
    os_version: str = ""
    python_version: str = ""
    infra_tier: str = ""
    federation_role: str = ""
    timestamp: str = ""
    nonce: str = ""
    signature: str = ""

    def __post_init__(self):
        if not self.os_family:
            self.os_family = platform.system().lower()
        if not self.python_version:
            self.python_version = f"{sys.version_info.major}.{sys.version_info.minor}"

    def signing_payload(self) -> bytes:
        d = asdict(self)
        d.pop("signature", None)
        return json.dumps(d, sort_keys=True, separators=(",", ":")).encode()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> CanaryAttestation:
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class PromotionDecision:
    promote: bool
    reason: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Smoke test registry
# ---------------------------------------------------------------------------


@dataclass
class SmokeTest:
    name: str
    tier: int
    fn: object


class SmokeRegistry:
    def __init__(self):
        self._tests: dict[int, list[SmokeTest]] = {}

    def test(self, tier: int, name: str):
        def decorator(fn):
            self._tests.setdefault(tier, []).append(SmokeTest(name=name, tier=tier, fn=fn))
            return fn

        return decorator

    def get_tier(self, tier: int) -> list[SmokeTest]:
        return self._tests.get(tier, [])

    def all_tiers(self) -> list[int]:
        return sorted(self._tests.keys())


# ---------------------------------------------------------------------------
# Promotion evaluator
# ---------------------------------------------------------------------------


class PromotionEvaluator:
    def __init__(
        self,
        policy: UpgradePolicy,
        os_family: str = "",
        infra_tier: str = "",
    ):
        self.policy = policy
        self.os_family = os_family or platform.system().lower()
        self.infra_tier = infra_tier

    def evaluate(
        self,
        version: str,
        attestations: list[CanaryAttestation],
    ) -> PromotionDecision:
        green = [a for a in attestations if a.status == "green"]
        red = [a for a in attestations if a.status in ("red", "rollback")]

        # Quorum check
        if len(green) < self.policy.min_canary_attestations:
            return PromotionDecision(
                promote=False,
                reason="insufficient_quorum",
                detail=f"{len(green)}/{self.policy.min_canary_attestations} green",
            )

        # OS diversity
        if self.policy.require_os_diversity:
            os_families = {a.os_family for a in green}
            if len(os_families) < 2:
                return PromotionDecision(
                    promote=False,
                    reason="insufficient_os_diversity",
                    detail=f"only {os_families}",
                )

        # Python diversity
        if self.policy.require_python_diversity:
            py_versions = {a.python_version.rsplit(".", 1)[0] for a in green}
            if len(py_versions) < 2:
                return PromotionDecision(
                    promote=False,
                    reason="insufficient_python_diversity",
                    detail=f"only {py_versions}",
                )

        # Profile match required
        if self.policy.require_matching_profile:
            matching_reds = [
                a for a in red if a.os_family == self.os_family and a.infra_tier == self.infra_tier
            ]
            if matching_reds:
                return PromotionDecision(
                    promote=False,
                    reason="profile_failure",
                    detail=f"{len(matching_reds)} failures on matching profile",
                )

        return PromotionDecision(promote=True, reason="quorum_met")


# ---------------------------------------------------------------------------
# Gossip attestation sink
# ---------------------------------------------------------------------------


class GossipSink:
    def __init__(self, state_dir: Path | None = None):
        self._dir = state_dir or Path.home() / ".axi" / "canary"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._file = self._dir / "attestations.json"

    def push(self, attestation: CanaryAttestation) -> None:
        state = self._load()
        state.setdefault(attestation.version, []).append(attestation.to_dict())
        self._save(state)

    def list_attestations(self, version: str) -> list[CanaryAttestation]:
        state = self._load()
        return [CanaryAttestation.from_dict(a) for a in state.get(version, [])]

    def _load(self) -> dict:
        if self._file.exists():
            return json.loads(self._file.read_text(encoding="utf-8"))
        return {}

    def _save(self, state: dict) -> None:
        self._file.write_text(json.dumps(state, indent=2), encoding="utf-8")
