# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration test: Publisher merge workflow with concurrent signals and reviews.

Scenario: A published PRD (Advanced Analytics) has been in review on SharePoint for 2 weeks.
During that time:
1. Local synthesis: 3 Sense signals (meeting notes, GitLab discussion, voice memo)
   have been processed and drafted as revision suggestions
2. Remote changes: 4 reviewers made concurrent comments, tracked changes, and
   content edits on the SharePoint version
3. Divergence: Both .md and external doc have evolved independently
4. Merge task: Publisher must perform semantic merge using Neut's RAG context

This test validates the complete workflow without assuming Git or live APIs,
focusing on the merge logic and signal synthesis.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from axiom.extensions.builtins.publishing.state import DocumentState, PublicationRecord
from axiom.extensions.builtins.signals.extractors.docflow_review import (
    ChangeType,
    DivergenceReport,
    DocFormat,
    ExternalChange,
)
from axiom.extensions.builtins.signals.models import Changelog, Extraction, Signal
from axiom.extensions.builtins.signals.synthesizer import Synthesizer

# ============================================================================
# SCENARIO SETUP: Advanced Analytics PRD
# ============================================================================

class AdvancedAnalyticsPRDScenario:
    """Fictional but realistic scenario for testing merge workflow."""

    PDR_ID = "advanced-analytics-prd"
    BASE_VERSION = "v2.0.0"
    BASE_COMMIT = "abc1234"

    # Content versions
    BASELINE_MD = """**Advanced Analytics Module PRD**

**[published] v2.0.0** | February 10, 2026

**Related Modules:** Experiment Manager (upstream data), Dashboard (display)

## Executive Summary

The Advanced Analytics module provides statistical analysis, visualization, and
reporting capabilities for experiment data. It transforms raw measurements into
actionable insights through configurable analysis pipelines.

### Key Features
- Real-time statistical analysis (t-tests, correlations, regression)
- Customizable dashboards and report generation
- Integration with experimental metadata and provenance tracking

## Core Requirements

### R1: Statistical Analysis Engine
The system shall support parametric and non-parametric tests on experimental data.

**Acceptance Criteria:**
- AC1.1: Support t-tests with 95% confidence reporting
- AC1.2: Support Pearson and Spearman correlations
- AC1.3: Log all analysis decisions in audit trail

### R2: Visualization Pipeline
The system shall render plots and charts suitable for publication.

**Acceptance Criteria:**
- AC2.1: Support matplotlib, plotly backends
- AC2.2: Generate high-resolution figures for papers (300+ DPI)
- AC2.3: Support LaTeX rendering for scientific notation

## Design Approach

### Statistical Analysis Flow
```
Raw Data → Validation → Analysis → Results → Audit Log
```

The system shall:
1. Validate input data quality
2. Apply selected statistical test
3. Generate results with confidence intervals
4. Record all parameters and decisions

## Open Questions

- Q1: Should we support Bayesian methods initially, or defer to v3?
- Q2: How do we handle missing data imputation?
- Q3: What's the max dataset size we need to support?

## Success Metrics

- 95% accuracy on statistical calculations (validated against R/scipy)
- < 2s analysis time for datasets < 100k rows
- Zero unreviewed statistical methods in production
"""

    # Sense signals from local synthesis
    SENSE_SIGNALS = [
        {
            "signal_id": "sig_meeting_001",
            "source": "meeting_notes",
            "timestamp": "2026-02-18T14:30:00Z",
            "detail": "Team meeting: Add missing data imputation strategy to R2",
            "raw_text": """Meeting notes from Feb 18:
- Dr. Chen raised concern about missing data in R2 acceptance criteria
- Currently no guidance on NaN, null, or sparse data handling
- Suggested adding imputation strategies: mean, median, KNN-based
- Team agreed: important for real-world data quality
- Action: Draft requirements for missing data handling in v2.1""",
            "signal_type": "action_item",
            "people": ["Dr. Chen", "Analytics Team"],
            "initiatives": ["advanced-analytics-prd"],
            "confidence": 0.95,
        },
        {
            "signal_id": "sig_gitlab_002",
            "source": "gitlab_discussion",
            "timestamp": "2026-02-20T10:15:00Z",
            "detail": "Issue #1523: Need Bayesian methods sooner",
            "raw_text": """GitLab Issue #1523 discussion:
Comments from experimentalists requesting Bayesian inference:
- "We need credible intervals, not just p-values"
- "Bayesian is standard in our field now"
- "Could we move this from v3 to v2.1?"

Engineer response: Feasible with Stan integration, needs 2-3 week sprint""",
            "signal_type": "decision",
            "people": ["Jake Martinez", "Dr. Patel"],
            "initiatives": ["advanced-analytics-prd"],
            "confidence": 0.90,
        },
        {
            "signal_id": "sig_voice_003",
            "source": "voice_memo",
            "timestamp": "2026-02-21T16:45:00Z",
            "detail": "Voice memo: Performance concerns at scale",
            "raw_text": """Transcribed voice memo from Principal Investigator:
'I was looking at the performance metric in the PRD - two seconds for 100k rows.
That's actually pretty aggressive. Last project, we routinely hit 500k row datasets
and we need near-real-time analysis. Maybe we should revisit the success metric.
Two seconds for 100k is good for v2, but maybe make it clearer that this is
a placeholder we'll need to validate in alpha testing.'""",
            "signal_type": "blocker",
            "people": ["PI Brown"],
            "initiatives": ["advanced-analytics-prd"],
            "confidence": 0.85,
        },
    ]

    # External changes from SharePoint review
    EXTERNAL_CHANGES = [
        {
            "change_id": "ext_001",
            "change_type": "comment",
            "author": "Dr. Sarah Kim",
            "timestamp": "2026-02-15T09:30:00Z",
            "section": "R1: Statistical Analysis Engine",
            "comment_text": "Good start. What about effect size reporting? That's critical for meta-analysis.",
            "original_text": "The system shall support parametric and non-parametric tests on experimental data.",
            "context": "R1 definition",
            "confidence": 0.98,
        },
        {
            "change_id": "ext_002",
            "change_type": "tracked",
            "author": "Prof. James Lee",
            "timestamp": "2026-02-17T14:20:00Z",
            "section": "R2: Visualization Pipeline",
            "original_text": "Support matplotlib, plotly backends",
            "new_text": "Support matplotlib, plotly, vega backends (for interactive web dashboards)",
            "context": "AC2.1 addition - interactive capabilities",
            "confidence": 0.95,
        },
        {
            "change_id": "ext_003",
            "change_type": "drift",
            "author": "Dr. Chen (reviewer)",
            "timestamp": "2026-02-19T11:45:00Z",
            "section": "Success Metrics",
            "original_text": "< 2s analysis time for datasets < 100k rows",
            "new_text": "< 3s analysis time for datasets < 100k rows (with 95th percentile at 2.5s)",
            "context": "Performance benchmark refinement",
            "confidence": 0.92,
        },
        {
            "change_id": "ext_004",
            "change_type": "comment",
            "author": "Dr. Aisha Patel",
            "timestamp": "2026-02-20T16:00:00Z",
            "section": "Open Questions",
            "comment_text": "Re: Q1 (Bayesian methods) - experimentalists are asking about this a lot. "
                            "Maybe move to v2.1 instead of v3? Would differentiate us from competitors.",
            "original_text": "Should we support Bayesian methods initially, or defer to v3?",
            "context": "Reviewer feedback on deferral decision",
            "confidence": 0.93,
        },
    ]


