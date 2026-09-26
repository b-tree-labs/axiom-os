# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRD-based signal clustering for Sense pipeline.

Groups signals by target PRD, then by theme within each PRD.

Note: Uses Signal.initiatives[0] as the PRD target (maps initiatives -> PRDs).
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from axiom.infra.state import atomic_write

if TYPE_CHECKING:
    pass


@dataclass
class SignalCluster:
    """A cluster of related signals."""
    cluster_id: str
    prd_target: str | None
    theme: str | None
    signals: list  # list[Signal] - avoid circular import
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def people(self) -> list[str]:
        """Unique people across all signals in cluster."""
        all_people = []
        for sig in self.signals:
            for person in sig.people:
                if person not in all_people:
                    all_people.append(person)
        return all_people

    @property
    def signal_types(self) -> dict[str, int]:
        """Count of each signal type in cluster."""
        counts: dict[str, int] = {}
        for sig in self.signals:
            counts[sig.signal_type] = counts.get(sig.signal_type, 0) + 1
        return counts

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "cluster_id": self.cluster_id,
            "prd_target": self.prd_target,
            "theme": self.theme,
            "signal_count": self.signal_count,
            "people": self.people,
            "signal_types": self.signal_types,
            "signals": [s.signal_id for s in self.signals],
            "created_at": self.created_at,
        }

    @staticmethod
    def get_prd_target(signal) -> str | None:
        """Extract PRD target from signal (handles initiatives list)."""
        if hasattr(signal, 'prd_target') and signal.prd_target:
            return signal.prd_target
        if hasattr(signal, 'initiatives') and signal.initiatives:
            return signal.initiatives[0]
        return None


