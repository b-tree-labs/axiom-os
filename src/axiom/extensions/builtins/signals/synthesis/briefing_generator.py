# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Design Briefing Generator.

Creates narrative briefings from signal clusters for stakeholder communication.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class BriefingType(Enum):
    """Types of briefings that can be generated."""
    DAILY_STANDUP = "daily_standup"
    WEEKLY_SUMMARY = "weekly_summary"
    STAKEHOLDER_UPDATE = "stakeholder_update"
    DECISION_LOG = "decision_log"
    RISK_REPORT = "risk_report"
    PROGRESS_NARRATIVE = "progress_narrative"


class Audience(Enum):
    """Target audience for briefing personalization."""
    TEAM_LEAD = "team_lead"
    ENGINEER = "engineer"
    MANAGER = "manager"
    EXECUTIVE = "executive"
    EXTERNAL = "external"


@dataclass
class DesignBriefing:
    """A generated design briefing document."""
    briefing_id: str
    briefing_type: BriefingType
    audience: Audience
    title: str
    summary: str
    sections: list[dict]  # [{heading, content, citations}]
    prd_targets: list[str]
    signal_count: int
    time_range: tuple[str, str]  # (start_date, end_date)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def word_count(self) -> int:
        """Approximate word count."""
        total = len(self.summary.split())
        for section in self.sections:
            total += len(section.get("content", "").split())
        return total

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "briefing_id": self.briefing_id,
            "briefing_type": self.briefing_type.value,
            "audience": self.audience.value,
            "title": self.title,
            "summary": self.summary,
            "sections": self.sections,
            "prd_targets": self.prd_targets,
            "signal_count": self.signal_count,
            "time_range": {
                "start": self.time_range[0],
                "end": self.time_range[1],
            },
            "word_count": self.word_count,
            "created_at": self.created_at,
        }

    def to_markdown(self) -> str:
        """Render briefing as markdown."""
        lines = [
            f"# {self.title}",
            "",
            f"*Generated: {self.created_at[:10]} | Audience: {self.audience.value} | "
            f"Signals: {self.signal_count}*",
            "",
            "## Executive Summary",
            "",
            self.summary,
            "",
        ]

        for section in self.sections:
            lines.append(f"## {section['heading']}")
            lines.append("")
            lines.append(section["content"])
            lines.append("")

            if section.get("citations"):
                lines.append("*Sources:*")
                for cit in section["citations"]:
                    lines.append(f"- {cit}")
                lines.append("")

        return "\n".join(lines)


