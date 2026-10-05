# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PRD Update Synthesizer.

Generates PRD section updates from signal clusters, with citations
and confidence scores.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from axiom.infra.state import atomic_write


class PRDSection(Enum):
    """Standard PRD sections that can be updated."""
    OVERVIEW = "overview"
    USER_STORIES = "user_stories"
    REQUIREMENTS = "requirements"
    DESIGN_DECISIONS = "design_decisions"
    OPEN_QUESTIONS = "open_questions"
    NON_GOALS = "non_goals"
    MILESTONES = "milestones"
    DEPENDENCIES = "dependencies"


class DraftStatus(Enum):
    """Status of a PRD update draft."""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    NEEDS_REVIEW = "needs_review"


@dataclass
class Citation:
    """Reference back to source signal."""
    signal_id: str
    source_type: str  # calendar, notes, etc.
    source_date: str
    excerpt: str  # Relevant excerpt from source


@dataclass
class PRDUpdateDraft:
    """A draft update to a PRD section."""
    draft_id: str
    prd_target: str
    section: PRDSection
    current_content: str | None
    proposed_content: str
    rationale: str
    citations: list[Citation]
    confidence_score: float  # 0.0 - 1.0
    status: DraftStatus = DraftStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    approved_by: str | None = None
    approved_at: str | None = None

    @property
    def citation_count(self) -> int:
        return len(self.citations)

    @property
    def is_high_confidence(self) -> bool:
        return self.confidence_score >= 0.8

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "draft_id": self.draft_id,
            "prd_target": self.prd_target,
            "section": self.section.value,
            "current_content": self.current_content,
            "proposed_content": self.proposed_content,
            "rationale": self.rationale,
            "citations": [
                {
                    "signal_id": c.signal_id,
                    "source_type": c.source_type,
                    "source_date": c.source_date,
                    "excerpt": c.excerpt,
                }
                for c in self.citations
            ],
            "confidence_score": self.confidence_score,
            "status": self.status.value,
            "created_at": self.created_at,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }

    def approve(self, approver: str) -> None:
        """Mark draft as approved."""
        self.status = DraftStatus.APPROVED
        self.approved_by = approver
        self.approved_at = datetime.now().isoformat()

    def reject(self) -> None:
        """Mark draft as rejected."""
        self.status = DraftStatus.REJECTED


