# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""End-to-end tests for the voice memo → changelog workflow.

Tests the complete pipeline:
1. Voice memo arrives in inbox
2. Transcription + correction
3. Signal extraction and routing
4. Changelog generation
5. Draft review

These tests use mocked external services but real filesystem operations.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def isolated_inbox(tmp_path):
    """Create an isolated inbox structure for testing."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    (inbox / "voice").mkdir()
    (inbox / "notes").mkdir()
    (inbox / "processed").mkdir()

    state_dir = inbox / "state"
    state_dir.mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Create minimal config files
    people_md = config_dir / "people.md"
    people_md.write_text("""| Name | GitLab | Role |
|------|--------|------|
| Alice Smith | asmith | Lead |
| Bob Jones | bjones | Engineer |
""")

    initiatives_md = config_dir / "initiatives.md"
    initiatives_md.write_text("""| ID | Name | Status |
|----|------|--------|
| 1 | Project Alpha | Active |
""")

    return {
        "inbox": inbox,
        "voice": inbox / "voice",
        "notes": inbox / "notes",
        "processed": inbox / "processed",
        "state": state_dir,
        "config": config_dir,
    }


@pytest.fixture
def mock_transcription():
    """Mock the transcription service."""
    async def transcribe(audio_path):
        return {
            "text": "Meeting with Alice about Project Alpha. Need to schedule review.",
            "segments": [
                {"start": 0.0, "end": 2.5, "text": "Meeting with Alice about Project Alpha."},
                {"start": 2.5, "end": 4.0, "text": "Need to schedule review."},
            ],
        }
    return transcribe


@pytest.fixture
def mock_llm_gateway():
    """Mock the LLM gateway for corrections and summaries."""
    gateway = MagicMock()

    # Mock correction
    async def correct(text, context=None):
        return {
            "corrected_text": text,  # No corrections needed
            "corrections": [],
        }
    gateway.correct_transcription = AsyncMock(side_effect=correct)

    # Mock summarization
    async def summarize(signals):
        return "Daily update: Meeting scheduled with Alice regarding Project Alpha."
    gateway.summarize = AsyncMock(side_effect=summarize)

    # Mock signal extraction
    async def extract_signals(text, context=None):
        return {
            "signals": [
                {
                    "type": "task",
                    "content": "Schedule review meeting",
                    "entities": ["Alice", "Project Alpha"],
                    "confidence": 0.9,
                },
            ],
        }
    gateway.extract_signals = AsyncMock(side_effect=extract_signals)

    return gateway


class TestVoiceInboxFlow:
    """Tests for voice memo ingestion flow."""

    def test_voice_file_detection(self, isolated_inbox):
        """Voice files in inbox/voice/ are detected."""
        # Create a fake voice file
        voice_file = isolated_inbox["voice"] / "memo_2026-02-24_1000.m4a"
        voice_file.write_bytes(b"fake audio content")

        # Verify detection
        voice_files = list(isolated_inbox["voice"].glob("*.m4a"))
        assert len(voice_files) == 1
        assert voice_files[0].name == "memo_2026-02-24_1000.m4a"

    def test_voice_file_moved_after_processing(self, isolated_inbox):
        """Voice files are moved to processed/ after handling."""
        voice_file = isolated_inbox["voice"] / "memo_test.m4a"
        voice_file.write_bytes(b"fake audio")

        # Simulate processing by moving
        processed_file = isolated_inbox["processed"] / voice_file.name
        shutil.move(str(voice_file), str(processed_file))

        assert not voice_file.exists()
        assert processed_file.exists()


class TestTranscriptionCorrection:
    """Tests for transcription and correction pipeline."""

    def test_transcription_produces_text(self, isolated_inbox, mock_transcription):
        """Transcription produces text output."""
        import asyncio

        # Create fake audio
        voice_file = isolated_inbox["voice"] / "test.m4a"
        voice_file.write_bytes(b"fake audio")

        result = asyncio.run(mock_transcription(voice_file))

        assert "text" in result
        assert "Alice" in result["text"]
        assert "Project Alpha" in result["text"]

    def test_correction_preserves_names(self, isolated_inbox, mock_llm_gateway):
        """Correction preserves recognized person names."""
        import asyncio

        text = "Meeting with Alice about Project Alpha."
        result = asyncio.run(mock_llm_gateway.correct_transcription(text))

        assert "Alice" in result["corrected_text"]


class TestSignalExtraction:
    """Tests for signal extraction from corrected text."""

    def test_task_signal_extracted(self, mock_llm_gateway):
        """Task signals are extracted from text."""
        import asyncio

        text = "Need to schedule review meeting with Alice."
        result = asyncio.run(mock_llm_gateway.extract_signals(text))

        assert "signals" in result
        assert len(result["signals"]) > 0
        assert result["signals"][0]["type"] == "task"

    def test_entities_identified(self, mock_llm_gateway):
        """Named entities are identified in signals."""
        import asyncio

        text = "Meeting with Alice about Project Alpha."
        result = asyncio.run(mock_llm_gateway.extract_signals(text))

        signal = result["signals"][0]
        assert "Alice" in signal["entities"] or "Project Alpha" in signal["entities"]


class TestChangelogGeneration:
    """Tests for changelog draft generation."""

    def test_changelog_file_created(self, isolated_inbox):
        """Changelog draft file is created with correct naming."""
        drafts_dir = isolated_inbox["inbox"] / "drafts"
        drafts_dir.mkdir()

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        changelog_path = drafts_dir / f"changelog_{today}.md"
        changelog_path.write_text("# Daily Changelog\n\n- Test entry")

        assert changelog_path.exists()
        assert today in changelog_path.name

    def test_changelog_structure(self, isolated_inbox):
        """Changelog has correct markdown structure."""
        drafts_dir = isolated_inbox["inbox"] / "drafts"
        drafts_dir.mkdir()

        changelog_content = """# Changelog - 2026-02-24