class BriefingGenerator:
    """Generates narrative briefings from signal clusters.

    Creates different briefing styles optimized for various audiences
    and communication needs.

    Usage:
        generator = BriefingGenerator()
        briefing = generator.generate(
            clusters=clusters,
            briefing_type=BriefingType.WEEKLY_SUMMARY,
            audience=Audience.MANAGER,
        )

        print(briefing.to_markdown())
    """

    # Word limits by audience type
    WORD_LIMITS = {
        Audience.EXECUTIVE: 300,
        Audience.MANAGER: 500,
        Audience.TEAM_LEAD: 750,
        Audience.ENGINEER: 1000,
        Audience.EXTERNAL: 400,
    }

    # Section templates by briefing type
    SECTION_TEMPLATES = {
        BriefingType.DAILY_STANDUP: [
            "Yesterday's Progress",
            "Today's Focus",
            "Blockers",
        ],
        BriefingType.WEEKLY_SUMMARY: [
            "Key Accomplishments",
            "Decisions Made",
            "Open Questions",
            "Next Week Focus",
        ],
        BriefingType.STAKEHOLDER_UPDATE: [
            "Progress Highlights",
            "Milestone Status",
            "Risk & Mitigation",
            "Upcoming Milestones",
        ],
        BriefingType.DECISION_LOG: [
            "Decisions This Period",
            "Rationale Summary",
            "Impact Assessment",
            "Pending Decisions",
        ],
        BriefingType.RISK_REPORT: [
            "Active Risks",
            "Mitigations in Progress",
            "New Risks Identified",
            "Risk Trends",
        ],
        BriefingType.PROGRESS_NARRATIVE: [
            "Where We Started",
            "What Changed",
            "Where We Are Now",
            "What's Next",
        ],
    }

    def __init__(self, output_path: Path | None = None):
        """Initialize generator.

        Args:
            output_path: Path to save briefings. Defaults to inbox/processed/briefings/
        """
        self.output_path = output_path or (
            Path(__file__).parent.parent / "inbox" / "processed" / "briefings"
        )

    def generate(
        self,
        clusters: list,  # list[SignalCluster]
        briefing_type: BriefingType,
        audience: Audience,
        title: str | None = None,
    ) -> DesignBriefing:
        """Generate a briefing from signal clusters.

        Args:
            clusters: SignalClusters to synthesize
            briefing_type: Type of briefing to generate
            audience: Target audience for personalization
            title: Custom title (auto-generated if None)

        Returns:
            DesignBriefing object
        """
        # Collect all signals
        all_signals = []
        prd_targets = set()
        for cluster in clusters:
            all_signals.extend(cluster.signals)
            if cluster.prd_target:
                prd_targets.add(cluster.prd_target)

        # Determine time range
        time_range = self._get_time_range(all_signals)

        # Generate title
        if not title:
            title = self._generate_title(briefing_type, time_range)

        # Generate summary
        summary = self._generate_summary(
            all_signals, briefing_type, audience
        )

        # Generate sections
        sections = self._generate_sections(
            all_signals, clusters, briefing_type, audience
        )

        briefing_id = f"briefing_{briefing_type.value}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        return DesignBriefing(
            briefing_id=briefing_id,
            briefing_type=briefing_type,
            audience=audience,
            title=title,
            summary=summary,
            sections=sections,
            prd_targets=list(prd_targets),
            signal_count=len(all_signals),
            time_range=time_range,
        )

    def _get_time_range(self, signals: list) -> tuple[str, str]:
        """Extract time range from signals."""
        if not signals:
            today = datetime.now().isoformat()[:10]
            return (today, today)

        timestamps = [s.timestamp for s in signals if hasattr(s, 'timestamp')]
        if not timestamps:
            today = datetime.now().isoformat()[:10]
            return (today, today)

        # Parse and sort
        sorted_ts = sorted(timestamps)
        return (sorted_ts[0][:10], sorted_ts[-1][:10])

    def _generate_title(
        self,
        briefing_type: BriefingType,
        time_range: tuple[str, str],
    ) -> str:
        """Generate briefing title."""
        type_names = {
            BriefingType.DAILY_STANDUP: "Daily Standup",
            BriefingType.WEEKLY_SUMMARY: "Weekly Design Summary",
            BriefingType.STAKEHOLDER_UPDATE: "Stakeholder Update",
            BriefingType.DECISION_LOG: "Decision Log",
            BriefingType.RISK_REPORT: "Risk Report",
            BriefingType.PROGRESS_NARRATIVE: "Progress Narrative",
        }

        type_name = type_names.get(briefing_type, "Briefing")

        if time_range[0] == time_range[1]:
            return f"{type_name} - {time_range[0]}"
        else:
            return f"{type_name} - {time_range[0]} to {time_range[1]}"

    def _generate_summary(
        self,
        signals: list,
        briefing_type: BriefingType,
        audience: Audience,
    ) -> str:
        """Generate executive summary.

        TODO: Replace with LLM call using prompts/briefing_v1
        """
        # Count by type
        type_counts: dict[str, int] = {}
        for sig in signals:
            sig_type = sig.signal_type
            type_counts[sig_type] = type_counts.get(sig_type, 0) + 1

        # Generate placeholder summary
        parts = []
        if type_counts.get("decision"):
            parts.append(f"{type_counts['decision']} decisions made")
        if type_counts.get("requirement"):
            parts.append(f"{type_counts['requirement']} requirements captured")
        if type_counts.get("question"):
            parts.append(f"{type_counts['question']} open questions")
        if type_counts.get("action_item"):
            parts.append(f"{type_counts['action_item']} action items")

        if parts:
            return f"This period: {', '.join(parts)}."
        else:
            return f"Synthesized from {len(signals)} signals."

    def _generate_sections(
        self,
        signals: list,
        clusters: list,
        briefing_type: BriefingType,
        audience: Audience,
    ) -> list[dict]:
        """Generate briefing sections.

        TODO: Replace with LLM call using prompts/briefing_v1
        """
        section_headings = self.SECTION_TEMPLATES.get(
            briefing_type,
            ["Summary", "Details", "Next Steps"],
        )

        sections = []
        word_limit = self.WORD_LIMITS.get(audience, 500)
        words_per_section = word_limit // len(section_headings)

        # Group signals by type for section allocation
        by_type: dict[str, list] = {}
        for sig in signals:
            sig_type = sig.signal_type
            if sig_type not in by_type:
                by_type[sig_type] = []
            by_type[sig_type].append(sig)

        for heading in section_headings:
            # Determine which signals map to this section
            relevant_signals = self._get_signals_for_section(heading, by_type)

            content = self._generate_section_content(
                heading, relevant_signals, words_per_section
            )

            citations = [
                f"{s.source}: {s.timestamp[:10]}"
                for s in relevant_signals[:3]  # Limit citations
            ]

            sections.append({
                "heading": heading,
                "content": content,
                "citations": citations,
            })

        return sections

    def _get_signals_for_section(
        self,
        heading: str,
        by_type: dict[str, list],
    ) -> list:
        """Map section heading to relevant signals."""
        heading_lower = heading.lower()

        if "decision" in heading_lower:
            return by_type.get("decision", [])
        elif "question" in heading_lower or "open" in heading_lower:
            return by_type.get("question", [])
        elif "progress" in heading_lower or "accomplishment" in heading_lower:
            return by_type.get("insight", []) + by_type.get("requirement", [])
        elif "blocker" in heading_lower or "risk" in heading_lower:
            return by_type.get("question", [])
        elif "next" in heading_lower or "focus" in heading_lower:
            return by_type.get("action_item", [])
        else:
            # Return all signals
            all_signals = []
            for sigs in by_type.values():
                all_signals.extend(sigs)
            return all_signals

    def _generate_section_content(
        self,
        heading: str,
        signals: list,
        word_limit: int,
    ) -> str:
        """Generate content for a single section.

        TODO: Replace with LLM synthesis
        """
        if not signals:
            return "No updates in this area."

        lines = []
        for sig in signals[:5]:  # Limit to 5 items per section
            excerpt = sig.content
            if len(excerpt.split()) > word_limit // 5:
                excerpt = " ".join(excerpt.split()[:word_limit // 5]) + "..."
            lines.append(f"- {excerpt}")

        return "\n".join(lines)

    def save_briefing(self, briefing: DesignBriefing, format: str = "both") -> dict[str, Path]:
        """Save briefing to file(s).

        Args:
            briefing: Briefing to save
            format: "json", "markdown", or "both"

        Returns:
            Dict of format -> filepath
        """
        self.output_path.mkdir(parents=True, exist_ok=True)

        paths = {}

        if format in ("json", "both"):
            json_path = self.output_path / f"{briefing.briefing_id}.json"
            json_path.write_text(json.dumps(briefing.to_dict(), indent=2))
            paths["json"] = json_path

        if format in ("markdown", "both"):
            md_path = self.output_path / f"{briefing.briefing_id}.md"
            md_path.write_text(briefing.to_markdown())
            paths["markdown"] = md_path

        return paths

    def generate_batch(
        self,
        clusters: list,
        audiences: list[Audience],
        briefing_type: BriefingType,
    ) -> list[DesignBriefing]:
        """Generate briefings for multiple audiences from same clusters.

        Args:
            clusters: SignalClusters to synthesize
            audiences: List of target audiences
            briefing_type: Type of briefing

        Returns:
            List of DesignBriefing objects
        """
        return [
            self.generate(clusters, briefing_type, audience)
            for audience in audiences
        ]
