# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Base converter ABC for Publisher — provider-agnostic format conversion."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ConversionOptions:
    """Options passed to Converter.convert()."""

    extract_media: bool = True  # Extract images to media/ subdirectory
    preserve_comments: bool = True  # Extract comments separately
    cleanup: bool = False  # Apply pandoc-specific cleanup (broken links, etc.)
    media_dir: Path | None = None  # Where to store extracted media
    toc_depth: int = 3  # Depth of generated TOCs
    reference_doc: str | None = None  # Template/reference doc for formatting
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConversionResult:
    """Result from Converter.convert()."""

    output_path: Path
    source_format: str  # e.g., "docx", "md", "latex"
    target_format: str  # e.g., "md", "docx", "pdf"
    size_bytes: int = 0
    media_extracted: list[Path] = field(default_factory=list)  # Images, diagrams, etc.
    comments: list[dict] = field(default_factory=list)  # Extracted comments/feedback
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class BaseConverter(ABC):
    """Abstract base for all document format converters.

    Converters are responsible for:
    1. Converting between formats (docx → md, md → docx, etc.)
    2. Extracting embedded media (images, diagrams)
    3. Extracting metadata (comments, properties, tracked changes)
    4. Applying format-specific transformations

    All converters are provider-agnostic and work with local files,
    allowing StorageProvider to handle remote access/upload.
    """

    @abstractmethod
    def supports_conversion(self, source_format: str, target_format: str) -> bool:
        """Check if this converter supports a format pair.

        Args:
            source_format: e.g., "docx", "md", "latex", "html"
            target_format: e.g., "md", "docx", "pdf"
        Returns:
            True if this converter handles the conversion
        """
        ...

    @abstractmethod
    def convert(
        self,
        source_path: Path,
        output_path: Path,
        source_format: str,
        target_format: str,
        options: ConversionOptions | None = None,
    ) -> ConversionResult:
        """Convert a document from one format to another.

        Args:
            source_path: Path to source file
            output_path: Path for output file
            source_format: Source format identifier
            target_format: Target format identifier
            options: Conversion options (media extraction, cleanup, etc.)
        Returns:
            ConversionResult with output path, extracted media, comments, warnings
        """
        ...

    @abstractmethod
    def extract_media(self, source_path: Path, media_dir: Path) -> list[Path]:
        """Extract images, diagrams, and other media from document.

        Args:
            source_path: Path to source file
            media_dir: Directory to extract media into
        Returns:
            List of paths to extracted media files
        """
        ...

    @abstractmethod
    def extract_comments(self, source_path: Path) -> list[dict]:
        """Extract reviewer comments/feedback from document.

        Args:
            source_path: Path to source file
        Returns:
            List of comment dicts with author, timestamp, text, resolved status
        """
        ...

    @abstractmethod
    def extract_metadata(self, source_path: Path) -> dict[str, Any]:
        """Extract document metadata (title, author, created, modified, etc.).

        Args:
            source_path: Path to source file
        Returns:
            Dict with metadata properties
        """
        ...

    @abstractmethod
    def get_format_name(self) -> str:
        """Return a human-readable name for this converter.

        e.g., "Pandoc", "python-docx", "LaTeX"
        """
        ...

    def get_supported_formats(self) -> dict[str, list[str]]:
        """Return mapping of source → target formats this converter supports.

        e.g., {"docx": ["md", "pdf"], "md": ["docx", "html"], ...}
        Returns:
            Dict with source format keys and list of target formats
        """
        # Subclasses may override for efficiency, default implementation is OK
        formats: dict[str, list[str]] = {}
        for src in ["docx", "md", "latex", "html", "pdf", "odt", "tex"]:
            targets = []
            for tgt in ["docx", "md", "latex", "html", "pdf", "odt", "tex"]:
                if src != tgt and self.supports_conversion(src, tgt):
                    targets.append(tgt)
            if targets:
                formats[src] = targets
        return formats