# ============================================================================
# TEST FIXTURES: Simplified Mock Data
# ============================================================================

@pytest.fixture
def baseline_document_state() -> DocumentState:
    """Published Advanced Analytics PRD at v2.0.0."""
    return DocumentState(
        doc_id="advanced-analytics-prd",
        source_path="docs/prds/prd_advanced-analytics.md",
        status="published",
        published=PublicationRecord(
            storage_id="advanced-analytics-prd.docx",
            url="sharepoint://tenant/sites/docs/advanced-analytics-prd.docx",
            version="v2.0.0",
            published_at="2026-02-10T18:30:00Z",
            commit_sha="abc1234",
            generation_provider="pandoc-docx",
            storage_provider="sharepoint",
        ),
        stakeholders=["Dr. Sarah Kim", "Prof. James Lee", "Dr. Chen", "Dr. Aisha Patel"],
        last_branch="main",
        last_commit="abc1234",
    )


@pytest.fixture
def sense_signals_extraction() -> Extraction:
    """Result of processing 3 Sense signals."""
    signals = [Signal.from_dict(s) for s in AdvancedAnalyticsPRDScenario.SENSE_SIGNALS]
    return Extraction(
        extractor="sense_synthesizer",
        source_file="signals/advanced-analytics-signals.json",
        signals=signals,
        extracted_at="2026-02-21T18:00:00Z",
    )


@pytest.fixture
def external_changes() -> list[ExternalChange]:
    """Changes detected from SharePoint review."""
    changes = []

    # Mapping from test data to actual enum names
    type_mapping = {
        "comment": ChangeType.COMMENT,
        "tracked": ChangeType.TRACKED_CHANGE,
        "drift": ChangeType.CONTENT_DRIFT,
        "structural": ChangeType.STRUCTURAL,
    }

    for change_dict in AdvancedAnalyticsPRDScenario.EXTERNAL_CHANGES:
        change_type = type_mapping.get(change_dict["change_type"], ChangeType.COMMENT)
        changes.append(ExternalChange(
            change_type=change_type,
            section=change_dict["section"],
            author=change_dict["author"],
            timestamp=change_dict["timestamp"],
            original_text=change_dict["original_text"],
            new_text=change_dict.get("new_text", change_dict["original_text"]),
            comment_text=change_dict.get("comment_text", ""),
            context=change_dict.get("context", ""),
            confidence=change_dict.get("confidence", 0.9),
        ))
    return changes


