# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Notes extractor for Sense pipeline.

Extracts signals from personal notes (markdown, text files).
Parses structured and unstructured notes for PRD-relevant content.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..models import Extraction, Signal


@dataclass
class ParsedNote:
    """Parsed note file."""
    filepath: Path
    title: str
    content: str
    created_at: datetime
    modified_at: datetime
    frontmatter: dict  # YAML frontmatter if present


class NotesExtractor:
    """Extracts signals from personal notes.

    Supports:
    - Markdown files (.md)
    - Plain text files (.txt)
    - Optional YAML frontmatter parsing

    Usage:
        extractor = NotesExtractor()
        extraction = extractor.extract_all()

        # Or specific file
        extraction = extractor.extract_file(Path("inbox/raw/notes/meeting-notes.md"))
    """

    # Keywords that indicate PRD relevance
    PRD_KEYWORDS = {
        "ops_log": ["ops", "operations", "console", "shift", "system", "compliance", "log"],
        "experiment_manager": ["experiment", "sample", "testing", "roc", "tracking", "lifecycle"],
        "operator_dashboard": ["operator", "dashboard", "alerts", "monitoring", "real-time"],
        "researcher_dashboard": ["researcher", "results", "analysis", "data", "my experiments"],
    }

    # Signal type indicators
    SIGNAL_MARKERS = {
        "requirement": ["need", "must", "should", "require", "want", "feature"],
        "decision": ["decided", "will use", "agreed", "chosen", "go with"],
        "question": ["?", "unclear", "need to determine", "open question", "tbd"],
        "action_item": ["todo", "action", "follow up", "schedule", "[ ]", "- [ ]"],
        "insight": ["learned", "noted", "context", "background", "constraint"],
    }

    # Known stakeholders
    STAKEHOLDERS = {
        "jim": "Jim (TJ)",
        "tj": "Jim (TJ)",
        "nick": "Nick Luciano",
        "luciano": "Nick Luciano",
        "khiloni": "Khiloni Shah",
        "kevin": "Kevin Clarno",
        "clarno": "Kevin Clarno",
        "ben": "Ben Booth",
    }

    def __init__(self, inbox_path: Path | None = None):
        """Initialize extractor.

        Args:
            inbox_path: Path to inbox directory. Defaults to tools/agents/inbox/
        """
        self.inbox_path = inbox_path or Path(__file__).parent.parent / "inbox"
        self.notes_dir = self.inbox_path / "raw" / "notes"

    def extract_all(self) -> Extraction:
        """Extract signals from all notes in inbox.

        Returns:
            Extraction containing signals from all note files
        """
        signals = []
        processed_files = []

        if not self.notes_dir.exists():
            return Extraction(
                extractor="notes",
                source_file=str(self.notes_dir),
                signals=[],
                errors=[f"Notes directory not found: {self.notes_dir}"],
            )

        # Process markdown and text files
        for pattern in ["*.md", "*.txt"]:
            for filepath in self.notes_dir.glob(pattern):
                extraction = self.extract_file(filepath)
                signals.extend(extraction.signals)
                processed_files.append(str(filepath))

        return Extraction(
            extractor="notes",
            source_file=str(self.notes_dir),
            signals=signals,
        )

    def extract_file(self, filepath: Path) -> Extraction:
        """Extract signals from a single note file.

        Args:
            filepath: Path to note file

        Returns:
            Extraction containing signals from the note
        """
        note = self._parse_note(filepath)
        signals = self._extract_signals_from_note(note)

        return Extraction(
            extractor="notes",
            source_file=str(filepath),
            signals=signals,
        )

    def _parse_note(self, filepath: Path) -> ParsedNote:
        """Parse a note file into structured form."""
        content = filepath.read_text(encoding="utf-8")
        stat = filepath.stat()

        # Extract YAML frontmatter if present
        frontmatter = {}
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = self._parse_frontmatter(parts[1])
                content = parts[2].strip()

        # Extract title from first heading or filename
        title = self._extract_title(content, filepath)

        return ParsedNote(
            filepath=filepath,
            title=title,
            content=content,
            created_at=datetime.fromtimestamp(stat.st_ctime),
            modified_at=datetime.fromtimestamp(stat.st_mtime),
            frontmatter=frontmatter,
        )

    def _parse_frontmatter(self, yaml_str: str) -> dict:
        """Parse YAML frontmatter."""
        try:
            import yaml
            return yaml.safe_load(yaml_str) or {}
        except Exception:
            return {}

    def _extract_title(self, content: str, filepath: Path) -> str:
        """Extract title from content or filename."""
        # Look for first heading
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()

        # Fall back to filename
        return filepath.stem.replace("-", " ").replace("_", " ").title()

    def _extract_signals_from_note(self, note: ParsedNote) -> list[Signal]:
        """Extract signals from a parsed note.

        Uses LLM for intelligent extraction, with fallback to rule-based.
        """
        # For MVP, use rule-based extraction
        # TODO: Add LLM extraction path
        return self._rule_based_extraction(note)

    def _rule_based_extraction(self, note: ParsedNote) -> list[Signal]:
        """Rule-based signal extraction (fallback/MVP)."""
        signals = []

        # Split into paragraphs/sections
        sections = self._split_into_sections(note.content)

        for section in sections:
            if len(section.strip()) < 20:  # Skip very short sections
                continue

            # Determine signal type
            signal_type = self._infer_signal_type(section)

            # Determine PRD target
            prd_target = self._infer_prd_target(section)

            # Extract mentioned people
            people = self._extract_people(section)

            # Only create signal if we found something relevant
            if prd_target or signal_type in ["requirement", "decision", "action_item"]:
                signals.append(Signal(
                    source="notes",
                    timestamp=note.modified_at.isoformat(),
                    raw_text=section.strip(),
                    signal_type=signal_type,
                    initiatives=[prd_target] if prd_target else [],
                    people=people,
                    detail=self._summarize_section(section),
                    confidence=0.7,  # Rule-based is medium confidence
                    metadata={
                        "filepath": str(note.filepath),
                        "title": note.title,
                    },
                ))

        return signals

    def _split_into_sections(self, content: str) -> list[str]:
        """Split content into logical sections."""
        # Split on headings, blank lines, or bullet boundaries
        sections = []
        current = []

        for line in content.split("\n"):
            # New section on heading
            if line.startswith("#"):
                if current:
                    sections.append("\n".join(current))
                current = [line]
            # New section on blank line after content
            elif not line.strip() and current:
                sections.append("\n".join(current))
                current = []
            else:
                current.append(line)

        if current:
            sections.append("\n".join(current))

        return sections

    def _infer_signal_type(self, text: str) -> str:
        """Infer signal type from text content."""
        text_lower = text.lower()

        for signal_type, markers in self.SIGNAL_MARKERS.items():
            if any(marker in text_lower for marker in markers):
                return signal_type

        return "insight"  # Default to insight

    def _infer_prd_target(self, text: str) -> str | None:
        """Infer PRD target from text content."""
        text_lower = text.lower()

        for prd, keywords in self.PRD_KEYWORDS.items():
            if any(kw in text_lower for kw in keywords):
                return prd

        return None

    def _extract_people(self, text: str) -> list[str]:
        """Extract known stakeholders from text."""
        people = []
        text_lower = text.lower()

        for key, name in self.STAKEHOLDERS.items():
            if key in text_lower and name not in people:
                people.append(name)

        return people

    def _summarize_section(self, section: str) -> str:
        """Create brief summary of section."""
        # Take first line or first 100 chars
        first_line = section.strip().split("\n")[0]
        if len(first_line) > 100:
            return first_line[:97] + "..."
        return first_line
