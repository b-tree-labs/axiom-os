# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Briefing Service — on-demand executive summaries of recent signals.

"Brief me" — Neut synthesizes signals into a concise executive update,
intelligently determining the time window based on:
- Last briefing delivered
- Last known consumption of synthesized data
- Conservative bias (assume user hasn't seen data unless confirmed)

Topic-Focused Briefings:
- "Brief me on Kevin" → signals mentioning Kevin
- "Brief me on Alpha" → signals about the Alpha initiative
- "Brief me on blockers" → all blockers across initiatives
- "Brief me on travel" → upcoming/recent travel signals
- "Brief me on tech" → technical signals (code, bugs, deploys)

The LLM determines what's relevant when the topic is ambiguous.

Usage:
    from axiom.extensions.builtins.signals.briefing import BriefingService

    briefer = BriefingService()
    brief = briefer.brief_me()
    print(brief.summary)

    # Topic-focused briefing
    brief = briefer.brief_me(topic="Kevin")
    brief = briefer.brief_me(topic="blockers")

    # Or with explicit time window
    brief = briefer.brief_me(since="2025-02-20T00:00:00")
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.state import LockedJsonFile
from axiom.infra.time_utils import time_ago

_RUNTIME_DIR = _REPO_ROOT / "runtime"
BRIEFING_STATE_PATH = _RUNTIME_DIR / "inbox" / "state" / "briefing_state.json"
PROCESSED_DIR = _RUNTIME_DIR / "inbox" / "processed"


class ConsumptionEvent(str, Enum):
    """Types of events that indicate user consumed synthesized data."""
    BRIEFING_DELIVERED = "briefing_delivered"      # User received a briefing
    BRIEFING_ACKNOWLEDGED = "briefing_acknowledged" # User explicitly acked
    SIGNALS_VIEWED = "signals_viewed"              # User viewed signal list
    SUGGESTION_ACCEPTED = "suggestion_accepted"    # User accepted a routing suggestion
    FEEDBACK_SUBMITTED = "feedback_submitted"      # User submitted feedback
    CHANGELOG_VIEWED = "changelog_viewed"          # User viewed changelog
    MANUAL_MARK = "manual_mark"                    # User manually marked as caught up


@dataclass
class ConsumptionRecord:
    """Record of user consuming synthesized data."""

    event_type: ConsumptionEvent
    timestamp: datetime
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type.value,
            "timestamp": self.timestamp.isoformat(),
            "details": self.details,
        }

    @classmethod
    def from_dict(cls, data: dict) -> ConsumptionRecord:
        return cls(
            event_type=ConsumptionEvent(data["event_type"]),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            details=data.get("details", {}),
        )


class BriefingTopic(str, Enum):
    """Built-in briefing topics."""
    GENERAL = "general"        # Default: all signals
    PEOPLE = "people"          # Signals about/from specific people
    INITIATIVES = "initiatives" # Project/initiative updates
    TECH = "tech"              # Technical signals
    ROADMAPS = "roadmaps"      # Timeline/milestone signals
    CONFERENCES = "conferences" # Conference/talk signals
    TRAVEL = "travel"          # Travel-related signals
    BLOCKERS = "blockers"      # Blockers and issues
    DECISIONS = "decisions"    # Decisions made
    MILESTONES = "milestones"  # Milestones reached
    LONG_RUNNING = "long_running"  # Auto-detected recurring topics


# Keywords for topic matching (fallback when RAG unavailable)
TOPIC_KEYWORDS = {
    BriefingTopic.PEOPLE: [
        "meeting with", "talked to", "spoke with", "said", "mentioned",
        "asked", "told", "email from", "call with", "1:1", "sync with",
        "kevin", "andres", "ben",  # Add known people names
    ],
    BriefingTopic.TECH: [
        "code", "bug", "feature", "api", "database", "server", "deploy",
        "git", "merge", "branch", "test", "ci/cd", "docker", "python",
        "model", "simulation", "thermal", "hydraulics", "neutronics",
        "sam", "moose", "openmc", "error", "fix", "implementation",
    ],
    BriefingTopic.INITIATIVES: [
        "initiative", "project", "prd", "milestone", "deliverable",
        "phase", "sprint", "alpha", "beta", "digital twin", "loop",
    ],
    BriefingTopic.ROADMAPS: [
        "roadmap", "timeline", "schedule", "deadline", "due", "milestone",
        "q1", "q2", "q3", "q4", "planning", "backlog", "priority",
    ],
    BriefingTopic.CONFERENCES: [
        "conference", "symposium", "workshop", "presentation", "poster",
        "abstract", "paper", "ans", "nureth", "physor", "talk", "keynote",
    ],
    BriefingTopic.TRAVEL: [
        "travel", "trip", "flight", "hotel", "visit", "onsite",
        "austin", "chicago", "airport", "conference",
    ],
    BriefingTopic.BLOCKERS: [
        "blocked", "blocker", "waiting", "stuck", "issue", "problem",
        "can't", "unable", "dependency", "need", "missing",
    ],
    BriefingTopic.DECISIONS: [
        "decided", "decision", "approved", "rejected", "chose", "selected",
        "go with", "agreed", "consensus", "sign off",
    ],
    BriefingTopic.MILESTONES: [
        "milestone", "completed", "finished", "shipped", "launched",
        "released", "deployed", "delivered", "achieved",
    ],
}


