# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Multi-part Recording Assembly — detect and order segmented voice memos.

Voice Memos on iOS often splits long recordings, or users may name files with
part numbers. Common patterns:
- "meeting part 1.m4a", "meeting part 2.m4a"
- "standup_pt1.m4a", "standup_pt2.m4a"
- "notes (1).m4a", "notes (2).m4a"
- "call-1of3.m4a", "call-2of3.m4a", "call-3of3.m4a"
- Files with same base name and sequential timestamps

This module:
1. Detects multi-part series in the inbox
2. Orders them correctly (by part number or timestamp)
3. Returns them as a group for concatenated transcription

Usage:
    from axiom.extensions.builtins.signals.multipart import MultipartDetector

    detector = MultipartDetector(inbox_path)
    groups = detector.find_groups()
    # groups = {"meeting": [Path("meeting part 1.m4a"), Path("meeting part 2.m4a")]}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Patterns to detect part numbers in filenames
PART_PATTERNS = [
    # "part 1", "part 2", "Part 1", etc.
    re.compile(r"(?P<base>.+?)\s*part\s*(?P<num>\d+)", re.IGNORECASE),
    # "pt1", "pt2", "PT1"
    re.compile(r"(?P<base>.+?)\s*pt\s*(?P<num>\d+)", re.IGNORECASE),
    # "(1)", "(2)" - common iOS duplicate naming
    re.compile(r"(?P<base>.+?)\s*\((?P<num>\d+)\)"),
    # "_1", "_2" suffix
    re.compile(r"(?P<base>.+?)_(?P<num>\d+)$"),
    # "-1of3", "-2of3"
    re.compile(r"(?P<base>.+?)-(?P<num>\d+)of\d+", re.IGNORECASE),
    # "1of3", "2of3" (no leading dash)
    re.compile(r"(?P<base>.+?)\s*(?P<num>\d+)of\d+", re.IGNORECASE),
    # " 1", " 2" trailing number with space
    re.compile(r"(?P<base>.+?)\s+(?P<num>\d+)$"),
]

# Audio extensions we handle
AUDIO_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".webm", ".aac", ".flac"}


@dataclass
class RecordingPart:
    """A single part of a potential multi-part recording."""

    path: Path
    base_name: str  # The common name without part number
    part_number: int
    timestamp: datetime | None = None
    duration_seconds: float | None = None

    def __post_init__(self):
        if self.timestamp is None:
            try:
                self.timestamp = datetime.fromtimestamp(self.path.stat().st_mtime)
            except OSError:
                pass


@dataclass
class RecordingGroup:
    """A group of related recording parts that should be processed together."""

    base_name: str
    parts: list[RecordingPart] = field(default_factory=list)

    @property
    def ordered_paths(self) -> list[Path]:
        """Return paths ordered by part number, then by timestamp."""
        # Sort by part number first, then timestamp as tiebreaker
        sorted_parts = sorted(
            self.parts,
            key=lambda p: (p.part_number, p.timestamp or datetime.min)
        )
        return [p.path for p in sorted_parts]

    @property
    def count(self) -> int:
        return len(self.parts)

    def is_complete(self) -> bool:
        """Check if parts are sequential (1, 2, 3, ...)."""
        if not self.parts:
            return False
        numbers = sorted(p.part_number for p in self.parts)
        expected = list(range(1, len(numbers) + 1))
        return numbers == expected

    def missing_parts(self) -> list[int]:
        """Return which part numbers appear to be missing."""
        if not self.parts:
            return []
        numbers = set(p.part_number for p in self.parts)
        max_num = max(numbers)
        return [n for n in range(1, max_num + 1) if n not in numbers]


