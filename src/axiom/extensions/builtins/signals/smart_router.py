# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Smart Router — LLM-powered signal-to-PRD relevance matching.

Extends rule-based routing with LLM inference to suggest signal relevance
to documents (especially PRDs) that haven't explicitly registered interest.

The LLM reads:
  1. Signal content (title, summary, context)
  2. PRD content (problem statement, goals, scope)

And returns:
  - Relevance score (0-1)
  - Reasoning (why this signal matters to this PRD)
  - Suggested action (inform, discuss, update_requirements, etc.)

Usage:
    from axiom.extensions.builtins.signals.smart_router import SmartRouter

    router = SmartRouter()
    matches = router.match_to_prds(signals)
    router.suggest(matches)  # Queue for human review
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.gateway import Gateway
from axiom.infra.state import atomic_write

from .models import Signal

_RUNTIME_DIR = _REPO_ROOT / "runtime"
PRD_DIR = _REPO_ROOT / "docs" / "prd"
SUGGESTIONS_FILE = _RUNTIME_DIR / "inbox" / "processed" / "prd_suggestions.json"


@dataclass
class PRDTarget:
    """A PRD document that can receive signals."""

    path: Path
    title: str
    status: str = "draft"  # draft, active, done
    problem_statement: str = ""
    goals: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    personas: list[str] = field(default_factory=list)

    # Content hash for cache invalidation
    content_hash: str = ""

    @property
    def doc_id(self) -> str:
        return self.path.stem

    def summary_for_llm(self) -> str:
        """Compact representation for LLM context window."""
        parts = [f"# {self.title}", f"Status: {self.status}"]
        if self.problem_statement:
            parts.append(f"Problem: {self.problem_statement}")
        if self.goals:
            parts.append(f"Goals: {', '.join(self.goals[:3])}")
        if self.scope:
            parts.append(f"Scope: {', '.join(self.scope[:5])}")
        if self.personas:
            parts.append(f"Users: {', '.join(self.personas)}")
        return "\n".join(parts)


@dataclass
class RelevanceMatch:
    """An LLM-suggested match between a signal and a PRD."""

    signal: Signal
    prd: PRDTarget
    relevance_score: float  # 0-1
    reasoning: str
    suggested_action: str  # inform, discuss, update_requirements, defer
    confidence: float = 0.0  # LLM's confidence in this match

    # Tracking
    suggested_at: str = ""
    reviewed: bool = False
    accepted: bool = False

    def __post_init__(self):
        if not self.suggested_at:
            self.suggested_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "signal_id": self.signal.signal_id,
            "signal_detail": self.signal.detail or self.signal.raw_text[:100],
            "signal_type": self.signal.signal_type,
            "prd_id": self.prd.doc_id,
            "prd_title": self.prd.title,
            "relevance_score": self.relevance_score,
            "reasoning": self.reasoning,
            "suggested_action": self.suggested_action,
            "confidence": self.confidence,
            "suggested_at": self.suggested_at,
            "reviewed": self.reviewed,
            "accepted": self.accepted,
        }