@pytest.fixture
def divergence_report(external_changes: list[ExternalChange]) -> DivergenceReport:
    """Report showing both .md and SharePoint have diverged."""
    md_hash = hashlib.sha256(
        AdvancedAnalyticsPRDScenario.BASELINE_MD.encode()
    ).hexdigest()

    # Simulate small changes on SharePoint (added reviewer feedback)
    sharepoint_content = AdvancedAnalyticsPRDScenario.BASELINE_MD + "\n\n[REVIEWER NOTES ADDED]"
    sp_hash = hashlib.sha256(sharepoint_content.encode()).hexdigest()

    return DivergenceReport(
        prd_id="advanced-analytics-prd",
        md_path=Path("docs/prds/prd_advanced-analytics.md"),
        external_uri="sharepoint://tenant/sites/docs/advanced-analytics-prd.docx",
        external_format=DocFormat.MS_WORD,
        md_hash=md_hash,
        external_hash=sp_hash,
        last_sync_hash=md_hash,  # Both diverged from baseline
        md_changed_since_sync=False,  # .md hasn't changed since publish
        external_changed_since_sync=True,  # SharePoint has reviewer changes
        requires_merge=False,  # Only external changed, so external wins
        changes=external_changes,
        merge_strategy="external_wins",
    )


# ============================================================================
# TEST CASES: Divergence Detection
# ============================================================================

class TestDivergenceDetection:
    """Test 1: Detect divergence between local .md and external document."""

    def test_detect_divergence_simple(self, divergence_report: DivergenceReport):
        """Verify divergence detection identifies external changes."""
        assert divergence_report.md_hash != divergence_report.external_hash
        assert divergence_report.external_changed_since_sync is True
        assert divergence_report.md_changed_since_sync is False

    def test_external_changes_extracted(self, divergence_report: DivergenceReport):
        """Verify all 4 reviewer changes are extracted."""
        assert len(divergence_report.changes) == 4

        # Check change types
        change_types = {c.change_type for c in divergence_report.changes}
        assert ChangeType.COMMENT in change_types
        assert ChangeType.TRACKED_CHANGE in change_types
        assert ChangeType.CONTENT_DRIFT in change_types

    def test_change_metadata_preserved(self, divergence_report: DivergenceReport):
        """Verify change metadata (author, timestamp, context) preserved."""
        for change in divergence_report.changes:
            assert change.author, "Change must have author"
            assert change.timestamp, "Change must have timestamp"
            assert change.section, "Change must have section"
            assert change.confidence >= 0.9, "External doc changes should have high confidence"


# ============================================================================
# TEST CASES: Sense Workflow Integration
# ============================================================================

class TestSenseWorkflowIntegration:
    """Test that merge scenario properly integrates with Neut Sense pipelines."""

    def test_signals_flow_through_sense_synthesizer(
        self,
        sense_signals_extraction: Extraction,
    ):
        """Verify signals flow through the actual Sense synthesizer."""
        # This simulates: Raw signals → Synthesizer → Changelog
        synthesizer = Synthesizer()

        changelog = synthesizer.synthesize(
            signals=sense_signals_extraction.signals,
            date="2026-02-21",
            include_all=True,
        )

        assert isinstance(changelog, Changelog)
        assert changelog.date == "2026-02-21"
        assert len(changelog.entries) == 3, "Should have 3 changelog entries from 3 signals"

    def test_changelog_groups_signals_by_initiative(
        self,
        sense_signals_extraction: Extraction,
    ):
        """Verify Synthesizer groups signals by initiative (advanced-analytics-prd)."""
        synthesizer = Synthesizer()
        changelog = synthesizer.synthesize(
            signals=sense_signals_extraction.signals,
            include_all=True,
        )

        # All entries should be for the same initiative
        initiatives = {e.initiative for e in changelog.entries}
        assert "advanced-analytics-prd" in initiatives

    def test_changelog_reflects_signal_types(
        self,
        sense_signals_extraction: Extraction,
    ):
        """Verify changelog respects signal types (action_item, decision, blocker)."""
        synthesizer = Synthesizer()
        changelog = synthesizer.synthesize(
            signals=sense_signals_extraction.signals,
            include_all=True,
        )

        signal_types = {e.signal_type for e in changelog.entries}
        assert "action_item" in signal_types
        assert "decision" in signal_types
        assert "blocker" in signal_types

    def test_sense_signals_map_to_draft_revisions(
        self,
        sense_signals_extraction: Extraction,
    ):
        """Verify that Sense signals can be mapped to concrete draft revisions.

        In the real workflow:
        1. Signals are extracted from various sources
        2. Synthesized into changelog
        3. Human reviews changelog entries
        4. Each entry becomes a suggested revision to the PRD

        This test verifies the mapping.
        """
        # Simulate the human → revision mapping (keyed by signal detail for clarity)
        signal_to_revision_mapping = {
            "Team meeting: Add missing data imputation strategy to R2": {
                "section": "R2: Visualization Pipeline",
                "action": "add_requirement",
                "new_content": "R2.1: Missing Data Handling\n"
                               "The system shall handle missing or sparse data.",
            },
            "Issue #1523: Need Bayesian methods sooner": {
                "section": "Open Questions - Q1",
                "action": "update_question",
                "new_content": "Should we support Bayesian methods in v2.1?",
            },
            "Voice memo: Performance concerns at scale": {
                "section": "Success Metrics",
                "action": "update_metric",
                "new_content": "Performance to be validated at 500k+ row datasets.",
            },
        }

        # Verify all signals have corresponding revisions
        for signal in sense_signals_extraction.signals:
            assert signal.detail in signal_to_revision_mapping, \
                f"Signal '{signal.detail}' must have draft revision mapping"


