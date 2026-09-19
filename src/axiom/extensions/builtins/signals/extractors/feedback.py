# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""User feedback extractor for Sense pipeline.

Extracts signals from user feedback sources to close the loop:
- Usability studies
- Support tickets
- Analytics data
- NPS/satisfaction surveys
- Community feedback

Categorizes feedback by type: usefulness, ease, joy, performance, completeness.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

from ..loop import FeedbackType
from ..models import Extraction, Signal


class FeedbackSource(Enum):
    """Sources of user feedback."""
    USABILITY_STUDY = "usability_study"
    SUPPORT_TICKET = "support_ticket"
    ANALYTICS = "analytics"
    SURVEY = "survey"
    COMMUNITY = "community"
    INTERVIEW = "interview"
    DOGFOOD = "dogfood"  # Internal usage feedback


@dataclass
class FeedbackItem:
    """Parsed feedback item."""
    feedback_id: str
    source: FeedbackSource
    timestamp: datetime
    user_id: str | None
    feature_ref: str | None  # Which feature this relates to
    feedback_type: FeedbackType
    sentiment: float  # -1.0 (negative) to 1.0 (positive)
    content: str
    metadata: dict


class FeedbackExtractor:
    """Extracts signals from user feedback sources.

    Supports:
    - Structured feedback (surveys, support tickets)
    - Semi-structured (usability study notes)
    - Analytics signals (drop-offs, error rates)

    Usage:
        extractor = FeedbackExtractor()

        # From survey export
        signals = extractor.extract_from_survey(Path("inbox/raw/feedback/nps.csv"))

        # From support tickets
        signals = extractor.extract_from_support(Path("inbox/raw/feedback/tickets.json"))

        # From analytics
        signals = extractor.extract_from_analytics(analytics_data)
    """

    # Keywords for feedback type classification
    FEEDBACK_TYPE_KEYWORDS = {
        FeedbackType.USEFULNESS: [
            "useful", "useless", "need", "want", "missing",
            "solves", "doesn't solve", "helps", "doesn't help",
            "valuable", "worthless", "pointless",
        ],
        FeedbackType.EASE: [
            "easy", "hard", "difficult", "confusing", "intuitive",
            "complicated", "simple", "straightforward", "unclear",
            "couldn't figure out", "took forever", "obvious",
        ],
        FeedbackType.JOY: [
            "love", "hate", "enjoy", "frustrating", "delightful",
            "annoying", "pleasant", "painful", "satisfying",
            "beautiful", "ugly", "elegant", "clunky",
        ],
        FeedbackType.PERFORMANCE: [
            "slow", "fast", "lag", "freeze", "crash",
            "responsive", "sluggish", "instant", "waiting",
            "timeout", "error", "bug", "broken",
        ],
        FeedbackType.COMPLETENESS: [
            "missing", "incomplete", "need more", "wish it had",
            "expected", "should have", "lacks", "where is",
            "can't find", "no way to", "doesn't support",
        ],
    }

    # Feature reference patterns
    FEATURE_KEYWORDS = {
        "ops_log": ["ops log", "console", "shift log", "operations"],
        "experiment_manager": ["experiment", "sample", "exposure", "tracking"],
        "operator_dashboard": ["dashboard", "alerts", "monitoring", "operator"],
        "researcher_dashboard": ["results", "analysis", "my experiments", "data"],
    }

    def __init__(self, inbox_path: Path | None = None):
        """Initialize extractor.

        Args:
            inbox_path: Path to inbox directory. Defaults to tools/agents/inbox/
        """
        self.inbox_path = inbox_path or Path(__file__).parent.parent / "inbox"
        self.feedback_dir = self.inbox_path / "raw" / "feedback"

    def extract_all(self) -> Extraction:
        """Extract from all feedback sources in inbox.

        Returns:
            Extraction with all feedback signals
        """
        signals = []
        errors = []

        if not self.feedback_dir.exists():
            return Extraction(
                extractor="feedback",
                source_file=str(self.feedback_dir),
                signals=[],
                errors=[f"Feedback directory not found: {self.feedback_dir}"],
            )

        # Process different feedback file types
        for csv_file in self.feedback_dir.glob("*.csv"):
            try:
                extraction = self.extract_from_survey(csv_file)
                signals.extend(extraction.signals)
                errors.extend(extraction.errors)
            except Exception as e:
                errors.append(f"Error processing {csv_file}: {e}")

        for json_file in self.feedback_dir.glob("*.json"):
            try:
                extraction = self.extract_from_support(json_file)
                signals.extend(extraction.signals)
                errors.extend(extraction.errors)
            except Exception as e:
                errors.append(f"Error processing {json_file}: {e}")

        for md_file in self.feedback_dir.glob("*.md"):
            try:
                extraction = self.extract_from_study_notes(md_file)
                signals.extend(extraction.signals)
                errors.extend(extraction.errors)
            except Exception as e:
                errors.append(f"Error processing {md_file}: {e}")

        return Extraction(
            extractor="feedback",
            source_file=str(self.feedback_dir),
            signals=signals,
            errors=errors,
        )

    def extract_from_survey(self, filepath: Path) -> Extraction:
        """Extract signals from survey export (CSV).

        Expected columns: timestamp, user_id, question, response, score

        Args:
            filepath: Path to survey CSV

        Returns:
            Extraction with survey signals
        """
        signals = []
        errors = []

        # TODO: Implement CSV parsing
        # For now, return stub

        return Extraction(
            extractor="feedback_survey",
            source_file=str(filepath),
            signals=signals,
            errors=errors,
        )

    def extract_from_support(self, filepath: Path) -> Extraction:
        """Extract signals from support tickets (JSON).

        Expected format: [{id, created_at, subject, body, tags, status}]

        Args:
            filepath: Path to tickets JSON

        Returns:
            Extraction with support ticket signals
        """
        import json

        signals = []
        errors = []

        try:
            data = json.loads(filepath.read_text())
            tickets = data if isinstance(data, list) else data.get("tickets", [])

            for ticket in tickets:
                feedback_item = self._parse_support_ticket(ticket)
                if feedback_item:
                    signal = self._feedback_to_signal(feedback_item)
                    signals.append(signal)

        except Exception as e:
            errors.append(f"Failed to parse {filepath}: {e}")

        return Extraction(
            extractor="feedback_support",
            source_file=str(filepath),
            signals=signals,
            errors=errors,
        )

    def extract_from_study_notes(self, filepath: Path) -> Extraction:
        """Extract signals from usability study notes (markdown).

        Args:
            filepath: Path to study notes markdown

        Returns:
            Extraction with study signals
        """
        signals = []
        errors = []

        try:
            content = filepath.read_text()
            feedback_items = self._parse_study_notes(content, filepath)

            for item in feedback_items:
                signal = self._feedback_to_signal(item)
                signals.append(signal)

        except Exception as e:
            errors.append(f"Failed to parse {filepath}: {e}")

        return Extraction(
            extractor="feedback_study",
            source_file=str(filepath),
            signals=signals,
            errors=errors,
        )

    def extract_from_analytics(
        self,
        analytics_data: dict,
        feature_ref: str | None = None,
    ) -> Extraction:
        """Extract signals from analytics data.

        Args:
            analytics_data: Dict with metrics like drop_off_rate, error_rate, etc.
            feature_ref: Feature these analytics relate to

        Returns:
            Extraction with analytics signals
        """
        signals = []

        # High drop-off rate → completeness/ease issue
        if analytics_data.get("drop_off_rate", 0) > 0.3:
            signals.append(Signal(
                source="analytics",
                timestamp=datetime.now().isoformat(),
                raw_text=f"High drop-off rate: {analytics_data['drop_off_rate']:.1%}",
                signal_type="insight",
                initiatives=[feature_ref] if feature_ref else [],
                detail=f"Users dropping off at {analytics_data['drop_off_rate']:.1%} rate",
                confidence=0.9,  # Analytics are high confidence
                metadata={
                    "feedback_type": FeedbackType.EASE.value,
                    "sentiment": -0.5,
                    "metric": "drop_off_rate",
                    "value": analytics_data["drop_off_rate"],
                },
            ))

        # High error rate → performance issue
        if analytics_data.get("error_rate", 0) > 0.05:
            signals.append(Signal(
                source="analytics",
                timestamp=datetime.now().isoformat(),
                raw_text=f"High error rate: {analytics_data['error_rate']:.1%}",
                signal_type="insight",
                initiatives=[feature_ref] if feature_ref else [],
                detail=f"Error rate at {analytics_data['error_rate']:.1%}",
                confidence=0.9,
                metadata={
                    "feedback_type": FeedbackType.PERFORMANCE.value,
                    "sentiment": -0.7,
                    "metric": "error_rate",
                    "value": analytics_data["error_rate"],
                },
            ))

        # Low completion rate → completeness/ease issue
        if analytics_data.get("completion_rate", 1.0) < 0.5:
            signals.append(Signal(
                source="analytics",
                timestamp=datetime.now().isoformat(),
                raw_text=f"Low completion rate: {analytics_data['completion_rate']:.1%}",
                signal_type="insight",
                initiatives=[feature_ref] if feature_ref else [],
                detail=f"Only {analytics_data['completion_rate']:.1%} completing flow",
                confidence=0.9,
                metadata={
                    "feedback_type": FeedbackType.COMPLETENESS.value,
                    "sentiment": -0.4,
                    "metric": "completion_rate",
                    "value": analytics_data["completion_rate"],
                },
            ))

        return Extraction(
            extractor="feedback_analytics",
            source_file="analytics_api",
            signals=signals,
        )

    def _parse_support_ticket(self, ticket: dict) -> FeedbackItem | None:
        """Parse a support ticket into FeedbackItem."""
        content = f"{ticket.get('subject', '')} {ticket.get('body', '')}"
        if not content.strip():
            return None

        feedback_type = self._classify_feedback_type(content)
        sentiment = self._analyze_sentiment(content)
        feature_ref = self._infer_feature(content)

        return FeedbackItem(
            feedback_id=str(ticket.get("id", "")),
            source=FeedbackSource.SUPPORT_TICKET,
            timestamp=datetime.fromisoformat(
                ticket.get("created_at", datetime.now().isoformat())[:19]
            ),
            user_id=ticket.get("user_id"),
            feature_ref=feature_ref,
            feedback_type=feedback_type,
            sentiment=sentiment,
            content=content,
            metadata={"tags": ticket.get("tags", [])},
        )

    def _parse_study_notes(self, content: str, filepath: Path) -> list[FeedbackItem]:
        """Parse usability study notes into FeedbackItems."""
        items = []

        # Split by participant sections (## Participant X)
        sections = content.split("## Participant")

        for i, section in enumerate(sections[1:], 1):  # Skip header
            # Extract observations
            lines = section.strip().split("\n")
            observations = [
                line.strip("- ").strip()
                for line in lines
                if line.strip().startswith("-") and len(line.strip()) > 3
            ]

            for obs in observations:
                feedback_type = self._classify_feedback_type(obs)
                sentiment = self._analyze_sentiment(obs)
                feature_ref = self._infer_feature(obs)

                items.append(FeedbackItem(
                    feedback_id=f"study_{filepath.stem}_p{i}_{len(items)}",
                    source=FeedbackSource.USABILITY_STUDY,
                    timestamp=datetime.fromtimestamp(filepath.stat().st_mtime),
                    user_id=f"participant_{i}",
                    feature_ref=feature_ref,
                    feedback_type=feedback_type,
                    sentiment=sentiment,
                    content=obs,
                    metadata={"study": filepath.stem},
                ))

        return items

    def _feedback_to_signal(self, feedback: FeedbackItem) -> Signal:
        """Convert FeedbackItem to Signal."""
        return Signal(
            source=f"feedback_{feedback.source.value}",
            timestamp=feedback.timestamp.isoformat(),
            raw_text=feedback.content,
            signal_type="insight",  # Feedback is insight for requirements
            initiatives=[feedback.feature_ref] if feedback.feature_ref else [],
            detail=feedback.content[:200],
            confidence=0.8,  # User feedback is relatively high confidence
            metadata={
                "feedback_id": feedback.feedback_id,
                "feedback_type": feedback.feedback_type.value,
                "sentiment": feedback.sentiment,
                "user_id": feedback.user_id,
                **feedback.metadata,
            },
        )

    def _classify_feedback_type(self, text: str) -> FeedbackType:
        """Classify feedback text into a FeedbackType."""
        text_lower = text.lower()

        scores = {}
        for fb_type, keywords in self.FEEDBACK_TYPE_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in text_lower)
            scores[fb_type] = score

        # Return type with highest score, default to USEFULNESS
        if max(scores.values()) > 0:
            return max(scores, key=lambda k: scores[k])
        return FeedbackType.USEFULNESS

    def _analyze_sentiment(self, text: str) -> float:
        """Simple sentiment analysis (-1.0 to 1.0).

        TODO: Replace with proper sentiment model
        """
        text_lower = text.lower()

        positive = ["love", "great", "easy", "helpful", "works", "thank", "awesome"]
        negative = ["hate", "broken", "confusing", "slow", "bug", "error", "can't"]

        pos_count = sum(1 for w in positive if w in text_lower)
        neg_count = sum(1 for w in negative if w in text_lower)

        if pos_count + neg_count == 0:
            return 0.0
        return (pos_count - neg_count) / (pos_count + neg_count)

    def _infer_feature(self, text: str) -> str | None:
        """Infer which feature feedback relates to."""
        text_lower = text.lower()

        for feature, keywords in self.FEATURE_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return feature

        return None
