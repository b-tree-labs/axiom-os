# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Agent pattern sharing — federate learned patterns between nodes.

Implements ShareableResource protocol for agent knowledge patterns.
When two nodes federate, their agent patterns merge (higher confidence wins).
"""

from __future__ import annotations

import json
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from axiom.agents.learning import AgentKnowledgeStore, LearnedPattern, load_all_agent_patterns


class AgentPatternResource:
    """ShareableResource implementation for agent learned patterns.

    Priority: 60 (above builtin seed patterns, below user overrides)
    """

    @property
    def resource_type(self) -> str:
        return "agent_patterns"

    def catalog(self) -> list[dict]:
        """Return metadata about available patterns by agent."""
        all_patterns = load_all_agent_patterns()
        return [
            {
                "agent": agent,
                "pattern_count": len(patterns),
                "green_count": sum(1 for p in patterns if p.confidence.value == "green"),
                "categories": sorted({p.category for p in patterns}),
            }
            for agent, patterns in sorted(all_patterns.items())
        ]

    def export_patterns(self, output: Path | None = None) -> Path:
        """Export all agent patterns as a .axiompack for federation sharing."""
        all_patterns = load_all_agent_patterns()

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            # Write patterns grouped by agent
            agents_dir = tmp_path / "agents"
            by_agent: dict[str, list[dict]] = {}
            for agent, patterns in all_patterns.items():
                by_agent[agent] = [p.to_dict() for p in patterns]

            for agent, pattern_dicts in by_agent.items():
                agent_dir = agents_dir / agent
                agent_dir.mkdir(parents=True, exist_ok=True)
                (agent_dir / "patterns.json").write_text(json.dumps(pattern_dicts, indent=2) + "\n")

            # Flatten for counting
            flat = [p for patterns in all_patterns.values() for p in patterns]

            # Write manifest
            manifest = {
                "content_type": "agent_patterns",
                "version": "1.0.0",
                "created_at": datetime.now(UTC).isoformat(),
                "agent_count": len(by_agent),
                "total_patterns": len(flat),
                "green_patterns": sum(1 for p in flat if p.confidence.value == "green"),
            }
            (tmp_path / "manifest.json").write_text(json.dumps(manifest, indent=2))

            # Create archive
            if output is None:
                output = Path.cwd() / "agent-patterns.axiompack"

            with tarfile.open(str(output), "w:gz") as tar:
                tar.add(str(tmp_path / "manifest.json"), arcname="manifest.json")
                for agent_dir in sorted(agents_dir.iterdir()):
                    if agent_dir.is_dir():
                        tar.add(
                            str(agent_dir / "patterns.json"),
                            arcname=f"agents/{agent_dir.name}/patterns.json",
                        )

            return output

    def import_patterns(self, pack_path: Path, *, merge: bool = True) -> dict:
        """Import agent patterns from a received .axiompack.

        Args:
            pack_path: Path to .axiompack file containing agent patterns
            merge: If True, merge with existing (higher confidence wins).
                   If False, replace.

        Returns:
            Summary of imported patterns.
        """
        imported: dict = {"agents": {}, "total": 0, "new": 0, "updated": 0}

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            with tarfile.open(str(pack_path), "r:gz") as tar:
                tar.extractall(tmp_path, filter="data")

            agents_dir = tmp_path / "agents"
            if not agents_dir.exists():
                return imported

            for agent_dir in sorted(agents_dir.iterdir()):
                if not agent_dir.is_dir():
                    continue

                patterns_file = agent_dir / "patterns.json"
                if not patterns_file.exists():
                    continue

                agent_name = agent_dir.name
                store = AgentKnowledgeStore(agent_name)
                existing = {p.pattern_id: p for p in store.load()}

                incoming = json.loads(patterns_file.read_text())
                agent_stats = {"new": 0, "updated": 0, "skipped": 0}

                for p_data in incoming:
                    p = LearnedPattern.from_dict(p_data)

                    if p.pattern_id in existing:
                        ex = existing[p.pattern_id]
                        if merge and p.verified_count > ex.verified_count:
                            store.save(p)
                            agent_stats["updated"] += 1
                        else:
                            agent_stats["skipped"] += 1
                    else:
                        store.save(p)
                        agent_stats["new"] += 1

                imported["agents"][agent_name] = agent_stats
                imported["total"] += agent_stats["new"] + agent_stats["updated"]
                imported["new"] += agent_stats["new"]
                imported["updated"] += agent_stats["updated"]

        return imported


def sync_patterns_with_peer(peer_patterns: list[dict], peer_node_id: str = "") -> dict:
    """Sync patterns received from a federation peer.

    Called when a peer pushes its catalog. Merges incoming patterns
    with local store (higher confidence wins).
    """
    result: dict = {"received": 0, "new": 0, "updated": 0, "agents": []}

    by_agent: dict[str, list[dict]] = {}
    for p_data in peer_patterns:
        agent = p_data.get("agent", "unknown")
        by_agent.setdefault(agent, []).append(p_data)

    for agent_name, patterns in by_agent.items():
        store = AgentKnowledgeStore(agent_name)
        existing = {p.pattern_id: p for p in store.load()}

        for p_data in patterns:
            p = LearnedPattern.from_dict(p_data)
            result["received"] += 1

            if p.pattern_id in existing:
                ex = existing[p.pattern_id]
                if p.verified_count > ex.verified_count:
                    # Merge verified_by lists
                    merged_by = list(set(ex.verified_by + p.verified_by))
                    p.verified_by = merged_by
                    p.verified_count = max(p.verified_count, ex.verified_count)
                    p.update_confidence()
                    store.save(p)
                    result["updated"] += 1
            else:
                store.save(p)
                result["new"] += 1

        result["agents"].append(agent_name)

    return result