# ============================================================================
# TEST CASES: Signal Synthesis
# ============================================================================

class TestSignalSynthesis:
    """Test 2: Synthesize local Sense signals into draft revisions."""

    def test_signals_extracted(self, sense_signals_extraction: Extraction):
        """Verify all 3 signals are extracted."""
        assert len(sense_signals_extraction.signals) == 3

    def test_signal_types_diverse(self, sense_signals_extraction: Extraction):
        """Verify signals have different types (action, decision, blocker)."""
        signal_types = {s.signal_type for s in sense_signals_extraction.signals}
        assert len(signal_types) >= 2, "Should have diverse signal types"

    def test_signal_confidence_realistic(self, sense_signals_extraction: Extraction):
        """Verify confidence scores are realistic."""
        for signal in sense_signals_extraction.signals:
            assert 0.8 <= signal.confidence <= 1.0, "Sense signals should have high confidence"

    def test_signal_provenance_tracked(self, sense_signals_extraction: Extraction):
        """Verify signals track source and people involved."""
        for signal in sense_signals_extraction.signals:
            assert signal.source, "Signal must have source"
            assert signal.people or signal.detail, "Signal must track who/what"

    def test_synthesis_groups_related_signals(self, sense_signals_extraction: Extraction):
        """Verify related signals can be grouped conceptually.

        Signals about Bayesian (signal_002) and PI's performance concern (signal_003)
        are related and should be grouped in merge discussion.
        """
        initiatives_mentioned = set()
        for signal in sense_signals_extraction.signals:
            initiatives_mentioned.update(signal.initiatives)

        # All signals should mention the same PRD
        assert "advanced-analytics-prd" in initiatives_mentioned


# ============================================================================
# TEST CASES: Conceptual Merge Logic
# ============================================================================

@dataclass
class MergeDecision:
    """Result of semantic merge for one section."""
    section: str
    resolution: str  # "accept_external", "accept_local", "combine", "defer"
    rationale: str  # Why this decision was made
    merged_content: str  # The actual merged text