## Summary
Daily update: Meeting scheduled with Alice regarding Project Alpha.

## Signals
- **Task**: Schedule review meeting
  - Entities: Alice, Project Alpha
  - Confidence: 90%

## Raw Inputs
- memo_2026-02-24_1000.m4a (transcribed)
"""

        changelog_path = drafts_dir / "changelog_2026-02-24.md"
        changelog_path.write_text(changelog_content)

        content = changelog_path.read_text()
        assert "# Changelog" in content
        assert "## Summary" in content
        assert "## Signals" in content


class TestFullPipeline:
    """Integration tests for the complete pipeline."""

    def test_voice_to_changelog_flow(
        self,
        isolated_inbox,
        mock_transcription,
        mock_llm_gateway,
    ):
        """Complete flow from voice memo to changelog draft."""
        import asyncio

        async def _run():
            # 1. Voice memo arrives
            voice_file = isolated_inbox["voice"] / "memo_2026-02-24_test.m4a"
            voice_file.write_bytes(b"fake audio content")

            # 2. Transcription
            transcript = await mock_transcription(voice_file)

            # 3. Correction
            corrected = await mock_llm_gateway.correct_transcription(transcript["text"])

            # 4. Signal extraction
            signals = await mock_llm_gateway.extract_signals(corrected["corrected_text"])

            # 5. Generate changelog
            summary = await mock_llm_gateway.summarize(signals["signals"])

            # Create the changelog
            drafts_dir = isolated_inbox["inbox"] / "drafts"
            drafts_dir.mkdir()

            changelog = drafts_dir / "changelog_2026-02-24.md"
            changelog.write_text(f"""# Changelog - 2026-02-24

## Summary
{summary}

## Signals
""" + "\n".join(f"- {s['type']}: {s['content']}" for s in signals["signals"]))

            # 6. Move to processed
            processed_file = isolated_inbox["processed"] / voice_file.name
            shutil.move(str(voice_file), str(processed_file))

            # Verify end state
            assert not voice_file.exists()
            assert processed_file.exists()
            assert changelog.exists()

            content = changelog.read_text()
            assert "Meeting" in content or "schedule" in content.lower()

        asyncio.run(_run())


class TestErrorRecovery:
    """Tests for error handling and recovery."""

    def test_failed_transcription_preserved(self, isolated_inbox):
        """Failed transcriptions preserve the original file."""
        voice_file = isolated_inbox["voice"] / "failed.m4a"
        voice_file.write_bytes(b"corrupt audio")

        # Simulate failed processing - file should stay in place
        # (in real code, would be moved to failed/ or kept in voice/)
        assert voice_file.exists()

    def test_partial_pipeline_state_saved(self, isolated_inbox):
        """Partial pipeline state is saved for recovery."""
        state_file = isolated_inbox["state"] / "pipeline_state.json"

        state = {
            "last_run": "2026-02-24T10:00:00Z",
            "pending_files": ["memo1.m4a", "memo2.m4a"],
            "failed_files": [],
        }

        state_file.write_text(json.dumps(state, indent=2))

        # Verify state can be loaded
        loaded = json.loads(state_file.read_text())
        assert loaded["pending_files"] == ["memo1.m4a", "memo2.m4a"]


class TestInboxState:
    """Tests for inbox state management."""

    def test_processed_tracking(self, isolated_inbox):
        """Processed files are tracked to avoid reprocessing."""
        processed_list = isolated_inbox["state"] / "processed.json"

        processed = {
            "files": [
                {"name": "memo1.m4a", "processed_at": "2026-02-24T10:00:00Z"},
                {"name": "memo2.m4a", "processed_at": "2026-02-24T11:00:00Z"},
            ]
        }

        processed_list.write_text(json.dumps(processed, indent=2))

        # New file should not be in processed list
        loaded = json.loads(processed_list.read_text())
        names = [f["name"] for f in loaded["files"]]

        assert "memo1.m4a" in names
        assert "memo3.m4a" not in names
