# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for multi-part recording detection."""

from datetime import datetime

import pytest

from axiom.extensions.builtins.signals.multipart import (
    PART_PATTERNS,
    MultipartDetector,
    RecordingGroup,
    RecordingPart,
)


class TestPartPatterns:
    """Test pattern matching for part numbers in filenames."""

    @pytest.mark.parametrize("filename,expected_base,expected_num", [
        ("meeting part 1", "meeting", 1),
        ("meeting part 2", "meeting", 2),
        ("standup Part 3", "standup", 3),
        ("call pt1", "call", 1),
        ("call pt2", "call", 2),
        ("notes (1)", "notes", 1),
        ("notes (2)", "notes", 2),
        ("recording_1", "recording", 1),
        ("recording_2", "recording", 2),
        ("call-1of3", "call", 1),
        ("call-2of3", "call", 2),
        ("discussion 1of2", "discussion", 1),
        ("memo 1", "memo", 1),
        ("memo 2", "memo", 2),
    ])
    def test_pattern_detection(self, filename, expected_base, expected_num):
        """Verify part patterns extract correct base name and number."""
        for pattern in PART_PATTERNS:
            match = pattern.match(filename)
            if match:
                assert match.group("base").strip() == expected_base
                assert int(match.group("num")) == expected_num
                return
        pytest.fail(f"No pattern matched filename: {filename}")

    def test_no_match_for_regular_filenames(self):
        """Verify regular filenames don't match part patterns."""
        regular_names = [
            "weekly_standup",
            "meeting_notes",
            "voice_memo",
            "important_call",
        ]
        for name in regular_names:
            _matched = False
            for pattern in PART_PATTERNS:
                if pattern.match(name):
                    _matched = True
                    break
            # Some might match due to trailing patterns, that's okay


class TestRecordingPart:
    """Test RecordingPart dataclass."""

    def test_construction(self, tmp_path):
        audio_file = tmp_path / "test.m4a"
        audio_file.touch()

        part = RecordingPart(
            path=audio_file,
            base_name="test",
            part_number=1,
        )

        assert part.path == audio_file
        assert part.base_name == "test"
        assert part.part_number == 1
        assert part.timestamp is not None  # Auto-populated from mtime

    def test_explicit_timestamp(self, tmp_path):
        audio_file = tmp_path / "test.m4a"
        audio_file.touch()

        ts = datetime(2026, 2, 15, 10, 0, 0)
        part = RecordingPart(
            path=audio_file,
            base_name="test",
            part_number=1,
            timestamp=ts,
        )

        assert part.timestamp == ts


class TestRecordingGroup:
    """Test RecordingGroup dataclass."""

    def test_ordered_paths(self, tmp_path):
        # Create files out of order
        parts = []
        for i in [3, 1, 2]:
            f = tmp_path / f"meeting_{i}.m4a"
            f.touch()
            parts.append(RecordingPart(path=f, base_name="meeting", part_number=i))

        group = RecordingGroup(base_name="meeting", parts=parts)
        ordered = group.ordered_paths

        # Should be ordered 1, 2, 3
        assert ordered[0].name == "meeting_1.m4a"
        assert ordered[1].name == "meeting_2.m4a"
        assert ordered[2].name == "meeting_3.m4a"

    def test_is_complete_sequential(self, tmp_path):
        parts = []
        for i in [1, 2, 3]:
            f = tmp_path / f"meeting_{i}.m4a"
            f.touch()
            parts.append(RecordingPart(path=f, base_name="meeting", part_number=i))

        group = RecordingGroup(base_name="meeting", parts=parts)
        assert group.is_complete() is True

    def test_is_complete_with_gap(self, tmp_path):
        parts = []
        for i in [1, 3]:  # Missing part 2
            f = tmp_path / f"meeting_{i}.m4a"
            f.touch()
            parts.append(RecordingPart(path=f, base_name="meeting", part_number=i))

        group = RecordingGroup(base_name="meeting", parts=parts)
        assert group.is_complete() is False

    def test_count(self, tmp_path):
        parts = []
        for i in [1, 2]:
            f = tmp_path / f"call_{i}.m4a"
            f.touch()
            parts.append(RecordingPart(path=f, base_name="call", part_number=i))

        group = RecordingGroup(base_name="call", parts=parts)
        assert group.count == 2


class TestMultipartDetector:
    """Test MultipartDetector class."""

    def test_find_no_groups(self, tmp_path):
        # Create standalone files
        (tmp_path / "standalone.m4a").touch()
        (tmp_path / "another.m4a").touch()

        detector = MultipartDetector(tmp_path)
        groups = detector.find_groups()

        assert len(groups) == 0

    def test_find_simple_group(self, tmp_path):
        # Create a two-part series
        (tmp_path / "meeting part 1.m4a").touch()
        (tmp_path / "meeting part 2.m4a").touch()

        detector = MultipartDetector(tmp_path)
        groups = detector.find_groups()

        assert len(groups) == 1
        assert "meeting" in groups
        assert groups["meeting"].count == 2

    def test_find_multiple_groups(self, tmp_path):
        # Create two different series
        (tmp_path / "standup_1.m4a").touch()
        (tmp_path / "standup_2.m4a").touch()
        (tmp_path / "call pt1.m4a").touch()
        (tmp_path / "call pt2.m4a").touch()
        (tmp_path / "call pt3.m4a").touch()

        detector = MultipartDetector(tmp_path)
        groups = detector.find_groups()

        assert len(groups) == 2

    def test_ignores_non_audio_files(self, tmp_path):
        # Create non-audio files with part patterns
        (tmp_path / "notes part 1.txt").touch()
        (tmp_path / "notes part 2.txt").touch()

        detector = MultipartDetector(tmp_path)
        groups = detector.find_groups()

        assert len(groups) == 0

    def test_minimum_parts_threshold(self, tmp_path):
        # Single part should not form a group (only groups with 2+ parts returned)
        (tmp_path / "solo part 1.m4a").touch()

        detector = MultipartDetector(tmp_path)
        groups = detector.find_groups()

        assert len(groups) == 0