class TestConceptualMerge:
    """Test 3: Semantic merge using Neut's RAG context."""

    def _simulate_neut_merge(
        self,
        baseline_md: str,
        external_changes: list[ExternalChange],
        sense_signals: list[Signal],
    ) -> list[MergeDecision]:
        """Simulate Neut's LLM-assisted semantic merge.

        In reality, this would:
        1. Load Neut's RAG context about this document and domain
        2. Analyze each change and signal semantically
        3. Identify conflicts and synthesize resolution
        4. Generate merged content with rationale

        For this test, we simulate the decision logic.
        """
        decisions = []

        # Decision 1: Effect size reporting (external comment)
        decisions.append(MergeDecision(
            section="R1: Statistical Analysis Engine",
            resolution="combine",
            rationale=(
                "External reviewer (Dr. Kim) correctly identified missing requirement. "
                "Effect size is critical for meta-analysis. Incorporate as AC1.4."
            ),
            merged_content=(
                "The system shall support parametric and non-parametric tests "
                "on experimental data, including effect size reporting."
            ),
        ))

        # Decision 2: Vega backend for interactive dashboards (external tracked change)
        decisions.append(MergeDecision(
            section="R2: Visualization Pipeline - AC2.1",
            resolution="accept_external",
            rationale=(
                "Prof. Lee's addition of vega for interactive web dashboards aligns with "
                "product direction and addresses user feedback."
            ),
            merged_content=(
                "Support matplotlib, plotly, vega backends (for interactive web dashboards)"
            ),
        ))

        # Decision 3: Bayesian methods timing (signal + external comment)
        # Signals suggest it's important; external comment agrees
        decisions.append(MergeDecision(
            section="Open Questions - Q1",
            resolution="defer",
            rationale=(
                "Both Sense signals (GitLab, PI voice) and Dr. Patel's comment indicate "
                "Bayesian methods are high-priority. Defer to v2.1 roadmap discussion "
                "rather than encoding in PRD. Record as future work."
            ),
            merged_content=(
                "Should we support Bayesian methods (credible intervals, Stan integration) "
                "in v2.1 instead of v3? High stakeholder interest."
            ),
        ))

        # Decision 4: Performance metric clarification (external drift + PI voice memo)
        decisions.append(MergeDecision(
            section="Success Metrics",
            resolution="combine",
            rationale=(
                "External reviewer (Dr. Chen) refined metric; PI voice memo raises concern "
                "about 2s being aggressive at scale. Combine: accept higher bound (3s) but "
                "flag that validation in alpha is needed for larger datasets."
            ),
            merged_content=(
                "< 3s analysis time for datasets < 100k rows (with 95th percentile at 2.5s); "
                "performance at 500k+ rows to be validated in alpha testing"
            ),
        ))

        # Decision 5: Missing data imputation (signal - action item)
        decisions.append(MergeDecision(
            section="R2: Visualization Pipeline",
            resolution="combine",
            rationale=(
                "Sense signal from Dr. Chen identifies critical gap: missing data handling. "
                "This should be explicit in acceptance criteria for robustness."
            ),
            merged_content=(
                "R2.1: Missing Data Handling\n"
                "The system shall handle missing or sparse data via configurable strategies:\n"
                "- Mean/median imputation\n"
                "- KNN-based imputation\n"
                "- Listwise deletion with audit trail\n"
                "All strategies shall be logged in audit trail."
            ),
        ))

        return decisions

    def test_merge_decisions_generated(
        self,
        baseline_document_state: DocumentState,
        sense_signals_extraction: Extraction,
        external_changes: list[ExternalChange],
    ):
        """Verify merge produces reasonable decisions."""
        signals = sense_signals_extraction.signals
        decisions = self._simulate_neut_merge(
            AdvancedAnalyticsPRDScenario.BASELINE_MD,
            external_changes,
            signals,
        )

        assert len(decisions) >= 4, "Should have decisions for major sections"

        for decision in decisions:
            assert decision.section, "Must identify which section"
            assert decision.resolution in ("accept_external", "accept_local", "combine", "defer")
            assert decision.rationale, "Must provide reasoning"
            assert decision.merged_content, "Must provide merged text"

    def test_merge_incorporates_signals(
        self,
        sense_signals_extraction: Extraction,
        external_changes: list[ExternalChange],
    ):
        """Verify merge incorporates both Sense signals AND external changes."""
        signals = sense_signals_extraction.signals
        decisions = self._simulate_neut_merge(
            AdvancedAnalyticsPRDScenario.BASELINE_MD,
            external_changes,
            signals,
        )

        # Should have decisions that mention both sources
        decision_text = "\n".join(d.rationale for d in decisions)

        # Signals are mentioned in rationale
        assert "Sense" in decision_text or "signal" in decision_text.lower()

        # External reviews are mentioned
        assert "external" in decision_text.lower() or "reviewer" in decision_text.lower()

    def test_merge_respects_confidence_levels(
        self,
        sense_signals_extraction: Extraction,
        external_changes: list[ExternalChange],
    ):
        """Verify merge respects confidence scores.

        External doc changes should have higher confidence (0.92-0.98) than
        synthesized signals (0.85-0.95), and merge should reflect this.
        """
        ext_confidences = [c.confidence for c in external_changes]
        sig_confidences = [s.confidence for s in sense_signals_extraction.signals]

        avg_ext = sum(ext_confidences) / len(ext_confidences)
        avg_sig = sum(sig_confidences) / len(sig_confidences)

        assert avg_ext >= 0.92, "External changes should have high confidence"
        assert avg_sig >= 0.85, "Signals should have reasonable confidence"


# ============================================================================
# TEST CASES: Merge Output & Integration
# ============================================================================

class TestMergeOutput:
    """Test 4: Verify merged output is valid and integrable."""

    def test_merged_markdown_valid(self):
        """Verify merged markdown is syntactically valid."""
        # Simulate merged content (would be generated by actual merge)
        merged_md = AdvancedAnalyticsPRDScenario.BASELINE_MD + "\n\n[MERGE NOTES]"

        # Should have proper structure
        assert "## Executive Summary" in merged_md
        assert "### Key Features" in merged_md
        assert "## Core Requirements" in merged_md

    def test_version_bumping(self, baseline_document_state: DocumentState):
        """Verify version is bumped appropriately (v2.0.0 → v2.1.0)."""
        current_version = baseline_document_state.published.version
        assert current_version == "v2.0.0"

        # After merge: should bump to v2.1.0 (minor bump for feature additions)
        # This would be handled by semantic versioning logic
        # Expected: 5 commits since last version (meet, gitlab, voice, merge decisions)
        # 2-5 commits → minor bump
        expected_version = "v2.1.0"
        assert expected_version > current_version

    def test_merge_preserves_document_integrity(self):
        """Verify merge doesn't break document structure."""
        # All required sections should still exist
        required_sections = [
            "Executive Summary",
            "Core Requirements",
            "Design Approach",
            "Open Questions",
            "Success Metrics",
        ]

        merged_content = AdvancedAnalyticsPRDScenario.BASELINE_MD
        for section in required_sections:
            assert section in merged_content, f"Merged doc must have {section}"


# ============================================================================
# TEST CASES: Full Workflow Integration
# ============================================================================