@dataclass
class Briefing:
    """A generated briefing."""

    briefing_id: str
    generated_at: datetime
    time_window_start: datetime
    time_window_end: datetime

    # Content
    summary: str  # The executive summary
    signal_count: int
    signals_by_type: dict[str, int] = field(default_factory=dict)
    key_signals: list[dict] = field(default_factory=list)  # Most important signals

    # Topic info
    topic: str = "general"  # What topic this briefing is about
    topic_query: str = ""   # The original query if topic-focused

    # Metadata
    confidence: float = 0.8  # How confident Neut is in the time window
    time_window_reason: str = ""  # Why this window was chosen

    def to_dict(self) -> dict:
        return {
            "briefing_id": self.briefing_id,
            "generated_at": self.generated_at.isoformat(),
            "time_window_start": self.time_window_start.isoformat(),
            "time_window_end": self.time_window_end.isoformat(),
            "summary": self.summary,
            "signal_count": self.signal_count,
            "signals_by_type": self.signals_by_type,
            "key_signals": self.key_signals,
            "topic": self.topic,
            "topic_query": self.topic_query,
            "confidence": self.confidence,
            "time_window_reason": self.time_window_reason,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Briefing:
        return cls(
            briefing_id=data["briefing_id"],
            generated_at=datetime.fromisoformat(data["generated_at"]),
            time_window_start=datetime.fromisoformat(data["time_window_start"]),
            time_window_end=datetime.fromisoformat(data["time_window_end"]),
            summary=data.get("summary", ""),
            signal_count=data.get("signal_count", 0),
            signals_by_type=data.get("signals_by_type", {}),
            key_signals=data.get("key_signals", []),
            topic=data.get("topic", "general"),
            topic_query=data.get("topic_query", ""),
            confidence=data.get("confidence", 0.8),
            time_window_reason=data.get("time_window_reason", ""),
        )


@dataclass
class BriefingState:
    """Persistent state for the briefing service."""

    consumption_history: list[ConsumptionRecord] = field(default_factory=list)
    briefing_history: list[Briefing] = field(default_factory=list)

    # Settings
    default_lookback_hours: int = 24  # If no consumption history, look back this far
    max_lookback_days: int = 7        # Never look back further than this

    def to_dict(self) -> dict:
        return {
            "consumption_history": [c.to_dict() for c in self.consumption_history],
            "briefing_history": [b.to_dict() for b in self.briefing_history[-20:]],  # Keep last 20
            "default_lookback_hours": self.default_lookback_hours,
            "max_lookback_days": self.max_lookback_days,
        }

    @classmethod
    def from_dict(cls, data: dict) -> BriefingState:
        return cls(
            consumption_history=[
                ConsumptionRecord.from_dict(c) for c in data.get("consumption_history", [])
            ],
            briefing_history=[
                Briefing.from_dict(b) for b in data.get("briefing_history", [])
            ],
            default_lookback_hours=data.get("default_lookback_hours", 24),
            max_lookback_days=data.get("max_lookback_days", 7),
        )

    def last_consumption(self) -> ConsumptionRecord | None:
        """Get the most recent consumption event."""
        if not self.consumption_history:
            return None
        return max(self.consumption_history, key=lambda c: c.timestamp)

    def last_briefing(self) -> Briefing | None:
        """Get the most recent briefing."""
        if not self.briefing_history:
            return None
        return max(self.briefing_history, key=lambda b: b.generated_at)


class BriefingService:
    """Generates on-demand executive briefings."""

    def __init__(self, state_path: Path | None = None):
        self.state_path = state_path or BRIEFING_STATE_PATH
        self.state = self._load_state()

    def _load_state(self) -> BriefingState:
        """Load state from disk."""
        if self.state_path.exists():
            data: dict = {}
            try:
                with LockedJsonFile(self.state_path) as f:
                    data = f.read()
                return BriefingState.from_dict(data)
            except (json.JSONDecodeError, KeyError):
                pass
        return BriefingState()

    def _save_state(self) -> None:
        """Persist state to disk."""
        with LockedJsonFile(self.state_path, exclusive=True) as f:
            f.write(self.state.to_dict())

    def record_consumption(
        self,
        event_type: ConsumptionEvent,
        details: dict | None = None,
    ) -> None:
        """Record that the user consumed synthesized data."""
        record = ConsumptionRecord(
            event_type=event_type,
            timestamp=datetime.now(UTC),
            details=details or {},
        )
        self.state.consumption_history.append(record)

        # Prune old history (keep last 100)
        if len(self.state.consumption_history) > 100:
            self.state.consumption_history = self.state.consumption_history[-100:]

        self._save_state()

    def _determine_time_window(
        self,
        since: str | datetime | None = None,
    ) -> tuple[datetime, datetime, float, str]:
        """Determine the time window for the briefing.

        Returns: (start, end, confidence, reason)

        Conservative approach: bias toward assuming user hasn't seen data.
        """
        now = datetime.now(UTC)

        # If explicit 'since' provided, use it
        if since:
            if isinstance(since, str):
                start = _parse_since(since)
            else:
                start = since
            if start.tzinfo is None:
                start = start.replace(tzinfo=UTC)
            return (start, now, 1.0, "Explicit time window requested")

        # Check for acknowledged briefings (highest confidence)
        acked_briefings = [
            c for c in self.state.consumption_history
            if c.event_type == ConsumptionEvent.BRIEFING_ACKNOWLEDGED
        ]
        if acked_briefings:
            last_ack = max(acked_briefings, key=lambda c: c.timestamp)
            return (
                last_ack.timestamp,
                now,
                0.95,
                f"Since last acknowledged briefing ({time_ago(last_ack.timestamp, compact=False)})"
            )

        # Check for manual marks (also high confidence)
        manual_marks = [
            c for c in self.state.consumption_history
            if c.event_type == ConsumptionEvent.MANUAL_MARK
        ]
        if manual_marks:
            last_mark = max(manual_marks, key=lambda c: c.timestamp)
            return (
                last_mark.timestamp,
                now,
                0.90,
                f"Since you marked as caught up ({time_ago(last_mark.timestamp, compact=False)})"
            )

        # Check last briefing delivered (medium confidence - user may not have read it)
        last_briefing = self.state.last_briefing()
        if last_briefing:
            # Conservative: use slightly before briefing end time
            # (user may not have read it immediately)
            window_start = last_briefing.time_window_end
            return (
                window_start,
                now,
                0.7,
                f"Since last briefing was generated ({time_ago(last_briefing.generated_at, compact=False)}) — assuming you reviewed it"
            )

        # Check other consumption events (lower confidence)
        last_consumption = self.state.last_consumption()
        if last_consumption:
            return (
                last_consumption.timestamp,
                now,
                0.6,
                f"Since last activity ({last_consumption.event_type.value}, {time_ago(last_consumption.timestamp, compact=False)})"
            )

        # No history — use default lookback
        default_start = now - timedelta(hours=self.state.default_lookback_hours)
        return (
            default_start,
            now,
            0.5,
            f"No activity history — showing last {self.state.default_lookback_hours} hours"
        )

    def _load_signals_in_window(
        self,
        start: datetime,
        end: datetime,
    ) -> list[dict]:
        """Load signals from the given time window."""
        signals = []

        # Load from processed signal files
        for json_file in PROCESSED_DIR.glob("signals_*.json"):
            try:
                data = json.loads(json_file.read_text())
                for sig in data:
                    sig_time = datetime.fromisoformat(
                        sig.get("timestamp", "").replace("Z", "+00:00")
                    )
                    if start <= sig_time <= end:
                        signals.append(sig)
            except (json.JSONDecodeError, OSError, ValueError):
                continue

        # Sort by timestamp
        signals.sort(key=lambda s: s.get("timestamp", ""))
        return signals

    def _generate_summary(self, signals: list[dict], window_reason: str) -> str:
        """Generate an executive summary using LLM."""
        if not signals:
            return "No new signals to report. All quiet on the western front."

        # Build context for LLM
        signal_summaries = []
        for sig in signals[:20]:  # Limit to 20 for context window
            signal_summaries.append(
                f"- [{sig.get('signal_type', 'unknown')}] {sig.get('detail', sig.get('summary', 'No detail'))}"
                + (f" (Initiative: {sig.get('initiative', 'N/A')})" if sig.get('initiative') else "")
            )

        signals_text = "\n".join(signal_summaries)

        # Count by type
        type_counts: dict[str, int] = {}
        for sig in signals:
            sig_type = sig.get("signal_type", "unknown")
            type_counts[sig_type] = type_counts.get(sig_type, 0) + 1

        prompt = f"""You are an executive briefing assistant. Generate a concise briefing (2-3 short paragraphs) summarizing the following signals.

Time window context: {window_reason}
Total signals: {len(signals)}
Breakdown: {', '.join(f"{k}: {v}" for k, v in type_counts.items())}

Signals:
{signals_text}

Guidelines:
- Lead with the most important/actionable items
- Group related items together
- Use crisp, executive language
- Highlight decisions needed or blockers
- End with any emerging patterns or themes
- Do NOT use bullet points — write in prose paragraphs
- Be direct and assume the reader is busy

Generate the briefing:"""

        try:
            from axiom.infra.gateway import Gateway

            gateway = Gateway()
            response = gateway.complete(prompt, task="briefing")
            return response.text
        except Exception:
            # Fallback to simple summary
            return self._generate_fallback_summary(signals, type_counts)

    def _generate_fallback_summary(
        self,
        signals: list[dict],
        type_counts: dict[str, int],
    ) -> str:
        """Generate a simple summary without LLM."""
        lines = [f"**{len(signals)} signals captured.**"]

        if type_counts:
            breakdown = ", ".join(f"{v} {k}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1]))
            lines.append(f"Breakdown: {breakdown}.")

        # Highlight decisions and blockers
        decisions = [s for s in signals if s.get("signal_type") == "decision"]
        blockers = [s for s in signals if s.get("signal_type") == "blocker"]

        if decisions:
            lines.append(f"\n**Decisions ({len(decisions)}):** " + "; ".join(
                d.get("detail", "")[:100] for d in decisions[:3]
            ))

        if blockers:
            lines.append(f"\n**Blockers ({len(blockers)}):** " + "; ".join(
                b.get("detail", "")[:100] for b in blockers[:3]
            ))

        return "\n".join(lines)

    def _identify_key_signals(self, signals: list[dict]) -> list[dict]:
        """Identify the most important signals for highlighting."""
        # Priority order
        priority_types = ["decision", "blocker", "risk", "action_item", "milestone"]

        key = []
        for sig_type in priority_types:
            matching = [s for s in signals if s.get("signal_type") == sig_type]
            key.extend(matching[:2])  # Up to 2 of each high-priority type
            if len(key) >= 5:
                break

        return key[:5]

    def _detect_topic_category(self, topic: str) -> tuple[BriefingTopic | None, str]:
        """Detect if topic matches a built-in category.

        Returns (category, normalized_query) - category may be None for custom topics.
        """
        topic_lower = topic.lower().strip()

        # Direct category match
        for cat in BriefingTopic:
            if cat.value in (topic_lower, topic_lower + "s"):
                return (cat, topic)

        # Check if query contains category keywords
        for cat, keywords in TOPIC_KEYWORDS.items():
            if topic_lower in keywords:
                return (cat, topic)

        # T0-4: structured_output replaces prompt-then-strip-lowercase.
        try:
            from axiom.infra.gateway import Gateway
            from axiom.infra.structured_output import (
                SchemaValidationError,
                structured_output,
            )

            valid_categories = [c.value for c in BriefingTopic]
            schema = {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": valid_categories,
                    },
                },
                "required": ["category"],
            }
            system = (
                "Classify the user's briefing request into exactly one "
                "category. If it names a specific person, choose 'people'."
            )
            prompt = (
                "Query: " + topic + "\n\n"
                "Categories:\n"
                "  - people (about specific individuals)\n"
                "  - initiatives (projects, PRDs)\n"
                "  - tech (code, bugs, technical)\n"
                "  - roadmaps (timelines, milestones, planning)\n"
                "  - conferences (talks, papers, events)\n"
                "  - travel (trips, visits)\n"
                "  - blockers (issues, dependencies)\n"
                "  - decisions (choices made)\n"
                "  - milestones (achievements)\n"
                "  - general (doesn't fit above)\n\n"
                "Call emit_category with exactly one category."
            )
            gateway = Gateway()
            result = structured_output(
                gateway=gateway,
                schema=schema,
                messages=[{"role": "user", "content": prompt}],
                system=system,
                tool_name="emit_category",
                task="classification",
                max_tokens=128,
            )
            try:
                return (BriefingTopic(result.value["category"]), topic)
            except ValueError:
                pass
        except (SchemaValidationError, Exception):
            pass

        return (None, topic)

    def _filter_signals_by_topic(
        self,
        signals: list[dict],
        topic: str,
        category: BriefingTopic | None = None,
    ) -> list[dict]:
        """Filter signals relevant to a topic using RAG or keywords."""
        if not signals:
            return []

        # Try RAG first
        try:
            from .signal_rag import SignalRAG

            rag = SignalRAG()
            if rag.store.chunks:
                # Use RAG for semantic search
                results = rag.query(topic, top_k=50, min_relevance=0.25)
                relevant_ids = {r.chunk.signal_id for r in results}

                # Match back to original signals
                filtered = []
                for sig in signals:
                    sig_id = sig.get("signal_id", "")
                    # Also check by content hash
                    import hashlib
                    content = sig.get("detail", "") + sig.get("raw_text", "")[:200]
                    content_id = hashlib.sha256(content.encode()).hexdigest()[:16]

                    if sig_id in relevant_ids or content_id in relevant_ids:
                        filtered.append(sig)

                if filtered:
                    return filtered
        except Exception:
            pass  # Fall back to keyword matching

        # Keyword fallback
        topic_lower = topic.lower()
        keywords = [topic_lower]

        # Add category keywords if we have one
        if category and category in TOPIC_KEYWORDS:
            keywords.extend(TOPIC_KEYWORDS[category])

        filtered = []
        for sig in signals:
            text = (
                sig.get("detail", "") + " " +
                sig.get("raw_text", "")[:300] + " " +
                sig.get("summary", "") + " " +
                sig.get("initiative", "") + " " +
                " ".join(sig.get("initiatives", [])) + " " +
                " ".join(sig.get("people", [])) + " " +
                sig.get("signal_type", "")
            ).lower()

            if any(kw in text for kw in keywords):
                filtered.append(sig)

        return filtered

    def _generate_topic_summary(
        self,
        signals: list[dict],
        topic: str,
        category: BriefingTopic | None,
        window_reason: str,
    ) -> str:
        """Generate a topic-focused executive summary using LLM."""
        if not signals:
            return f"No signals found related to '{topic}'. Either nothing has been captured on this topic, or you may need to refine your query."

        # Build context for LLM
        signal_summaries = []
        for sig in signals[:25]:  # Limit for context window
            signal_summaries.append(
                f"- [{sig.get('signal_type', 'unknown')}] {sig.get('detail', sig.get('summary', 'No detail'))}"
                + (f" (Initiative: {sig.get('initiative', 'N/A')})" if sig.get('initiative') else "")
            )

        signals_text = "\n".join(signal_summaries)

        category_context = ""
        if category:
            category_context = f"\nTopic category: {category.value}"

        prompt = f"""You are an executive briefing assistant. Generate a focused briefing (2-3 paragraphs) about "{topic}".

Context: User asked to be briefed specifically about "{topic}".{category_context}
Time window: {window_reason}
Signals found: {len(signals)}

Relevant signals:
{signals_text}

Guidelines:
- Focus ONLY on information relevant to "{topic}"
- Lead with the most recent/important updates
- Note any patterns, changes, or emerging themes about this topic
- If the topic is a person, summarize what they've been working on or mentioned
- If the topic is an initiative, focus on progress, blockers, and next steps
- Use crisp, executive language in prose (no bullet points)
- If signals are sparse, acknowledge what's known and what's unclear

Generate the focused briefing:"""

        try:
            from axiom.infra.gateway import Gateway

            gateway = Gateway()
            response = gateway.complete(prompt, task="briefing")
            return response.text
        except Exception:
            return self._generate_fallback_summary(signals, {})

    def brief_me(
        self,
        since: str | datetime | None = None,
        topic: str | None = None,
        acknowledge: bool = False,
    ) -> Briefing:
        """Generate an executive briefing.

        Args:
            since: Optional explicit start time (ISO string or datetime)
            topic: Optional topic to focus on (person name, initiative, category)
            acknowledge: If True, record that user acknowledged this briefing

        Returns:
            Briefing object with summary and metadata
        """
        import hashlib

        # Determine time window
        start, end, confidence, reason = self._determine_time_window(since)

        # Cap at max lookback (extend for topic queries)
        max_days = self.state.max_lookback_days
        if topic:
            max_days = max_days * 2  # Allow longer lookback for focused queries

        max_start = end - timedelta(days=max_days)
        if start < max_start:
            start = max_start
            reason += f" (capped at {max_days} days)"
            confidence = min(confidence, 0.6)

        # Load signals
        signals = self._load_signals_in_window(start, end)

        # Filter by topic if provided
        category = None
        topic_query = ""
        if topic:
            category, topic_query = self._detect_topic_category(topic)
            signals = self._filter_signals_by_topic(signals, topic, category)
            topic_name = category.value if category else "custom"
        else:
            topic_name = "general"

        # Generate summary
        if topic:
            summary = self._generate_topic_summary(signals, topic, category, reason)
        else:
            summary = self._generate_summary(signals, reason)

        # Count by type
        type_counts: dict[str, int] = {}
        for sig in signals:
            sig_type = sig.get("signal_type", "unknown")
            type_counts[sig_type] = type_counts.get(sig_type, 0) + 1

        # Create briefing
        briefing_id = hashlib.sha256(
            f"{start.isoformat()}-{end.isoformat()}-{len(signals)}-{topic or ''}".encode()
        ).hexdigest()[:12]

        briefing = Briefing(
            briefing_id=briefing_id,
            generated_at=datetime.now(UTC),
            time_window_start=start,
            time_window_end=end,
            summary=summary,
            signal_count=len(signals),
            signals_by_type=type_counts,
            key_signals=self._identify_key_signals(signals),
            topic=topic_name,
            topic_query=topic_query,
            confidence=confidence,
            time_window_reason=reason,
        )

        # Record delivery
        self.state.briefing_history.append(briefing)
        self.record_consumption(
            ConsumptionEvent.BRIEFING_DELIVERED,
            {"briefing_id": briefing_id, "signal_count": len(signals), "topic": topic or "general"},
        )

        if acknowledge:
            self.record_consumption(
                ConsumptionEvent.BRIEFING_ACKNOWLEDGED,
                {"briefing_id": briefing_id},
            )

        # Record for echo suppression - prevents briefing content from being
        # re-extracted as new signals when read aloud in meetings
        try:
            from .echo_suppression import record_published
            from .models import Signal

            # Convert loaded signal dicts back to Signal objects for lineage tracking
            source_signals = [
                Signal.from_dict(s) if isinstance(s, dict) else s
                for s in signals[:50]  # Limit to first 50 for performance
            ]
            record_published(
                signals=source_signals,
                output_text=summary,
                content_id=f"briefing_{briefing_id}",
                content_type="briefing",
            )
        except ImportError:
            pass  # Echo suppression not available

        self._save_state()
        return briefing

    def brief_on_person(self, name: str, days: int = 14) -> Briefing:
        """Generate a briefing focused on a specific person."""
        since = datetime.now(UTC) - timedelta(days=days)
        return self.brief_me(since=since, topic=name)

    def brief_on_initiative(self, initiative: str, days: int = 30) -> Briefing:
        """Generate a briefing focused on an initiative/project."""
        since = datetime.now(UTC) - timedelta(days=days)
        return self.brief_me(since=since, topic=initiative)

    def get_available_topics(self) -> dict:
        """Get available topics for focused briefings."""
        # Built-in categories
        categories = [cat.value for cat in BriefingTopic if cat != BriefingTopic.GENERAL]

        # Detect long-running topics from signals
        detected_topics = []
        try:
            from .signal_rag import SignalRAG
            rag = SignalRAG()
            long_running = rag.detect_long_running_topics()
            detected_topics = [t.name for t in long_running[:10]]
        except Exception:
            pass

        # Extract initiatives from recent signals
        initiatives = set()
        for json_file in PROCESSED_DIR.glob("signals_*.json"):
            try:
                data = json.loads(json_file.read_text())
                for sig in data:
                    if sig.get("initiative"):
                        initiatives.add(sig["initiative"])
            except Exception:
                pass

        return {
            "categories": categories,
            "detected_topics": detected_topics,
            "initiatives": list(initiatives)[:15],
        }

    def acknowledge_briefing(self, briefing_id: str | None = None) -> bool:
        """Acknowledge that user has reviewed a briefing.

        This updates the consumption history so future briefings start
        from this point.
        """
        if briefing_id is None:
            # Acknowledge the most recent briefing
            last = self.state.last_briefing()
            if last:
                briefing_id = last.briefing_id
            else:
                return False

        self.record_consumption(
            ConsumptionEvent.BRIEFING_ACKNOWLEDGED,
            {"briefing_id": briefing_id},
        )
        return True

    def mark_caught_up(self, until: datetime | None = None) -> None:
        """Manually mark that user is caught up until a certain time.

        Use this when user has consumed data through other means
        (e.g., read the changelog directly).
        """
        until = until or datetime.now(UTC)
        self.record_consumption(
            ConsumptionEvent.MANUAL_MARK,
            {"until": until.isoformat()},
        )

    def status(self) -> dict:
        """Get briefing service status."""
        last_consumption = self.state.last_consumption()
        last_briefing = self.state.last_briefing()

        return {
            "last_consumption": {
                "type": last_consumption.event_type.value if last_consumption else None,
                "when": last_consumption.timestamp.isoformat() if last_consumption else None,
                "relative": time_ago(last_consumption.timestamp, compact=False) if last_consumption else None,
            } if last_consumption else None,
            "last_briefing": {
                "id": last_briefing.briefing_id if last_briefing else None,
                "when": last_briefing.generated_at.isoformat() if last_briefing else None,
                "signal_count": last_briefing.signal_count if last_briefing else 0,
                "relative": time_ago(last_briefing.generated_at, compact=False) if last_briefing else None,
            } if last_briefing else None,
            "briefings_generated": len(self.state.briefing_history),
            "consumption_events": len(self.state.consumption_history),
        }