class PRDUpdater:
    """Synthesizes PRD updates from signal clusters.

    Usage:
        updater = PRDUpdater()
        drafts = updater.generate_updates(cluster, prd_path)

        for draft in drafts:
            if draft.is_high_confidence:
                draft.approve("system")
            else:
                draft.status = DraftStatus.NEEDS_REVIEW
    """

    PRD_PATHS = {
        "ops_log": Path("docs/specs/ops_log_prd.md"),
        "experiment_manager": Path("docs/specs/experiment_manager_prd.md"),
        "operator_dashboard": Path("docs/specs/operator_dashboard_prd.md"),
        "researcher_dashboard": Path("docs/specs/researcher_dashboard_prd.md"),
    }

    # Signal types that map to specific sections
    SIGNAL_SECTION_MAP = {
        "requirement": PRDSection.REQUIREMENTS,
        "decision": PRDSection.DESIGN_DECISIONS,
        "question": PRDSection.OPEN_QUESTIONS,
        "insight": PRDSection.OVERVIEW,
        "action_item": PRDSection.MILESTONES,
    }

    def __init__(self, drafts_path: Path | None = None):
        """Initialize updater.

        Args:
            drafts_path: Path to save draft files. Defaults to inbox/processed/drafts/
        """
        self.drafts_path = drafts_path or (
            Path(__file__).parent.parent / "inbox" / "processed" / "drafts"
        )

    def generate_updates(
        self,
        cluster,  # SignalCluster
        prd_path: Path | None = None,
    ) -> list[PRDUpdateDraft]:
        """Generate PRD update drafts from a signal cluster.

        Args:
            cluster: SignalCluster with signals to synthesize
            prd_path: Path to existing PRD file (optional)

        Returns:
            List of PRDUpdateDraft objects
        """
        drafts = []

        # Group signals by target section
        by_section: dict[PRDSection, list] = {}
        for signal in cluster.signals:
            section = self.SIGNAL_SECTION_MAP.get(
                signal.signal_type,
                PRDSection.OVERVIEW,
            )
            if section not in by_section:
                by_section[section] = []
            by_section[section].append(signal)

        # Load existing PRD content if available
        current_prd = self._load_prd(prd_path or self.PRD_PATHS.get(cluster.prd_target))

        # Generate draft for each section
        for section, signals in by_section.items():
            draft = self._synthesize_section(
                prd_target=cluster.prd_target,
                section=section,
                signals=signals,
                current_content=current_prd.get(section.value) if current_prd else None,
            )
            if draft:
                drafts.append(draft)

        return drafts

    def _synthesize_section(
        self,
        prd_target: str,
        section: PRDSection,
        signals: list,
        current_content: str | None,
    ) -> PRDUpdateDraft | None:
        """Synthesize content for a single section.

        TODO: Call LLM with prompts/prd_synthesis_v1 prompts
        """
        if not signals:
            return None

        # Build citations
        citations = [
            Citation(
                signal_id=s.signal_id,
                source_type=s.source,
                source_date=s.timestamp,
                excerpt=s.content[:200] + "..." if len(s.content) > 200 else s.content,
            )
            for s in signals
        ]

        # Calculate confidence based on signal quality
        confidence = self._calculate_confidence(signals)

        # Generate proposed content
        # TODO: Replace with LLM call
        proposed_content = self._generate_placeholder_content(section, signals)

        # Generate rationale
        rationale = self._generate_rationale(section, signals)

        draft_id = f"draft_{prd_target}_{section.value}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        return PRDUpdateDraft(
            draft_id=draft_id,
            prd_target=prd_target,
            section=section,
            current_content=current_content,
            proposed_content=proposed_content,
            rationale=rationale,
            citations=citations,
            confidence_score=confidence,
        )

    def _calculate_confidence(self, signals: list) -> float:
        """Calculate confidence score for synthesis.

        Factors:
        - Number of corroborating signals
        - Source diversity (calendar, notes, email)
        - Signal quality scores
        - Recency
        """
        if not signals:
            return 0.0

        # Base: more signals = higher confidence (up to 0.5)
        signal_score = min(len(signals) / 5, 0.5)

        # Source diversity bonus (up to 0.25)
        sources = set(s.source for s in signals)
        diversity_score = min(len(sources) / 3, 0.25)

        # Quality score average (up to 0.25)
        quality_scores = [s.quality_score for s in signals if hasattr(s, 'quality_score')]
        quality_score = (sum(quality_scores) / len(quality_scores) * 0.25) if quality_scores else 0.125

        return min(signal_score + diversity_score + quality_score, 1.0)

    def _generate_placeholder_content(self, section: PRDSection, signals: list) -> str:
        """Generate placeholder content (until LLM integration).

        TODO: Replace with actual LLM synthesis
        """
        lines = [f"## {section.value.replace('_', ' ').title()}", ""]

        if section == PRDSection.REQUIREMENTS:
            for i, sig in enumerate(signals, 1):
                lines.append(f"- REQ-{i:03d}: {sig.content[:100]}...")
        elif section == PRDSection.DESIGN_DECISIONS:
            for sig in signals:
                lines.append(f"**Decision:** {sig.content[:100]}...")
                lines.append("")
        elif section == PRDSection.OPEN_QUESTIONS:
            for sig in signals:
                lines.append(f"- [ ] {sig.content[:100]}...")
        else:
            for sig in signals:
                lines.append(f"- {sig.content[:100]}...")

        return "\n".join(lines)

    def _generate_rationale(self, section: PRDSection, signals: list) -> str:
        """Generate rationale for the update."""
        sources = set(s.source for s in signals)
        people = set()
        for s in signals:
            people.update(s.people)

        return (
            f"Synthesized from {len(signals)} signals across {len(sources)} sources. "
            f"Contributors: {', '.join(people) if people else 'N/A'}."
        )

    def _load_prd(self, prd_path: Path | None) -> dict[str, str] | None:
        """Load existing PRD and parse sections.

        TODO: Implement proper markdown section parsing
        """
        if not prd_path or not prd_path.exists():
            return None

        content = prd_path.read_text()

        # Simple section extraction
        # TODO: Proper markdown parsing
        sections = {}
        current_section = None
        current_content = []

        for line in content.split("\n"):
            if line.startswith("## "):
                if current_section:
                    sections[current_section] = "\n".join(current_content)
                section_name = line[3:].strip().lower().replace(" ", "_")
                current_section = section_name
                current_content = [line]
            elif current_section:
                current_content.append(line)

        if current_section:
            sections[current_section] = "\n".join(current_content)

        return sections

    def save_drafts(self, drafts: list[PRDUpdateDraft]) -> Path:
        """Save drafts to JSON file for review.

        Args:
            drafts: List of drafts to save

        Returns:
            Path to saved file
        """
        self.drafts_path.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = self.drafts_path / f"prd_drafts_{timestamp}.json"

        data = {
            "created_at": datetime.now().isoformat(),
            "draft_count": len(drafts),
            "drafts": [d.to_dict() for d in drafts],
        }

        atomic_write(filepath, data)
        return filepath

    def apply_approved_drafts(self, drafts: list[PRDUpdateDraft]) -> dict[str, int]:
        """Apply approved drafts to PRD files.

        Args:
            drafts: List of drafts (only APPROVED will be applied)

        Returns:
            Dict with counts of applied/skipped per PRD
        """
        results = {}

        approved = [d for d in drafts if d.status == DraftStatus.APPROVED]

        for draft in approved:
            prd_path = self.PRD_PATHS.get(draft.prd_target)
            if not prd_path:
                continue

            if draft.prd_target not in results:
                results[draft.prd_target] = {"applied": 0, "skipped": 0}

            # TODO: Implement actual PRD file update
            # For now, just count
            results[draft.prd_target]["applied"] += 1

        return results