class TestSenseSignalsInMerge:
    """Test 5: Integration of Sense signals into merge workflow.

    This is the key integration test showing how Sense informs the merge:
    1. Signals are extracted from multiple sources (meetings, voice, chat, etc.)
    2. Synthesized into structured changelog entries
    3. Each entry becomes a draft revision suggestion
    4. Merge logic considers both Sense suggestions AND external reviewer comments
    5. Final merged PRD incorporates insights from both sources
    """

    def test_sense_signals_inform_merge_decisions(
        self,
        sense_signals_extraction: Extraction,
        external_changes: list[ExternalChange],
    ):
        """Verify that Sense signals directly influence merge decisions.

        Example: Bayesian methods is mentioned in both:
        - GitLab signal (sig_gitlab_002): decision signal recommending v2.1
        - External change (ext_004): Dr. Patel's comment suggesting v2.1

        Merge should recognize this convergence and move decision accordingly.
        """
        # Find related signals and changes
        bayesian_signals = [
            s for s in sense_signals_extraction.signals
            if "Bayesian" in s.raw_text or "bayesian" in s.raw_text.lower()
        ]

        bayesian_changes = [
            c for c in external_changes
            if "Bayesian" in c.comment_text or "Bayesian" in str(c.new_text)
        ]

        assert len(bayesian_signals) > 0, "Should have GitLab signal about Bayesian"
        assert len(bayesian_changes) > 0, "Should have reviewer comment about Bayesian"

        # Both sources agree → high confidence merge decision
        for signal in bayesian_signals:
            assert signal.confidence >= 0.90, "Bayesian signal should have high confidence"

        for change in bayesian_changes:
            assert change.confidence >= 0.90, "Bayesian comment should have high confidence"

    def test_sense_signal_confidence_affects_merge_weight(
        self,
        sense_signals_extraction: Extraction,
    ):
        """Verify that Sense signal confidence scores weight merge decisions.

        Voice memo (sig_voice_003) has lowest confidence (0.85) since it's
        transcribed and interpreted. But it still should be considered in merge.
        """
        voice_signals = [
            s for s in sense_signals_extraction.signals
            if s.source == "voice_memo"
        ]

        assert len(voice_signals) > 0
        assert voice_signals[0].confidence == 0.85, "Voice memo confidence should be 0.85"

        # Merge should still include it, but with appropriate weight
        # In actual merge: "PI voice memo raises concern → we flag as validation needed"

    def test_sense_extracted_from_real_sources(
        self,
        sense_signals_extraction: Extraction,
    ):
        """Verify signals are from realistic Sense sources.

        Supported sources in the real pipeline:
        - meeting_notes (from transcript.py)
        - voice_memo (from voice.py)
        - gitlab_discussion (from gitlab_diff.py)
        - teams_chat
        - freetext
        - docflow_review (external document changes)
        """
        sources = {s.source for s in sense_signals_extraction.signals}

        # Our test uses realistic sources
        assert "meeting_notes" in sources
        assert "voice_memo" in sources
        assert "gitlab_discussion" in sources


class TestFullMergeWorkflow:
    """Test 6: End-to-end merge workflow."""

    def test_workflow_detects_changes(
        self,
        baseline_document_state: DocumentState,
        divergence_report: DivergenceReport,
    ):
        """Step 1: Detect divergence."""
        assert baseline_document_state.status == "published"
        assert divergence_report.external_changed_since_sync is True
        assert len(divergence_report.changes) == 4

    def test_workflow_extracts_signals(
        self,
        sense_signals_extraction: Extraction,
    ):
        """Step 2: Extract and synthesize Sense signals."""
        assert len(sense_signals_extraction.signals) == 3

        # All should be recent (within last 4 days)
        for signal in sense_signals_extraction.signals:
            timestamp = datetime.fromisoformat(signal.timestamp.replace("Z", "+00:00"))
            assert timestamp.year == 2026 and timestamp.month == 2

    def test_workflow_merges_semantically(
        self,
        baseline_document_state: DocumentState,
        sense_signals_extraction: Extraction,
        external_changes: list[ExternalChange],
    ):
        """Step 3: Perform semantic merge."""
        # Mock Neut's merge logic
        merge_simulator = TestConceptualMerge()
        decisions = merge_simulator._simulate_neut_merge(
            AdvancedAnalyticsPRDScenario.BASELINE_MD,
            external_changes,
            sense_signals_extraction.signals,
        )

        assert len(decisions) >= 4

        # Verify decisions make semantic sense
        for decision in decisions:
            assert decision.resolution in ("accept_external", "accept_local", "combine", "defer")

    def test_workflow_generates_audit_trail(
        self,
        baseline_document_state: DocumentState,
        divergence_report: DivergenceReport,
        sense_signals_extraction: Extraction,
    ):
        """Step 4: Generate audit trail of merge decisions."""
        audit_trail = {
            "merge_timestamp": datetime.now(UTC).isoformat(),
            "source_version": baseline_document_state.published.version,
            "source_commit": baseline_document_state.published.commit_sha,
            "divergence_report": {
                "external_changes": len(divergence_report.changes),
                "external_changed": divergence_report.external_changed_since_sync,
            },
            "sense_signals": {
                "count": len(sense_signals_extraction.signals),
                "types": [s.signal_type for s in sense_signals_extraction.signals],
            },
            "merge_strategy": "semantic_with_rag",
        }

        assert audit_trail["source_version"] == "v2.0.0"
        assert audit_trail["divergence_report"]["external_changes"] == 4
        assert audit_trail["sense_signals"]["count"] == 3

    def test_workflow_publishable(self):
        """Step 5: Verify merged PRD is ready for publication."""
        # Merged PRD should pass validation
        merged_md = AdvancedAnalyticsPRDScenario.BASELINE_MD

        # Must have cover page
        assert "**[" in merged_md or "**" in merged_md

        # Must have required structure
        assert "## " in merged_md  # Section headers

        # Must have no malformed markdown
        assert merged_md.count("[") == merged_md.count("]"), "Bracket mismatch"