def _parse_since(expr: str) -> datetime:
    """Parse a human-friendly time expression into a tz-aware UTC datetime.

    Supported formats:
        ISO:       2026-02-01, 2026-02-01T14:00, 2026-02-01T14:00:00+00:00
        Shorthand: 2d, 3h, 1w, 2m (days, hours, weeks, months)
        Words:     yesterday, today
        Phrases:   "2 days ago", "3 hours ago", "last week", "last month"
    """
    now = datetime.now(UTC)
    s = expr.strip().lower()

    # --- ISO passthrough ---
    try:
        dt = datetime.fromisoformat(expr.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        pass

    # --- Named keywords ---
    if s == "today":
        return datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    if s == "yesterday":
        yest = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        return yest.astimezone(UTC)

    # --- "last week" / "last month" ---
    if s == "last week":
        return now - timedelta(weeks=1)
    if s == "last month":
        return now - timedelta(days=30)

    # --- Shorthand: 2d, 3h, 1w, 2m/2mo ---
    m = re.fullmatch(r"(\d+)\s*(d|h|w|m|mo)", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - _unit_to_delta(n, unit)

    # --- "N days/hours/weeks/months ago" ---
    m = re.fullmatch(r"(\d+)\s+(days?|hours?|weeks?|months?)\s+ago", s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - _unit_to_delta(n, unit[0])  # first char: d/h/w/m

    raise ValueError(
        f"Unrecognized time expression: '{expr}'. "
        "Supported: ISO date (2026-02-01), shorthand (2d, 3h, 1w), "
        "words (yesterday, today), phrases ('2 days ago', 'last week')."
    )


def _unit_to_delta(n: int, unit: str) -> timedelta:
    """Convert a count and unit character to a timedelta."""
    if unit in ("d", "day", "days"):
        return timedelta(days=n)
    if unit in ("h", "hour", "hours"):
        return timedelta(hours=n)
    if unit in ("w", "week", "weeks"):
        return timedelta(weeks=n)
    if unit in ("m", "mo", "month", "months"):
        return timedelta(days=30 * n)
    raise ValueError(f"Unknown time unit: {unit}")



def get_briefing_service() -> BriefingService:
    """Get singleton briefing service instance."""
    return BriefingService()
