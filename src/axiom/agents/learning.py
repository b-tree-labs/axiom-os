# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unified agent learning framework.

Every Axiom agent (RIVET, AXI, SCAN, Tidy, CURIO, PRESS, TRIAGE) learns patterns
from its operations. These patterns are:
1. Stored in the repo (.axi/agents/<agent>/patterns.json) for fast matching
2. Indexed in the RAG corpus as facts for searchability
3. Federated to other nodes via the knowledge pipeline
4. Scored by confidence (trust gradient: GREEN/YELLOW/RED)

Agent knowledge IS RAG knowledge. The pattern files are a materialized cache
optimized for fast pattern matching. The corpus is the source of truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path


class Confidence(Enum):
    RED = "red"  # new, unverified — needs human confirmation
    YELLOW = "yellow"  # worked once — apply but verify
    GREEN = "green"  # verified 3+ times — auto-apply


@dataclass
class LearnedPattern:
    """A pattern learned by an agent from its operations."""

    # Identity
    pattern_id: str  # unique hash
    agent: str  # rivet, secur-t, scan, tidy, etc.
    category: str  # failure, anomaly, extraction, health, etc.

    # Matching
    signature: str  # regex or keyword for fast matching
    description: str  # human-readable description

    # Resolution
    diagnosis: str  # what went wrong / what was detected
    resolution: str  # how to fix / what to do
    prevention: str = ""  # how to prevent next time

    # Trust
    confidence: Confidence = Confidence.RED
    verified_count: int = 0  # times this pattern was verified
    failed_count: int = 0  # times the resolution didn't work
    verified_by: list[str] = field(default_factory=list)  # node_ids that verified

    # Provenance
    source_node: str = ""  # which node learned this
    learned_at: str = ""
    last_used: str = ""

    # RAG integration
    corpus_fact_id: str = ""  # link to RAG corpus entry
    maturity: int = 0  # knowledge maturity level (0-5)

    def to_dict(self) -> dict:
        return {
            "pattern_id": self.pattern_id,
            "agent": self.agent,
            "category": self.category,
            "signature": self.signature,
            "description": self.description,
            "diagnosis": self.diagnosis,
            "resolution": self.resolution,
            "prevention": self.prevention,
            "confidence": self.confidence.value,
            "verified_count": self.verified_count,
            "failed_count": self.failed_count,
            "verified_by": self.verified_by,
            "source_node": self.source_node,
            "learned_at": self.learned_at,
            "last_used": self.last_used,
            "corpus_fact_id": self.corpus_fact_id,
            "maturity": self.maturity,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LearnedPattern:
        return cls(
            pattern_id=data["pattern_id"],
            agent=data["agent"],
            category=data.get("category", ""),
            signature=data.get("signature", ""),
            description=data.get("description", ""),
            diagnosis=data.get("diagnosis", ""),
            resolution=data.get("resolution", ""),
            prevention=data.get("prevention", ""),
            confidence=Confidence(data.get("confidence", "red")),
            verified_count=data.get("verified_count", 0),
            failed_count=data.get("failed_count", 0),
            verified_by=data.get("verified_by", []),
            source_node=data.get("source_node", ""),
            learned_at=data.get("learned_at", ""),
            last_used=data.get("last_used", ""),
            corpus_fact_id=data.get("corpus_fact_id", ""),
            maturity=data.get("maturity", 0),
        )

    def update_confidence(self) -> None:
        """Recompute confidence based on verification history."""
        old_confidence = self.confidence
        if self.verified_count >= 3 and len(self.verified_by) >= 2:
            self.confidence = Confidence.GREEN
            self.maturity = max(self.maturity, 3)  # multi-site validated
        elif self.verified_count >= 1:
            self.confidence = Confidence.YELLOW
            self.maturity = max(self.maturity, 1)
        else:
            self.confidence = Confidence.RED
            self.maturity = 0

        # Trace confidence transitions
        if self.confidence != old_confidence:
            try:
                from axiom.agents.promotion_tracer import get_tracer
                event = "MULTI_VERIFIED" if self.confidence == Confidence.GREEN else "VERIFIED"
                get_tracer().trace(
                    event=event,
                    pattern_id=self.pattern_id,
                    confidence=self.confidence.value,
                    details={"from": old_confidence.value, "verified_count": self.verified_count},
                )
            except Exception:
                pass

    def record_success(self, node_id: str = "") -> None:
        """Record that this pattern's resolution worked."""
        self.verified_count += 1
        self.last_used = datetime.now(UTC).isoformat()
        if node_id and node_id not in self.verified_by:
            self.verified_by.append(node_id)
        self.update_confidence()

    def record_failure(self) -> None:
        """Record that this pattern's resolution didn't work."""
        self.failed_count += 1
        self.last_used = datetime.now(UTC).isoformat()
        # Degrade confidence if failure rate is high
        if self.failed_count > self.verified_count:
            self.confidence = Confidence.RED


def generate_pattern_id(agent: str, signature: str) -> str:
    """Deterministic pattern ID from agent + signature."""
    h = hashlib.sha256(f"{agent}:{signature}".encode()).hexdigest()[:12]
    return f"{agent}-{h}"


class AgentKnowledgeStore:
    """Unified store for agent-learned patterns.

    Reads from two locations (repo patterns take precedence):
    1. Repo: .axi/agents/<agent>/patterns.json (shared via git)
    2. Local: ~/.axi/agents/<agent>/patterns.json (personal overrides)

    Writes go to BOTH locations. Repo patterns are committed and shared.
    Local patterns are personal until explicitly promoted.
    """

    def __init__(
        self,
        agent: str,
        repo_root: Path | None = None,
        local_dir_override: Path | None = None,
    ):
        self.agent = agent
        # When the caller passes `local_dir_override` (test isolation mode),
        # also bypass repo-root lookup so repo `.axi/agents/<agent>/`
        # patterns don't merge in unexpectedly.
        if local_dir_override is not None:
            self._repo_root = None
        else:
            self._repo_root = repo_root if repo_root is not None else self._find_repo_root()
        if local_dir_override is not None:
            self._local_dir = local_dir_override
        else:
            self._local_dir = Path.home() / ".axi" / "agents" / agent
        self._local_dir.mkdir(parents=True, exist_ok=True)
        self._local_file_override: Path | None = None

    def _find_repo_root(self) -> Path | None:
        """Walk up from cwd to find .git or .axi directory."""
        path = Path.cwd().resolve()
        while path != path.parent:
            if (path / ".git").exists() or (path / ".axi").exists():
                return path
            path = path.parent
        return None

    @property
    def _repo_patterns_file(self) -> Path | None:
        if self._repo_root is None:
            return None
        return self._repo_root / ".axi" / "agents" / self.agent / "patterns.json"

    @property
    def _local_patterns_file(self) -> Path:
        if self._local_file_override is not None:
            return self._local_file_override
        return self._local_dir / "patterns.json"

    def load(self) -> list[LearnedPattern]:
        """Load patterns from both repo and local, merged by ID."""
        patterns: dict[str, LearnedPattern] = {}

        # Load repo patterns first (shared baseline)
        repo_file = self._repo_patterns_file
        if repo_file and repo_file.exists():
            for p in self._read_patterns(repo_file):
                patterns[p.pattern_id] = p

        # Load local patterns (may override repo)
        if self._local_patterns_file.exists():
            for p in self._read_patterns(self._local_patterns_file):
                existing = patterns.get(p.pattern_id)
                if existing is None or p.verified_count > existing.verified_count:
                    patterns[p.pattern_id] = p

        return sorted(patterns.values(), key=lambda p: p.verified_count, reverse=True)

    def save(self, pattern: LearnedPattern) -> None:
        """Save a pattern to local store. Call promote() to push to repo."""
        patterns = {p.pattern_id: p for p in self.load()}
        patterns[pattern.pattern_id] = pattern
        self._write_patterns(self._local_patterns_file, list(patterns.values()))

    def promote_to_repo(self, pattern_id: str) -> bool:
        """Promote a local pattern to the repo (shared with team via git)."""
        repo_file = self._repo_patterns_file
        if repo_file is None:
            return False

        local_patterns = {p.pattern_id: p for p in self._read_patterns(self._local_patterns_file)}
        pattern = local_patterns.get(pattern_id)
        if pattern is None:
            # Also check already-loaded patterns (may be repo-only)
            all_patterns = {p.pattern_id: p for p in self.load()}
            pattern = all_patterns.get(pattern_id)
        if pattern is None:
            return False

        # Load repo patterns, add/update this one
        repo_file.parent.mkdir(parents=True, exist_ok=True)
        repo_patterns: dict[str, LearnedPattern] = {}
        if repo_file.exists():
            repo_patterns = {p.pattern_id: p for p in self._read_patterns(repo_file)}
        repo_patterns[pattern_id] = pattern
        self._write_patterns(repo_file, list(repo_patterns.values()))

        # Trace promotion
        try:
            from axiom.agents.promotion_tracer import get_tracer
            get_tracer().trace(
                event="PROMOTED",
                pattern_id=pattern_id,
                agent=self.agent,
                confidence=pattern.confidence.value,
                details={"to": "repo", "maturity": pattern.maturity},
            )
        except Exception:
            pass
        return True

    def learn(
        self,
        category: str,
        signature: str,
        description: str,
        diagnosis: str,
        resolution: str,
        prevention: str = "",
        auto_promote: bool = False,
    ) -> LearnedPattern:
        """Record a new learned pattern."""
        pattern = LearnedPattern(
            pattern_id=generate_pattern_id(self.agent, signature),
            agent=self.agent,
            category=category,
            signature=signature,
            description=description,
            diagnosis=diagnosis,
            resolution=resolution,
            prevention=prevention,
            confidence=Confidence.RED,
            learned_at=datetime.now(UTC).isoformat(),
        )
        self.save(pattern)

        # Trace learning
        try:
            from axiom.agents.promotion_tracer import get_tracer
            get_tracer().trace(
                event="LEARNED",
                pattern_id=pattern.pattern_id,
                agent=self.agent,
                confidence="red",
                details={"category": category, "signature": signature},
            )
        except Exception:
            pass

        if auto_promote:
            self.promote_to_repo(pattern.pattern_id)

        # Index in knowledge corpus
        self._index_in_corpus(pattern)

        return pattern

    def match(self, text: str) -> list[LearnedPattern]:
        """Find patterns matching the given text."""
        import re

        matches = []
        for pattern in self.load():
            try:
                if re.search(pattern.signature, text, re.IGNORECASE):
                    matches.append(pattern)
            except re.error:
                if pattern.signature.lower() in text.lower():
                    matches.append(pattern)
        return matches

    def verify(self, pattern_id: str, success: bool, node_id: str = "") -> None:
        """Record verification result for a pattern."""
        patterns = {p.pattern_id: p for p in self.load()}
        pattern = patterns.get(pattern_id)
        if pattern is None:
            return

        if success:
            pattern.record_success(node_id)
            # Auto-promote GREEN patterns to repo
            if pattern.confidence == Confidence.GREEN:
                self.promote_to_repo(pattern_id)
        else:
            pattern.record_failure()

        self.save(pattern)

    def _index_in_corpus(self, pattern: LearnedPattern) -> None:
        """Index a pattern as a fact in the knowledge corpus."""
        try:
            from axiom.vega.federation.knowledge_metrics import KnowledgeMetricsService

            svc = KnowledgeMetricsService()
            svc.record_event(
                "fact_added",
                fact_id=pattern.pattern_id,
                source=f"agent:{self.agent}",
                domain=f"agent_knowledge:{self.agent}",
                maturity=pattern.maturity,
                content=(f"{pattern.description}: {pattern.diagnosis}. Fix: {pattern.resolution}"),
            )
        except Exception:
            pass  # Knowledge metrics not available — that's OK

    @staticmethod
    def _read_patterns(path: Path) -> list[LearnedPattern]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [LearnedPattern.from_dict(d) for d in data]
        except (json.JSONDecodeError, KeyError, FileNotFoundError):
            return []

    @staticmethod
    def _write_patterns(path: Path, patterns: list[LearnedPattern]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [p.to_dict() for p in sorted(patterns, key=lambda p: p.verified_count, reverse=True)]
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_all_agent_patterns(
    repo_root: Path | None = None,
) -> dict[str, list[LearnedPattern]]:
    """Load patterns from all agents. Returns {agent_name: [patterns]}."""
    result: dict[str, list[LearnedPattern]] = {}

    # Scan repo .axi/agents/ for agent directories
    if repo_root is None:
        store = AgentKnowledgeStore("_probe")
        repo_root = store._repo_root

    if repo_root:
        agents_dir = repo_root / ".axi" / "agents"
        if agents_dir.is_dir():
            for agent_dir in sorted(agents_dir.iterdir()):
                if agent_dir.is_dir() and (agent_dir / "patterns.json").exists():
                    store = AgentKnowledgeStore(agent_dir.name, repo_root)
                    patterns = store.load()
                    if patterns:
                        result[agent_dir.name] = patterns

    # Also scan ~/.axi/agents/ for local-only agents
    local_agents = Path.home() / ".axi" / "agents"
    if local_agents.is_dir():
        for agent_dir in sorted(local_agents.iterdir()):
            if (
                agent_dir.is_dir()
                and agent_dir.name not in result
                and (agent_dir / "patterns.json").exists()
            ):
                store = AgentKnowledgeStore(agent_dir.name, repo_root)
                patterns = store.load()
                if patterns:
                    result[agent_dir.name] = patterns

    return result