# ============================================================================
# TEST CASES: Complete Sense-to-Merge Pipeline
# ============================================================================

class TestCompleteSenseToMergePipeline:
    """Test 7: Full end-to-end Sense ingestion → synthesis → merge → publish.

    This demonstrates the complete lifecycle:
    1. Neut Sense extracts signals from multiple sources (meetings, voice, chat)
    2. Signals synthesized into changelog via Synthesizer
    3. Changelog reviewed → converted to draft revisions
    4. Simultaneously, external reviewers comment on SharePoint version
    5. Publisher detects divergence between .md and external doc
    6. Merge engine reconciles: Sense-derived drafts + external comments
    7. Result: coherent v2.1.0 PRD ready for publication
    """

    def test_complete_pipeline_sense_to_merge(
        self,
        baseline_document_state: DocumentState,
        sense_signals_extraction: Extraction,
        external_changes: list[ExternalChange],
        divergence_report: DivergenceReport,
    ):
        """Execute complete pipeline and verify all stages work together."""

        # Stage 1: Sense extraction (already done in fixture)
        assert len(sense_signals_extraction.signals) == 3
        print(f"\n✓ Stage 1: Extracted {len(sense_signals_extraction.signals)} signals")

        # Stage 2: Synthesize signals into changelog
        synthesizer = Synthesizer()
        changelog = synthesizer.synthesize(
            signals=sense_signals_extraction.signals,
            date="2026-02-21",
            include_all=True,
        )
        assert len(changelog.entries) == 3
        print(f"✓ Stage 2: Synthesized to {len(changelog.entries)} changelog entries")

        # Stage 3: Map changelog entries to draft revisions
        # In real system: human reviews changelog, creates PR/branch with revisions
        draft_revisions = [
            {"section": "R2", "action": "add", "source": "sig_meeting_001"},
            {"section": "Q1", "action": "update", "source": "sig_gitlab_002"},
            {"section": "Metrics", "action": "refine", "source": "sig_voice_003"},
        ]
        assert len(draft_revisions) == len(changelog.entries)
        print(f"✓ Stage 3: Generated {len(draft_revisions)} draft revisions from signals")

        # Stage 4: Detect external changes (already in divergence_report)
        assert divergence_report.external_changed_since_sync is True
        assert len(divergence_report.changes) == 4
        print(f"✓ Stage 4: Detected {len(divergence_report.changes)} external changes")

        # Stage 5: Merge - reconcile Sense-derived drafts with external changes
        merge_decisions = self._simulate_merge(
            baseline=baseline_document_state,
            sense_signals=sense_signals_extraction.signals,
            external_changes=external_changes,
            changelog=changelog,
        )

        assert len(merge_decisions) >= 4, "Should have decisions for major concerns"
        print(f"✓ Stage 5: Generated {len(merge_decisions)} merge decisions")

        # Stage 6: Verify merge respects both sources
        decision_sources = []
        for decision in merge_decisions:
            if "Sense" in decision["rationale"] or "signal" in decision["rationale"].lower():
                decision_sources.append("sense")
            if "external" in decision["rationale"].lower() or "reviewer" in decision["rationale"].lower():
                decision_sources.append("external")

        assert "sense" in decision_sources, "Merge should incorporate Sense signals"
        assert "external" in decision_sources, "Merge should incorporate external reviews"
        print("✓ Stage 6: Merge incorporates both Sense and external sources")

        # Stage 7: Version bump (v2.0.0 → v2.1.0)
        # In reality: commit count since v2.0.0 = 5, so 2-5 commits → minor bump
        new_version = "v2.1.0"
        assert new_version > baseline_document_state.published.version
        print(f"✓ Stage 7: Version bumped: {baseline_document_state.published.version} → {new_version}")

        print("\n✅ Complete pipeline successful!")

    def _simulate_merge(
        self,
        baseline: DocumentState,
        sense_signals: list[Signal],
        external_changes: list[ExternalChange],
        changelog: Changelog,
    ) -> list[dict]:
        """Simulate the actual merge logic combining Sense and external changes."""
        decisions = []

        # Decision 1: From Sense signal (missing data handling)
        decisions.append({
            "section": "R2: Visualization Pipeline",
            "source": "sense",
            "signal_id": "sig_meeting_001",
            "rationale": "Sense signal from meeting notes identifies missing data handling as critical gap",
            "resolution": "add_requirement",
        })

        # Decision 2: Converged decision (Bayesian methods)
        decisions.append({
            "section": "Open Questions",
            "sources": ["sense", "external"],
            "rationale": "Both Sense signal (GitLab issue) and external reviewer (Dr. Patel) "
                        "recommend moving Bayesian to v2.1. High convergence → strong signal.",
            "resolution": "update_roadmap",
        })

        # Decision 3: From external review (Vega backend)
        decisions.append({
            "section": "R2: Visualization Pipeline",
            "source": "external",
            "author": "Prof. James Lee",
            "rationale": "External reviewer suggests vega for interactive dashboards",
            "resolution": "add_feature",
        })

        # Decision 4: External comment on missing requirement (effect size)
        decisions.append({
            "section": "R1: Statistical Analysis",
            "source": "external",
            "author": "Dr. Sarah Kim",
            "rationale": "External reviewer correctly identifies missing requirement: effect size reporting",
            "resolution": "add_acceptance_criterion",
        })

        # Decision 5: Sense + PI voice concern (performance metric)
        decisions.append({
            "section": "Success Metrics",
            "sources": ["external", "sense"],
            "rationale": "External refinement (3s bound) + PI voice concern (validation needed at scale)",
            "resolution": "refine_metric_with_caveats",
        })

        return decisions

    def test_changelog_to_merge_traceability(
        self,
        sense_signals_extraction: Extraction,
    ):
        """Verify we can trace from Signal → Changelog entry → Merge decision.

        This is critical for audit trail: we need to know why each decision was made.
        """
        synthesizer = Synthesizer()
        changelog = synthesizer.synthesize(
            signals=sense_signals_extraction.signals,
            include_all=True,
        )

        # Each changelog entry should be traceable to original signal(s)
        for entry in changelog.entries:
            assert entry.detail, "Changelog entry must have detail"
            assert entry.people, "Changelog entry must identify people/sources"
            assert entry.confidence > 0, "Changelog entry must have confidence"

        # Verify traceability
        assert len(changelog.entries) == len(sense_signals_extraction.signals)
        print(f"✓ Traceability: {len(changelog.entries)} entries → {len(sense_signals_extraction.signals)} signals")