class SmartRouter:
    """LLM-powered signal-to-PRD relevance matching."""

    def __init__(self, prd_dir: Path = PRD_DIR):
        self.prd_dir = prd_dir
        self.gateway = Gateway()
        self.prds: dict[str, PRDTarget] = {}
        self.suggestions: list[dict] = []

        self._load_prds()
        self._load_suggestions()

    def _load_prds(self) -> None:
        """Scan PRD directory and parse PRD documents."""
        if not self.prd_dir.exists():
            return

        for path in self.prd_dir.glob("*.md"):
            if path.name.startswith(".") or path.name == "README.md":
                continue
            if "template" in path.name.lower():
                continue

            prd = self._parse_prd(path)
            if prd:
                self.prds[prd.doc_id] = prd

    def _parse_prd(self, path: Path) -> PRDTarget | None:
        """Extract key fields from a PRD markdown file."""
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

        lines = content.split("\n")

        # Extract title (first H1)
        title = path.stem.replace("-", " ").title()
        for line in lines:
            if line.startswith("# "):
                title = line[2:].strip()
                break

        # Extract status
        status = "draft"
        for line in lines:
            lower = line.lower()
            if "status:" in lower:
                if "active" in lower:
                    status = "active"
                elif "done" in lower or "complete" in lower:
                    status = "done"
                elif "draft" in lower:
                    status = "draft"
                break

        # Extract problem statement (section 2 or "Problem")
        problem = ""
        in_problem_section = False
        for line in lines:
            if "problem" in line.lower() and line.startswith("#"):
                in_problem_section = True
                continue
            if in_problem_section:
                if line.startswith("#"):
                    break
                if line.strip().startswith("-"):
                    problem += line.strip()[1:].strip() + " "
                elif line.strip():
                    problem += line.strip() + " "

        # Extract goals
        goals = []
        in_goals_section = False
        for line in lines:
            if "goal" in line.lower() and line.startswith("#"):
                in_goals_section = True
                continue
            if in_goals_section:
                if line.startswith("#"):
                    break
                if line.strip().startswith("-"):
                    goal = line.strip()[1:].strip()
                    if goal:
                        goals.append(goal)

        # Extract scope/capabilities
        scope = []
        in_scope_section = False
        for line in lines:
            if ("scope" in line.lower() or "capabilit" in line.lower()) and line.startswith("#"):
                in_scope_section = True
                continue
            if in_scope_section:
                if line.startswith("#"):
                    break
                # Match numbered items: "1. Something"
                stripped = line.strip()
                if stripped and stripped[0].isdigit() and "." in stripped[:3]:
                    scope.append(stripped.split(".", 1)[1].strip())
                elif stripped.startswith("-"):
                    scope.append(stripped[1:].strip())

        # Extract personas
        personas = []
        in_users_section = False
        for line in lines:
            if ("user" in line.lower() or "persona" in line.lower()) and line.startswith("#"):
                in_users_section = True
                continue
            if in_users_section:
                if line.startswith("#"):
                    break
                if line.strip().startswith("-"):
                    personas.append(line.strip()[1:].strip())

        from axiom.infra.hash_utils import LONG, fingerprint
        content_hash = fingerprint(content, length=LONG)

        return PRDTarget(
            path=path,
            title=title,
            status=status,
            problem_statement=problem.strip()[:500],
            goals=goals[:5],
            scope=scope[:10],
            personas=personas[:5],
            content_hash=content_hash,
        )

    def _load_suggestions(self) -> None:
        """Load persisted suggestions."""
        if SUGGESTIONS_FILE.exists():
            try:
                self.suggestions = json.loads(SUGGESTIONS_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                self.suggestions = []

    def _save_suggestions(self) -> None:
        """Persist suggestions to disk."""
        SUGGESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(SUGGESTIONS_FILE, self.suggestions)

    def match_to_prds(
        self,
        signals: list[Signal],
        prd_filter: list[str] | None = None,
        min_relevance: float = 0.3,
    ) -> list[RelevanceMatch]:
        """Use LLM to match signals to relevant PRDs.

        Args:
            signals: Signals to route
            prd_filter: Optional list of PRD IDs to consider (None = all non-done)
            min_relevance: Minimum relevance score to include (0-1)

        Returns:
            List of RelevanceMatch objects sorted by relevance score
        """
        # Filter PRDs
        target_prds = []
        for prd_id, prd in self.prds.items():
            if prd_filter and prd_id not in prd_filter:
                continue
            if prd.status == "done":
                continue
            target_prds.append(prd)

        if not target_prds or not signals:
            return []

        matches = []

        # Batch signals for efficiency (avoid one LLM call per signal)
        for signal in signals:
            signal_matches = self._match_signal_to_prds(signal, target_prds)
            matches.extend([m for m in signal_matches if m.relevance_score >= min_relevance])

        # Sort by relevance
        matches.sort(key=lambda m: m.relevance_score, reverse=True)
        return matches

    def _match_signal_to_prds(
        self,
        signal: Signal,
        prds: list[PRDTarget],
    ) -> list[RelevanceMatch]:
        """Match a single signal against multiple PRDs."""
        # Build PRD summaries
        prd_summaries = []
        for i, prd in enumerate(prds):
            prd_summaries.append(f"[PRD-{i}] {prd.summary_for_llm()}")

        prompt = f"""Analyze this signal and determine its relevance to each PRD.

## Signal
Type: {signal.signal_type}
Detail: {signal.detail or '(none)'}
People: {', '.join(signal.people) if signal.people else 'N/A'}
Raw content: {signal.raw_text[:500] if signal.raw_text else 'N/A'}

## PRDs to Consider
{chr(10).join(prd_summaries)}

## Task
For each PRD, determine:
1. Relevance score (0.0 to 1.0) - how relevant is this signal to shaping the PRD?
2. Reasoning - why is this relevant (or not)?
3. Suggested action - what should happen?
   - "none" - not relevant
   - "inform" - stakeholders should be aware
   - "discuss" - warrants discussion
   - "update_requirements" - may change PRD scope/requirements
   - "validate" - confirms or challenges assumptions

Respond in JSON:
{{
  "matches": [
    {{
      "prd_index": 0,
      "relevance": 0.75,
      "reasoning": "...",
      "action": "inform",
      "confidence": 0.8
    }}
  ]
}}

Only include PRDs with relevance > 0.1. Be selective - not every signal is relevant."""

        system = """You are a product manager analyzing signals (user feedback, team updates, decisions)
for relevance to Product Requirements Documents (PRDs). Be precise and selective - only flag
genuine relevance, not tangential connections. Consider:
- Does this signal affect the problem the PRD is solving?
- Does it impact the goals or success metrics?
- Does it change scope or requirements?
- Does it provide user/stakeholder insight relevant to this feature?"""

        # T0-4: structured_output replaces prompt+regex JSON extraction.
        from axiom.infra.structured_output import (
            SchemaValidationError,
            structured_output,
        )

        schema = {
            "type": "object",
            "properties": {
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "prd_index": {"type": "integer"},
                            "relevance": {"type": "number"},
                            "reasoning": {"type": "string"},
                            "action": {"type": "string"},
                            "confidence": {"type": "number"},
                        },
                        "required": [
                            "prd_index", "relevance", "reasoning",
                            "action", "confidence",
                        ],
                    },
                },
            },
            "required": ["matches"],
        }

        try:
            result = structured_output(
                gateway=self.gateway,
                schema=schema,
                messages=[{"role": "user", "content": prompt}],
                system=system,
                tool_name="emit_prd_matches",
                task="extraction",
                max_tokens=2048,
            )
        except SchemaValidationError:
            # Preserve defensive behavior: on any failure, return empty
            # matches — caller treats as "no routing signal".
            return []

        matches = []
        for m in result.value.get("matches", []):
            prd_idx = m.get("prd_index", 0)
            if prd_idx >= len(prds):
                continue
            matches.append(RelevanceMatch(
                signal=signal,
                prd=prds[prd_idx],
                relevance_score=float(m.get("relevance", 0)),
                reasoning=m.get("reasoning", ""),
                suggested_action=m.get("action", "none"),
                confidence=float(m.get("confidence", 0.5)),
            ))
        return matches

    def suggest(self, matches: list[RelevanceMatch]) -> int:
        """Queue matches as suggestions for human review.

        Returns number of new suggestions added.
        """
        added = 0
        existing_keys = {
            (s["signal_id"], s["prd_id"])
            for s in self.suggestions
        }

        for match in matches:
            key = (match.signal.signal_id, match.prd.doc_id)
            if key not in existing_keys:
                self.suggestions.append(match.to_dict())
                existing_keys.add(key)
                added += 1

        if added:
            self._save_suggestions()

        return added

    def get_pending_suggestions(self, prd_id: str | None = None) -> list[dict]:
        """Get unreviewed suggestions, optionally filtered by PRD."""
        pending = [s for s in self.suggestions if not s.get("reviewed", False)]
        if prd_id:
            pending = [s for s in pending if s.get("prd_id") == prd_id]
        return pending

    def accept_suggestion(self, signal_id: str, prd_id: str) -> bool:
        """Mark a suggestion as accepted."""
        for s in self.suggestions:
            if s["signal_id"] == signal_id and s["prd_id"] == prd_id:
                s["reviewed"] = True
                s["accepted"] = True
                self._save_suggestions()
                return True
        return False

    def reject_suggestion(self, signal_id: str, prd_id: str) -> bool:
        """Mark a suggestion as rejected."""
        for s in self.suggestions:
            if s["signal_id"] == signal_id and s["prd_id"] == prd_id:
                s["reviewed"] = True
                s["accepted"] = False
                self._save_suggestions()
                return True
        return False

    def status(self) -> dict:
        """Get smart router status."""
        pending = [s for s in self.suggestions if not s.get("reviewed")]
        accepted = [s for s in self.suggestions if s.get("accepted")]

        # PRD stats
        prd_counts = {}
        for s in pending:
            prd_id = s.get("prd_id", "unknown")
            prd_counts[prd_id] = prd_counts.get(prd_id, 0) + 1

        return {
            "prds_loaded": len(self.prds),
            "prds_active": len([p for p in self.prds.values() if p.status != "done"]),
            "total_suggestions": len(self.suggestions),
            "pending_review": len(pending),
            "accepted": len(accepted),
            "by_prd": prd_counts,
        }


def get_smart_router() -> SmartRouter:
    """Get singleton smart router instance."""
    return SmartRouter()