class PRDClusterer:
    """Clusters signals by PRD target.

    Groups signals first by their prd_target field, then optionally
    sub-clusters by theme (people, signal type, topic).

    Usage:
        clusterer = PRDClusterer()
        clusters = clusterer.cluster_by_prd(signals)

        for cluster in clusters:
            print(f"{cluster.prd_target}: {cluster.signal_count} signals")
    """

    PRD_CODES = {
        "ops_log": "OPS",
        "experiment_manager": "EXP",
        "operator_dashboard": "OPDB",
        "researcher_dashboard": "RSDB",
    }

    def __init__(self, output_path: Path | None = None):
        """Initialize clusterer.

        Args:
            output_path: Path to save cluster data. Defaults to inbox/processed/clusters/
        """
        self.output_path = output_path or (
            Path(__file__).parent.parent / "inbox" / "processed" / "clusters"
        )

    def cluster_by_prd(self, signals: list) -> list[SignalCluster]:
        """Cluster signals by PRD target.

        Args:
            signals: List of signals to cluster

        Returns:
            List of SignalCluster objects, one per PRD (plus one for unassigned)
        """
        # Group by prd_target
        by_prd: dict[str | None, list] = {}

        for signal in signals:
            target = SignalCluster.get_prd_target(signal)
            if target not in by_prd:
                by_prd[target] = []
            by_prd[target].append(signal)

        # Create clusters
        clusters = []
        for prd_target, prd_signals in by_prd.items():
            cluster_id = self._generate_cluster_id(prd_target)
            clusters.append(SignalCluster(
                cluster_id=cluster_id,
                prd_target=prd_target,
                theme=None,  # Top-level PRD cluster
                signals=prd_signals,
            ))

        return clusters

    def cluster_by_theme(
        self,
        signals: list,
        strategy: str = "signal_type",
    ) -> list[SignalCluster]:
        """Sub-cluster signals by theme within a PRD.

        Args:
            signals: Signals (typically from one PRD cluster)
            strategy: Clustering strategy - "signal_type", "person", or "topic"

        Returns:
            List of themed sub-clusters
        """
        if strategy == "signal_type":
            return self._cluster_by_signal_type(signals)
        elif strategy == "person":
            return self._cluster_by_person(signals)
        elif strategy == "topic":
            return self._cluster_by_topic(signals)
        else:
            raise ValueError(f"Unknown clustering strategy: {strategy}")

    def _cluster_by_signal_type(self, signals: list) -> list[SignalCluster]:
        """Cluster by signal_type (requirement, decision, etc.)."""
        by_type: dict[str, list] = {}

        for signal in signals:
            sig_type = signal.signal_type
            if sig_type not in by_type:
                by_type[sig_type] = []
            by_type[sig_type].append(signal)

        clusters = []
        prd = SignalCluster.get_prd_target(signals[0]) if signals else None

        for sig_type, type_signals in by_type.items():
            cluster_id = self._generate_cluster_id(prd, sig_type)
            clusters.append(SignalCluster(
                cluster_id=cluster_id,
                prd_target=prd,
                theme=sig_type,
                signals=type_signals,
            ))

        return clusters

    def _cluster_by_person(self, signals: list) -> list[SignalCluster]:
        """Cluster by mentioned person."""
        by_person: dict[str, list] = {}

        for signal in signals:
            if signal.people:
                for person in signal.people:
                    if person not in by_person:
                        by_person[person] = []
                    by_person[person].append(signal)
            else:
                # Signals without people go to "unattributed"
                if "_unattributed" not in by_person:
                    by_person["_unattributed"] = []
                by_person["_unattributed"].append(signal)

        clusters = []
        prd = SignalCluster.get_prd_target(signals[0]) if signals else None

        for person, person_signals in by_person.items():
            cluster_id = self._generate_cluster_id(prd, person.replace(" ", "_").lower())
            clusters.append(SignalCluster(
                cluster_id=cluster_id,
                prd_target=prd,
                theme=person,
                signals=person_signals,
            ))

        return clusters

    def _cluster_by_topic(self, signals: list) -> list[SignalCluster]:
        """Cluster by topic using LLM or keyword extraction.

        TODO: Implement topic modeling
        """
        # For now, return single cluster
        prd = SignalCluster.get_prd_target(signals[0]) if signals else None
        return [SignalCluster(
            cluster_id=self._generate_cluster_id(prd, "all"),
            prd_target=prd,
            theme="all_topics",
            signals=signals,
        )]

    def _generate_cluster_id(self, prd: str | None, suffix: str | None = None) -> str:
        """Generate unique cluster ID."""
        parts = ["cluster"]
        if prd:
            parts.append(self.PRD_CODES.get(prd, prd))
        else:
            parts.append("UNASSIGNED")
        if suffix:
            parts.append(suffix)
        parts.append(datetime.now().strftime("%Y%m%d"))
        return "_".join(parts).lower()

    def save_clusters(self, clusters: list[SignalCluster]) -> Path:
        """Save clusters to JSON file.

        Args:
            clusters: List of clusters to save

        Returns:
            Path to saved file
        """
        self.output_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = self.output_path / f"clusters_{timestamp}.json"

        data = {
            "created_at": datetime.now().isoformat(),
            "cluster_count": len(clusters),
            "total_signals": sum(c.signal_count for c in clusters),
            "clusters": [c.to_dict() for c in clusters],
        }

        atomic_write(filepath, data)
        return filepath

    def load_clusters(self, filepath: Path, signals: list) -> list[SignalCluster]:
        """Load clusters from JSON file.

        Args:
            filepath: Path to cluster JSON
            signals: Full signal list for hydration

        Returns:
            List of hydrated SignalCluster objects
        """
        data = json.loads(filepath.read_text())

        # Build signal lookup
        signal_lookup = {s.signal_id: s for s in signals}

        clusters = []
        for cluster_data in data["clusters"]:
            cluster_signals = [
                signal_lookup[sid]
                for sid in cluster_data["signals"]
                if sid in signal_lookup
            ]
            clusters.append(SignalCluster(
                cluster_id=cluster_data["cluster_id"],
                prd_target=cluster_data["prd_target"],
                theme=cluster_data["theme"],
                signals=cluster_signals,
                created_at=cluster_data["created_at"],
            ))

        return clusters

    def get_cluster_summary(self, clusters: list[SignalCluster]) -> dict:
        """Generate summary of clusters.

        Args:
            clusters: List of clusters

        Returns:
            Summary dict with counts and breakdowns
        """
        summary = {
            "total_clusters": len(clusters),
            "total_signals": sum(c.signal_count for c in clusters),
            "by_prd": {},
            "unassigned_count": 0,
        }

        for cluster in clusters:
            if cluster.prd_target:
                if cluster.prd_target not in summary["by_prd"]:
                    summary["by_prd"][cluster.prd_target] = {
                        "signal_count": 0,
                        "people": [],
                        "signal_types": {},
                    }
                prd_data = summary["by_prd"][cluster.prd_target]
                prd_data["signal_count"] += cluster.signal_count
                for person in cluster.people:
                    if person not in prd_data["people"]:
                        prd_data["people"].append(person)
                for sig_type, count in cluster.signal_types.items():
                    prd_data["signal_types"][sig_type] = (
                        prd_data["signal_types"].get(sig_type, 0) + count
                    )
            else:
                summary["unassigned_count"] += cluster.signal_count

        return summary