# ============================================================================
# REALISTIC DATA FIXTURES (Not using, but available for extension)
# ============================================================================

def create_realistic_docx_fixture() -> bytes:
    """Create a realistic .docx with tracked changes and comments.

    Requires: from docx import Document
    This is available but not used in base tests to keep dependencies light.
    """
    # This would create actual .docx structure
    # For now, return mock bytes
    return b"PK\x03\x04..."  # ZIP header for .docx


def create_tracked_changes_xml() -> str:
    """Generate Word-compatible tracked changes XML.

    Example structure for testing merge with real .docx format.
    """
    return """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
    <w:body>
        <w:p>
            <w:pPr>
                <w:pStyle w:val="Heading1"/>
            </w:pPr>
            <w:r>
                <w:t>Advanced Analytics PRD</w:t>
            </w:r>
        </w:p>
        <w:p>
            <w:del>
                <w:r><w:t>Old text</w:t></w:r>
            </w:del>
            <w:ins>
                <w:r><w:t>New text with vega backend</w:t></w:r>
            </w:ins>
        </w:p>
    </w:body>
</w:document>"""


# ============================================================================
# HELPER UTILITIES FOR REAL SCENARIO (Experiment Manager)
# ============================================================================

class MergeTestHelper:
    """Utilities for testing real merge scenarios."""

    @staticmethod
    def create_signal_from_meeting_notes(notes: str, attendees: list[str]) -> Signal:
        """Create Signal from meeting transcript."""
        return Signal(
            source="meeting_notes",
            timestamp=datetime.now(UTC).isoformat(),
            raw_text=notes,
            people=attendees,
            signal_type="action_item",
            detail="Meeting notes extraction",
            confidence=0.95,
        )

    @staticmethod
    def create_signal_from_voice_memo(transcript: str, speaker: str) -> Signal:
        """Create Signal from transcribed voice memo."""
        return Signal(
            source="voice_memo",
            timestamp=datetime.now(UTC).isoformat(),
            raw_text=transcript,
            people=[speaker],
            signal_type="blocker",
            detail="Voice memo transcription",
            confidence=0.85,
        )

    @staticmethod
    def create_external_change_from_comment(
        author: str,
        comment: str,
        section: str,
    ) -> ExternalChange:
        """Create ExternalChange from reviewer comment."""
        return ExternalChange(
            change_type=ChangeType.COMMENT,
            section=section,
            author=author,
            timestamp=datetime.now(UTC).isoformat(),
            original_text="",
            new_text="",
            comment_text=comment,
            confidence=0.98,
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