class MultipartDetector:
    """Detects and groups multi-part recordings in the inbox."""

    def __init__(self, inbox_path: Path):
        self.inbox_path = inbox_path

    def _extract_part_info(self, path: Path) -> tuple[str, int] | None:
        """Extract base name and part number from a filename.

        Returns (base_name, part_number) or None if not a multi-part file.
        """
        stem = path.stem

        for pattern in PART_PATTERNS:
            match = pattern.match(stem)
            if match:
                base = match.group("base").strip()
                num = int(match.group("num"))
                if base and num > 0:
                    return (base.lower(), num)

        return None

    def find_groups(self, subdir: str | None = None) -> dict[str, RecordingGroup]:
        """Find all multi-part recording groups in the inbox.

        Args:
            subdir: Optional subdirectory to scan (e.g., "voice")

        Returns:
            Dict mapping base_name to RecordingGroup
        """
        search_path = self.inbox_path / subdir if subdir else self.inbox_path
        if not search_path.exists():
            return {}

        # Collect all audio files
        audio_files: list[Path] = []
        for ext in AUDIO_EXTENSIONS:
            audio_files.extend(search_path.glob(f"*{ext}"))
            audio_files.extend(search_path.glob(f"*{ext.upper()}"))

        # Group by base name
        groups: dict[str, RecordingGroup] = {}
        standalone: list[Path] = []

        for path in audio_files:
            info = self._extract_part_info(path)
            if info:
                base_name, part_num = info
                if base_name not in groups:
                    groups[base_name] = RecordingGroup(base_name=base_name)
                groups[base_name].parts.append(RecordingPart(
                    path=path,
                    base_name=base_name,
                    part_number=part_num,
                ))
            else:
                standalone.append(path)

        # Filter to only groups with multiple parts
        multi_part_groups = {
            name: group for name, group in groups.items()
            if group.count > 1
        }

        return multi_part_groups

    def find_related_by_timestamp(
        self,
        files: list[Path],
        max_gap_minutes: int = 5,
    ) -> list[list[Path]]:
        """Group files that were created close together in time.

        This catches cases where files don't have part numbers but are
        clearly from the same session (created within minutes of each other).
        """
        if not files:
            return []

        # Sort by modification time
        with_times = []
        for f in files:
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                with_times.append((f, mtime))
            except OSError:
                continue

        if not with_times:
            return []

        with_times.sort(key=lambda x: x[1])

        # Group by time proximity
        groups: list[list[Path]] = []
        current_group: list[Path] = [with_times[0][0]]
        current_time = with_times[0][1]

        for path, mtime in with_times[1:]:
            gap = (mtime - current_time).total_seconds() / 60
            if gap <= max_gap_minutes:
                current_group.append(path)
            else:
                if len(current_group) > 1:
                    groups.append(current_group)
                current_group = [path]
            current_time = mtime

        if len(current_group) > 1:
            groups.append(current_group)

        return groups

    def get_processing_order(self, paths: list[Path]) -> list[Path]:
        """Return paths in the order they should be processed/concatenated.

        Handles both explicitly numbered parts and timestamp-based ordering.
        """
        # Try to extract part numbers
        parts_with_num: list[tuple[Path, int]] = []
        parts_without_num: list[tuple[Path, datetime]] = []

        for path in paths:
            info = self._extract_part_info(path)
            if info:
                _, num = info
                parts_with_num.append((path, num))
            else:
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime)
                    parts_without_num.append((path, mtime))
                except OSError:
                    parts_without_num.append((path, datetime.min))

        # If we have part numbers, use those
        if parts_with_num:
            parts_with_num.sort(key=lambda x: x[1])
            return [p[0] for p in parts_with_num]

        # Otherwise, order by timestamp
        parts_without_num.sort(key=lambda x: x[1])
        return [p[0] for p in parts_without_num]

    def status(self) -> dict:
        """Get detector status."""
        groups = self.find_groups("voice")

        total_files = sum(g.count for g in groups.values())
        complete = sum(1 for g in groups.values() if g.is_complete())

        return {
            "groups_found": len(groups),
            "total_files_in_groups": total_files,
            "complete_groups": complete,
            "incomplete_groups": len(groups) - complete,
            "group_names": list(groups.keys()),
        }


def concatenate_audio_files(paths: list[Path], output_path: Path) -> bool:
    """Concatenate multiple audio files into one.

    Uses ffmpeg if available, otherwise returns False.
    """
    if not paths:
        return False

    if len(paths) == 1:
        # Just copy the single file
        import shutil
        shutil.copy(paths[0], output_path)
        return True

    import subprocess

    try:
        # Create a temporary file list for ffmpeg
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
            list_file = f.name
            for path in paths:
                # Escape single quotes in path
                escaped = str(path).replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")

        # Run ffmpeg concat
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-f", "concat", "-safe", "0",
                "-i", list_file,
                "-c", "copy",
                str(output_path)
            ],
            capture_output=True,
            timeout=300,  # 5 minute timeout
        )

        # Clean up
        Path(list_file).unlink(missing_ok=True)

        return result.returncode == 0

    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def get_multipart_detector(inbox_path: Path | None = None) -> MultipartDetector:
    """Get a multipart detector for the inbox."""
    if inbox_path is None:
        from axiom import REPO_ROOT as _REPO_ROOT
        inbox_path = _REPO_ROOT / "runtime" / "inbox" / "raw"
    return MultipartDetector(inbox_path)
