# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Provider ABCs for Publisher — all five extension point contracts.

These abstract base classes define the contracts that all Publisher providers
must implement. The core engine works exclusively through these ABCs,
never importing concrete implementations.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Re-exported from the one true home (ADR-112): publishing no longer
# defines its own storage twin.
from axiom.infra.storage import (  # noqa: F401
    StorageEntry,
    StorageProvider,
    UploadResult,
)

# ─── Shared data models used by providers ───


@dataclass
class GenerationOptions:
    """Options passed to GenerationProvider.generate()."""

    toc: bool = True
    toc_depth: int = 3
    watermark: str | None = None  # Draft watermark text
    reference_doc: str | None = None  # Template path
    mermaid_renderer: str = "mermaid.ink"  # or "mermaid-cli"
    metadata: dict[str, Any] = field(default_factory=dict)
    footer_metadata: dict[str, Any] = field(default_factory=dict)  # For footer (source URL, version, date)


@dataclass
class GenerationResult:
    """Result from GenerationProvider.generate()."""

    output_path: Path
    format: str  # e.g., "docx", "pdf", "html"
    size_bytes: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    """Result from EmbeddingProvider.search()."""

    doc_id: str
    score: float
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ─── Provider ABCs ───


class GenerationProvider(ABC):
    """Converts .md source files into publishable artifact format."""

    @abstractmethod
    def generate(
        self, source_path: Path, output_path: Path, options: GenerationOptions
    ) -> GenerationResult:
        """Generate artifact from markdown source.

        Args:
            source_path: Path to .md file
            output_path: Path for generated artifact
            options: Generation options (TOC, watermark, metadata, etc.)
        Returns:
            GenerationResult with output_path, format, size, warnings
        """
        ...

    @abstractmethod
    def rewrite_links(self, artifact_path: Path, link_map: dict[str, str]) -> None:
        """Rewrite internal document links in the generated artifact.

        Args:
            artifact_path: Path to generated artifact
            link_map: Mapping of relative .md paths to published URLs
        """
        ...

    @abstractmethod
    def get_output_extension(self) -> str:
        """Return the file extension this provider produces
        (e.g., '.docx', '.pdf', '.html')."""
        ...

    @abstractmethod
    def supports_watermark(self) -> bool:
        """Whether this format supports draft watermarks."""
        ...


class FeedbackProvider(ABC):
    """Extracts reviewer feedback from published artifacts or external systems."""

    @abstractmethod
    def fetch_comments(self, artifact_ref: str) -> list:
        """Fetch all comments/feedback for an artifact.

        Args:
            artifact_ref: Storage ID, URL, or issue ID depending on provider
        Returns:
            List of Comment objects
        """
        ...

    @abstractmethod
    def supports_inline_comments(self) -> bool:
        """Whether this provider supports comments anchored
        to specific content ranges."""
        ...

    @abstractmethod
    def mark_resolved(self, artifact_ref: str, comment_id: str) -> bool:
        """Mark a comment as resolved (if supported)."""
        ...


class NotificationProvider(ABC):
    """Sends notifications to stakeholders."""

    @abstractmethod
    def send(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        urgency: str = "normal",
    ) -> bool:
        """Send a notification.

        Args:
            recipients: List of identifiers (email, usernames, channels)
            subject: Notification subject/title
            body: Notification body (markdown supported where applicable)
            urgency: "low", "normal", "high"
        """
        ...


class EmbeddingProvider(ABC):
    """Indexes document content for retrieval-augmented generation."""

    @abstractmethod
    def index_document(self, doc_id: str, content: str, metadata: dict) -> bool:
        """Index a document's content for retrieval."""
        ...

    @abstractmethod
    def search(self, query: str, k: int = 10) -> list[SearchResult]:
        """Search indexed documents."""
        ...

    @abstractmethod
    def remove_document(self, doc_id: str) -> bool:
        """Remove a document from the index."""
        ...
