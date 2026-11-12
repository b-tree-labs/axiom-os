# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end tests for the `neut signal` CLI.

These tests exercise the full pipeline: CLI invocation → extractors →
correlator → synthesizer → filesystem output. They verify user-facing
behavior through the complete ingest → draft workflow.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
AXI_CLI = str(REPO_ROOT / "src" / "axiom" / "axiom_cli.py")


# ─── Subprocess Tests (true external invocation) ───


class TestSenseCLISubprocess:
    """Tests that invoke the real CLI binary via subprocess."""

    def test_signal_status_subprocess(self):
        """neut signal status exits 0 and shows expected sections."""
        result = subprocess.run(
            [sys.executable, AXI_CLI, "signal", "status"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        assert result.returncode == 0
        assert "Inbox" in result.stdout
        assert "Processed" in result.stdout
        assert "Config" in result.stdout
        assert "LLM gateway" in result.stdout

    def test_signal_help_subprocess(self):
        """neut signal (no subcommand) shows help."""
        result = subprocess.run(
            [sys.executable, AXI_CLI, "signal"],
            capture_output=True, text=True, cwd=str(REPO_ROOT),
        )
        # Should exit non-zero (no subcommand) and print usage
        assert result.returncode != 0
        assert "pipeline" in (result.stdout + result.stderr).lower()


# ─── Full Pipeline Tests ───


class TestSenseIngestPipeline:
    """End-to-end: GitLab export → ingest → signals JSON on disk."""

    @pytest.fixture
    def workspace(self, tmp_path):
        """Set up an isolated workspace mimicking the signal directory layout."""
        agents_dir = tmp_path / "agents"

        # Config
        config_dir = agents_dir / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "people.md").write_text(
            "| Name | GitLab | Linear | Role | Initiative(s) |\n"
            "|------|--------|--------|------|---------------|\n"
            "| Alice Smith | asmith | — | Lead | Project Alpha |\n"
            "| Bob Jones | bjones | bob.j | Engineer | Data Platform |\n"
        )
        (config_dir / "initiatives.md").write_text(
            "| ID | Name | Status | Owners | GitLab Repos |\n"
            "|----|------|--------|--------|-------------|\n"
            "| 1 | Project Alpha | Active | Smith | project-alpha/* |\n"
            "| 2 | Data Platform | Active | Jones | data-platform/* |\n"
        )

        # Inbox
        inbox_raw = agents_dir / "inbox" / "raw"
        (inbox_raw / "voice").mkdir(parents=True)
        (inbox_raw / "teams").mkdir(parents=True)
        inbox_processed = agents_dir / "inbox" / "processed"
        inbox_processed.mkdir(parents=True)

        # Drafts
        drafts = agents_dir / "drafts"
        drafts.mkdir(parents=True)

        # Exports
        exports = tmp_path / "exports"
        exports.mkdir()

        return tmp_path

    def _write_export(self, workspace, filename, data):
        """Write a GitLab export JSON."""
        path = workspace / "exports" / filename
        path.write_text(json.dumps(data, indent=2))
        return path

    def _make_gitlab_export(self, date_str, commits, issues=None):
        """Build a minimal GitLab export structure."""
        # Build contributor_summary from commits
        author_counts = {}
        for c in commits:
            name = c.get("author_name", "Unknown")
            author_counts[name] = author_counts.get(name, 0) + 1

        return {
            "exported_at": f"{date_str}T00:00:00+00:00",
            "gitlab_url": "https://gitlab.example.com",
            "group": "test-group",
            "time_window_days": 90,
            "projects": [
                {
                    "info": {
                        "id": 1,
                        "name": "Project Alpha",
                        "path": "project-alpha",
                        "path_with_namespace": "test-group/project-alpha",
                        "description": "Digital twin",
                        "default_branch": "main",
                        "last_activity_at": f"{date_str}T10:00:00Z",
                        "web_url": "https://gitlab.example.com/test-group/project-alpha",
                    },
                    "activity": {
                        "commits": commits,
                        "contributor_summary": author_counts,
                        "open_issues": issues or [],
                        "recently_closed_issues": [],
                        "open_mrs": [],
                        "recently_merged_mrs": [],
                        "milestones": [],
                        "labels": [],
                        "active_branches": [],
                    },
                }
            ],
            "summary": {
                "total_commits_by_author": author_counts,
                "stale_repos": [],
                "project_stats": [],
                "newly_discovered_projects": [],
                "total_projects": 1,
                "total_commits": len(commits),
                "total_open_issues": len(issues or []),
                "total_open_mrs": 0,
            },
        }

    def test_gitlab_ingest_single_export(self, workspace):
        """Ingest a single GitLab export — produces signals."""
        from axiom.extensions.builtins.signals.correlator import Correlator
        from axiom.extensions.builtins.signals.extractors.gitlab_diff import (
            GitLabDiffExtractor,
        )

        export_data = self._make_gitlab_export("2026-02-17", [
            {
                "sha": "abc123",
                "author_name": "Alice Smith",
                "author_email": "alice@example.com",
                "created_at": "2026-02-16T10:00:00+00:00",
                "title": "Add thermal model",
            },
        ])
        export_path = self._write_export(workspace, "gitlab_export_2026-02-17.json", export_data)

        extractor = GitLabDiffExtractor()
        correlator = Correlator(config_dir=workspace / "agents" / "config")

        extraction = extractor.extract(export_path)
        assert len(extraction.signals) > 0
        assert extraction.errors == []

        # Correlator resolves people
        for sig in extraction.signals:
            sig.people = correlator.resolve_people(sig.people)
        assert any("Alice Smith" in sig.people for sig in extraction.signals)

    def test_gitlab_diff_ingest(self, workspace):
        """Diff two GitLab exports — detects new commits and issues."""
        from axiom.extensions.builtins.signals.extractors.gitlab_diff import (
            GitLabDiffExtractor,
        )

        old = self._make_gitlab_export("2026-02-10", [
            {
                "sha": "old111",
                "author_name": "Alice Smith",
                "author_email": "alice@example.com",
                "created_at": "2026-02-05T10:00:00+00:00",
                "title": "Initial setup",
            },
        ])
        new = self._make_gitlab_export("2026-02-17", [
            {
                "sha": "old111",
                "author_name": "Alice Smith",
                "author_email": "alice@example.com",
                "created_at": "2026-02-05T10:00:00+00:00",
                "title": "Initial setup",
            },
            {
                "sha": "new222",
                "author_name": "Bob Jones",
                "author_email": "bob@example.com",
                "created_at": "2026-02-15T10:00:00+00:00",
                "title": "Add data ingestion pipeline",
            },
        ], issues=[
            {
                "iid": 1,
                "title": "Set up CI/CD",
                "labels": [],
                "assignees": ["bjones"],
                "author": "asmith",
                "created_at": "2026-02-12T10:00:00Z",
                "updated_at": "2026-02-15T10:00:00Z",
                "milestone": None,
                "description": "",
            },
        ])

        old_path = self._write_export(workspace, "gitlab_export_2026-02-10.json", old)
        new_path = self._write_export(workspace, "gitlab_export_2026-02-17.json", new)

        extractor = GitLabDiffExtractor()
        extraction = extractor.extract(new_path, previous=old_path)

        types = {s.signal_type for s in extraction.signals}
        assert "progress" in types  # commits produce "progress" signals
        assert "action_item" in types  # new issues produce "action_item" signals

        # New commit detected as a progress signal
        progress_signals = [s for s in extraction.signals if s.signal_type == "progress"]
        assert any("Bob Jones" in s.people for s in progress_signals)

    def test_freetext_ingest(self, workspace):
        """Drop a text file in inbox/raw → freetext extractor picks it up."""
        from axiom.extensions.builtins.signals.correlator import Correlator
        from axiom.extensions.builtins.signals.extractors.freetext import FreetextExtractor

        note = workspace / "agents" / "inbox" / "raw" / "meeting-notes.md"
        note.write_text(
            "# Meeting Notes 2026-02-17\n\n"
            "Alice presented Project Alpha progress. Bob raised concerns about "
            "data platform latency.\n\n"
            "Action items:\n"
            "- Alice to run benchmark by Friday\n"
            "- Bob to profile ingestion pipeline\n"
        )

        extractor = FreetextExtractor()
        correlator = Correlator(config_dir=workspace / "agents" / "config")

        assert extractor.can_handle(note)
        extraction = extractor.extract(note, correlator=correlator)

        assert len(extraction.signals) >= 1
        # Signal should mention at least one person from the text
        all_people = []
        for sig in extraction.signals:
            all_people.extend(sig.people)
        assert any("Alice Smith" in p for p in all_people)

    def test_transcript_ingest(self, workspace):
        """Place a transcript file in inbox/raw/teams → transcript extractor processes it."""
        from axiom.extensions.builtins.signals.extractors.transcript import (
            TranscriptExtractor,
        )

        transcript = workspace / "agents" / "inbox" / "raw" / "teams" / "standup_transcript.md"
        transcript.write_text(
            "# Weekly Standup - Feb 17\n\n"
            "Alice: Project Alpha model is passing validation tests.\n"
            "Bob: Working on data platform migration to Iceberg.\n"
            "Alice: Need to review the thermal analysis results.\n"
        )

        extractor = TranscriptExtractor()
        assert extractor.can_handle(transcript)

        extraction = extractor.extract(transcript)
        assert len(extraction.signals) >= 1


class TestSenseDraftPipeline:
    """End-to-end: signals → synthesizer → changelog + weekly summary on disk."""

    def test_synthesize_from_signals(self, tmp_path):
        """Given raw signals, synthesizer produces changelog and summary files."""
        from axiom.extensions.builtins.signals.models import Signal
        from axiom.extensions.builtins.signals.synthesizer import Synthesizer

        signals = [
            Signal(
                source="gitlab",
                timestamp="2026-02-17T10:00:00Z",
                raw_text="Add thermal model validation",
                signal_type="progress",
                detail="Alice added thermal model to Project Alpha",
                people=["Alice Smith"],
                initiatives=["Project Alpha"],
                confidence=1.0,
            ),
            Signal(
                source="gitlab",
                timestamp="2026-02-17T11:00:00Z",
                raw_text="#42: Set up CI/CD",
                signal_type="action_item",
                detail="New issue: Set up CI/CD for data-platform",
                people=["Bob Jones"],
                initiatives=["Data Platform"],
                confidence=1.0,
            ),
            Signal(
                source="freetext",
                timestamp="2026-02-17T12:00:00Z",
                raw_text="Alice needs to run benchmark by Friday",
                signal_type="action_item",
                detail="Run benchmark by Friday",
                people=["Alice Smith"],
                initiatives=["Project Alpha"],
                confidence=0.8,
            ),
        ]

        synthesizer = Synthesizer(drafts_dir=tmp_path)
        changelog = synthesizer.synthesize(signals)

        # Changelog groups by initiative
        assert len(changelog.entries) > 0
        initiatives_in_changelog = {e.initiative for e in changelog.entries}
        assert "Project Alpha" in initiatives_in_changelog
        assert "Data Platform" in initiatives_in_changelog

        # Write changelog and summary files
        cl_path = synthesizer.write_changelog(changelog)
        summary_path = synthesizer.write_weekly_summary(changelog)

        assert cl_path.exists()
        assert summary_path.exists()

        # Changelog is markdown with table format
        cl_text = cl_path.read_text()
        assert "Project Alpha" in cl_text
        assert "Data Platform" in cl_text
        assert "|" in cl_text  # Table format

        # Summary is prose
        summary_text = summary_path.read_text()
        assert len(summary_text) > 50

    def test_full_ingest_to_draft_pipeline(self, tmp_path):
        """Complete pipeline: export → ingest → save signals → load → synthesize → files."""
        from axiom.extensions.builtins.signals.correlator import Correlator
        from axiom.extensions.builtins.signals.extractors.gitlab_diff import (
            GitLabDiffExtractor,
        )
        from axiom.extensions.builtins.signals.models import Signal
        from axiom.extensions.builtins.signals.synthesizer import Synthesizer

        # 1. Set up config
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "people.md").write_text(
            "| Name | GitLab | Linear | Role | Initiative(s) |\n"
            "|------|--------|--------|------|---------------|\n"
            "| Alice Smith | asmith | — | Lead | Project Alpha |\n"
        )
        (config_dir / "initiatives.md").write_text(
            "| ID | Name | Status | Owners | GitLab Repos |\n"
            "|----|------|--------|--------|-------------|\n"
            "| 1 | Project Alpha | Active | Smith | project-alpha/* |\n"
        )

        # 2. Create export
        export = {
            "exported_at": "2026-02-17T00:00:00+00:00",
            "gitlab_url": "https://gitlab.example.com",
            "group": "test",
            "time_window_days": 90,
            "projects": [{
                "info": {
                    "id": 1, "name": "Project Alpha", "path": "project-alpha",
                    "path_with_namespace": "test/project-alpha",
                    "description": "", "default_branch": "main",
                    "last_activity_at": "2026-02-16T10:00:00Z",
                    "web_url": "https://gitlab.example.com/test/project-alpha",
                },
                "activity": {
                    "commits": [{
                        "sha": "aaa111", "author_name": "Alice Smith",
                        "author_email": "alice@example.com",
                        "created_at": "2026-02-16T10:00:00+00:00",
                        "title": "Add thermal model validation",
                    }],
                    "contributor_summary": {"Alice Smith": 1},
                    "open_issues": [], "recently_closed_issues": [],
                    "open_mrs": [], "recently_merged_mrs": [],
                    "milestones": [], "labels": [], "active_branches": [],
                },
            }],
            "summary": {
                "total_commits_by_author": {"Alice Smith": 1},
                "stale_repos": [], "project_stats": [],
                "newly_discovered_projects": [],
                "total_projects": 1, "total_commits": 1,
                "total_open_issues": 0, "total_open_mrs": 0,
            },
        }
        export_path = tmp_path / "export.json"
        export_path.write_text(json.dumps(export))

        # 3. Ingest
        extractor = GitLabDiffExtractor()
        correlator = Correlator(config_dir=config_dir)
        extraction = extractor.extract(export_path)

        for sig in extraction.signals:
            sig.people = correlator.resolve_people(sig.people)
            sig.initiatives = correlator.resolve_initiatives(sig.initiatives)

        assert len(extraction.signals) > 0

        # 4. Save signals (mimics what CLI does)
        processed_dir = tmp_path / "processed"
        processed_dir.mkdir()
        signals_file = processed_dir / "signals_2026-02-17.json"
        signals_file.write_text(json.dumps(
            [s.to_dict() for s in extraction.signals], indent=2
        ))

        # 5. Load signals back (mimics what CLI draft command does)
        loaded_data = json.loads(signals_file.read_text())
        loaded_signals = [Signal.from_dict(d) for d in loaded_data]
        assert len(loaded_signals) == len(extraction.signals)

        # 6. Synthesize
        synthesizer = Synthesizer(drafts_dir=tmp_path / "drafts")
        changelog = synthesizer.synthesize(loaded_signals)
        cl_path = synthesizer.write_changelog(changelog)
        summary_path = synthesizer.write_weekly_summary(changelog)

        # 7. Verify outputs
        assert cl_path.exists()
        assert summary_path.exists()
        assert "Alice Smith" in cl_path.read_text() or "Project Alpha" in cl_path.read_text()


class TestSenseRealData:
    """Integration tests against real repo data (skipped if not available)."""

    def test_ingest_real_gitlab_export(self):
        """Run GitLab diff extractor against the real export file."""
        exports_dir = REPO_ROOT / "src" / "axiom" / "exports"
        exports = sorted(exports_dir.glob("gitlab_export_*.json"), reverse=True)
        if not exports:
            pytest.skip("No real GitLab exports found")

        from axiom.extensions.builtins.signals.correlator import Correlator
        from axiom.extensions.builtins.signals.extractors.gitlab_diff import (
            GitLabDiffExtractor,
        )

        extractor = GitLabDiffExtractor()
        correlator = Correlator()

        extraction = extractor.extract(exports[0])
        assert len(extraction.signals) > 0
        assert extraction.errors == []

        # Resolve with real correlator
        for sig in extraction.signals:
            sig.people = correlator.resolve_people(sig.people)

        # Should contain real team member names
        all_people = set()
        for sig in extraction.signals:
            all_people.update(sig.people)
        assert len(all_people) > 0
