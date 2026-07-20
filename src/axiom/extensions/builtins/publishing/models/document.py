# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Document model for Publisher — structured representation of document content.

This model represents the actual document structure (sections, images, links, etc.)
independent of storage format. It's used by cleanup and validation logic to
operate on semantic content rather than raw text.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Image:
    """An image or diagram in the document."""

    path: str  # Relative path to image (e.g., "media/diagram.png")
    alt_text: str = ""  # Alternative text for accessibility
    title: str = ""  # Title/caption
    width: int | None = None  # Width in pixels (if inline)
    height: int | None = None  # Height in pixels (if inline)
    source: str = ""  # Provider name that extracted this


@dataclass
class Link:
    """A hyperlink in the document."""

    text: str  # Displayed text
    url: str  # Target URL
    title: str = ""  # Link title/tooltip
    is_internal: bool = False  # True if links to another local doc
    is_broken: bool = False  # True if URL is invalid or unreachable
    source: str = ""  # Provider name that identified this issue


@dataclass
class Section:
    """A section or heading in the document."""

    level: int  # Heading level (1-6)
    title: str  # Section title text
    content: str = ""  # Plain text content under this section
    subsections: list[Section] = field(default_factory=list)
    images: list[Image] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    toc_entry: bool = True  # Whether this section appears in TOC


@dataclass
class Document:
    """Structured representation of a document's content.

    Operates at the semantic level: sections, images, links, text blocks.
    Allows cleanup and validation to reason about document structure
    without being tied to a specific format (markdown, docx, latex, etc.).
    """

    title: str = ""
    subtitle: str = ""
    abstract: str = ""
    table_of_contents: list[Section] = field(default_factory=list)

    # Main content
    sections: list[Section] = field(default_factory=list)

    # Document-level metadata
    author: str = ""
    created: str = ""  # ISO 8601
    modified: str = ""  # ISO 8601
    version: str = ""

    # Properties
    metadata: dict[str, Any] = field(default_factory=dict)
    front_matter: dict[str, Any] = field(default_factory=dict)

    def get_all_images(self) -> list[Image]:
        """Recursively collect all images in the document."""
        images: list[Image] = []

        def collect_images(sections: list[Section]) -> None:
            for section in sections:
                images.extend(section.images)
                collect_images(section.subsections)

        collect_images(self.sections)
        return images

    def get_all_links(self) -> list[Link]:
        """Recursively collect all links in the document."""
        links: list[Link] = []

        def collect_links(sections: list[Section]) -> None:
            for section in sections:
                links.extend(section.links)
                collect_links(section.subsections)

        collect_links(self.sections)
        return links

    def get_broken_links(self) -> list[Link]:
        """Return all broken or suspicious links."""
        return [link for link in self.get_all_links() if link.is_broken]

    def get_missing_alt_text(self) -> list[Image]:
        """Return all images without alternative text."""
        return [img for img in self.get_all_images() if not img.alt_text]

    def validate(self) -> dict[str, list[str]]:
        """Validate document integrity and structure.

        Returns:
            Dict with validation categories:
            {
                "errors": [...],
                "warnings": [...],
                "info": [...]
            }
        """
        issues = {"errors": [], "warnings": [], "info": []}

        # Check for missing title
        if not self.title:
            issues["warnings"].append("Document has no title")

        # Check for broken links
        broken = self.get_broken_links()
        if broken:
            issues["errors"].extend([
                f"Broken link: {link.text} → {link.url}"
                for link in broken
            ])

        # Check for missing alt text
        missing_alt = self.get_missing_alt_text()
        if missing_alt:
            issues["warnings"].extend([
                f"Missing alt text for image: {img.path}"
                for img in missing_alt
            ])

        # Check for empty sections
        def check_empty(sections: list[Section], path: str = "") -> None:
            for section in sections:
                full_path = f"{path}/{section.title}" if path else section.title
                if not section.content and not section.subsections:
                    issues["info"].append(f"Empty section: {full_path}")
                check_empty(section.subsections, full_path)

        check_empty(self.sections)

        return issues
