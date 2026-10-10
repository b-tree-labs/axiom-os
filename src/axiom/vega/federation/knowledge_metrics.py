# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Knowledge observatory — velocity, accumulation, and impact metrics."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class KnowledgeVelocity:
    """How fast knowledge enters the system."""

    facts_per_day: float = 0
    facts_by_source: dict = field(default_factory=dict)  # source -> count
    promotion_rate: float = 0  # fraction promoted GREEN
    median_time_to_promote_hours: float = 0
    period_days: int = 30

    def to_dict(self) -> dict:
        return {
            "facts_per_day": round(self.facts_per_day, 1),
            "facts_by_source": self.facts_by_source,
            "promotion_rate": round(self.promotion_rate, 3),
            "median_time_to_promote_hours": round(self.median_time_to_promote_hours, 1),
            "period_days": self.period_days,
        }


@dataclass
class KnowledgeAccumulation:
    """What do we know?"""

    total_facts: int = 0
    by_maturity: dict = field(default_factory=dict)  # level -> count
    by_domain: dict = field(default_factory=dict)  # domain -> count
    coverage_gaps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total_facts": self.total_facts,
            "by_maturity": self.by_maturity,
            "by_domain": self.by_domain,
            "coverage_gaps": self.coverage_gaps,
        }


@dataclass
class KnowledgeImpact:
    """Is knowledge being used?"""

    retrievals_per_day: float = 0
    unique_facts_accessed: int = 0
    never_accessed: int = 0
    federation_facts_retrieved: int = 0
    federation_facts_cited: int = 0
    federation_unique_answers: int = 0  # THE killer metric
    self_sufficiency_rate: float = 0

    def to_dict(self) -> dict:
        return {
            "retrievals_per_day": round(self.retrievals_per_day, 1),
            "unique_facts_accessed": self.unique_facts_accessed,
            "never_accessed": self.never_accessed,
            "federation_facts_retrieved": self.federation_facts_retrieved,
            "federation_facts_cited": self.federation_facts_cited,
            "federation_unique_answers": self.federation_unique_answers,
            "self_sufficiency_rate": round(self.self_sufficiency_rate, 3),
        }


@dataclass
class KnowledgeReport:
    """Complete knowledge observatory snapshot."""

    velocity: KnowledgeVelocity
    accumulation: KnowledgeAccumulation
    impact: KnowledgeImpact
    generated_at: str = ""
    node_id: str = ""

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at or datetime.now(UTC).isoformat(),
            "node_id": self.node_id,
            "velocity": self.velocity.to_dict(),
            "accumulation": self.accumulation.to_dict(),
            "impact": self.impact.to_dict(),
        }


class KnowledgeMetricsService:
    """Computes knowledge observatory metrics from log data."""

    def __init__(self, logs_dir: Path | None = None):
        self._logs_dir = logs_dir or Path.home() / ".axi" / "logs"
        self._events_file = self._logs_dir / "knowledge_events.jsonl"

    def record_event(self, event_type: str, **kwargs) -> None:
        """Record a knowledge event for metrics computation."""
        self._logs_dir.mkdir(parents=True, exist_ok=True)
        event = {
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            **kwargs,
        }
        with open(self._events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")

    def compute_velocity(self, period_days: int = 30) -> KnowledgeVelocity:
        events = self._load_events(period_days)
        additions = [e for e in events if e.get("type") == "fact_added"]
        promotions = [e for e in events if e.get("type") == "fact_promoted"]

        facts_per_day = len(additions) / max(period_days, 1)
        by_source: dict[str, int] = {}
        for e in additions:
            src = e.get("source", "unknown")
            by_source[src] = by_source.get(src, 0) + 1

        promotion_rate = len(promotions) / max(len(additions), 1)

        return KnowledgeVelocity(
            facts_per_day=facts_per_day,
            facts_by_source=by_source,
            promotion_rate=promotion_rate,
            period_days=period_days,
        )

    def compute_accumulation(self) -> KnowledgeAccumulation:
        events = self._load_events(period_days=36500)  # all time
        facts: dict[str, dict] = {}
        for e in events:
            if e.get("type") == "fact_added":
                fid = e.get("fact_id", e.get("timestamp"))
                facts[fid] = {
                    "maturity": e.get("maturity", 0),
                    "domain": e.get("domain", "unknown"),
                }

        by_maturity: dict[str, int] = {}
        by_domain: dict[str, int] = {}
        for f in facts.values():
            m = str(f["maturity"])
            by_maturity[m] = by_maturity.get(m, 0) + 1
            d = f["domain"]
            by_domain[d] = by_domain.get(d, 0) + 1

        gaps = [d for d, c in by_domain.items() if c < 50]

        return KnowledgeAccumulation(
            total_facts=len(facts),
            by_maturity=by_maturity,
            by_domain=by_domain,
            coverage_gaps=sorted(gaps),
        )

    def compute_impact(self, period_days: int = 30) -> KnowledgeImpact:
        events = self._load_events(period_days)
        retrievals = [e for e in events if e.get("type") == "fact_retrieved"]

        unique_accessed = len({e.get("fact_id") for e in retrievals if e.get("fact_id")})
        fed_retrievals = [e for e in retrievals if e.get("source_type") == "federation"]
        fed_unique = len({e.get("query") for e in fed_retrievals if e.get("federation_only")})

        return KnowledgeImpact(
            retrievals_per_day=len(retrievals) / max(period_days, 1),
            unique_facts_accessed=unique_accessed,
            federation_facts_retrieved=len(fed_retrievals),
            federation_unique_answers=fed_unique,
            self_sufficiency_rate=(
                1.0
                if not retrievals
                else sum(1 for e in retrievals if e.get("answered")) / len(retrievals)
            ),
        )

    def generate_report(self, node_id: str = "") -> KnowledgeReport:
        return KnowledgeReport(
            velocity=self.compute_velocity(),
            accumulation=self.compute_accumulation(),
            impact=self.compute_impact(),
            generated_at=datetime.now(UTC).isoformat(),
            node_id=node_id,
        )

    def _load_events(self, period_days: int) -> list[dict]:
        if not self._events_file.exists():
            return []

        events = []
        for line in self._events_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events
