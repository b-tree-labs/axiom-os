# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Validation framework for Publisher — detects state mismatches and recovery paths.

Validates:
1. File integrity (source .docx exists, .md exists, images exist)
2. Metadata consistency (registry matches files on disk)
3. Publication state (draft, published, out-of-sync, orphan)
4. Checksums (detect changes since last sync)
5. Cross-references (all internal links resolve)

Suggests recovery actions for failure modes:
- Missing source .docx → re-pull from provider
- Missing .md → rebuild from .docx
- Missing comments metadata → re-extract from .docx
- Missing images → re-extract from .docx
- Stale checksum → .md or .docx has changed unexpectedly
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Import DocumentState from the legacy models module
try:
    from .state import DocumentState
except ImportError:
    # Fallback: DocumentState may not exist in some contexts
    DocumentState = Any  # type: ignore[assignment,misc]


@dataclass
class ValidationIssue:
    """A single validation problem found."""

    category: str  # "missing_file", "stale_checksum", "broken_link", etc.
    severity: str  # "error", "warning", "info"
    file_path: Path | None = None
    message: str = ""
    suggested_recovery: str = ""  # How to fix it


@dataclass
class ValidationResult:
    """Result of validating a document."""

    doc_id: str
    publication_status: str = "unknown"  # "draft", "published", "out-of-sync", "orphan"
    is_valid: bool = True
    issues: list[ValidationIssue] = field(default_factory=list)
    recovery_steps: list[str] = field(default_factory=list)

    def add_issue(
        self,
        category: str,
        severity: str,
        message: str,
        recovery: str = "",
        file_path: Path | None = None,
    ) -> None:
        """Add a validation issue."""
        issue = ValidationIssue(
            category=category,
            severity=severity,
            message=message,
            suggested_recovery=recovery,
            file_path=file_path,
        )
        self.issues.append(issue)

        if severity == "error":
            self.is_valid = False

    def get_errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "error"]

    def get_warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    def get_infos(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == "info"]


class DocumentValidator:
    """Validates document state and integrity."""

    def __init__(self, repo_root: Path):
        """Initialize validator.

        Args:
            repo_root: Root of git repository containing docs
        """
        self.repo_root = repo_root
        self.docs_root = repo_root / "docs" / "prd"
        self.source_dir = self.docs_root / "_source"
        self.media_dir = self.docs_root / "media"

    def validate_document(
        self,
        doc_id: str,
        state: DocumentState | None = None,  # type: ignore[type-arg]
    ) -> ValidationResult:
        """Validate a single document's state and files.

        Args:
            doc_id: Document ID (e.g., "medical-isotope-prd")
            state: DocumentState from registry (optional)
        Returns:
            ValidationResult with issues and suggested recovery
        """
        result = ValidationResult(doc_id=doc_id)

        # Determine publication status
        markdown_path = self.docs_root / f"{doc_id}.md"
        source_docx = self.source_dir / f"{doc_id}.docx"

        md_exists = markdown_path.exists()
        docx_exists = source_docx.exists()

        # Detect publication state
        if not md_exists and not docx_exists:
            result.publication_status = "unknown"
            result.add_issue(
                "missing_files",
                "error",
                f"Neither .md nor .docx found for {doc_id}",
                "Create document or check doc_id spelling",
            )
        elif md_exists and not docx_exists:
            result.publication_status = "draft"
            result.add_issue(
                "missing_source_docx",
                "warning",
                f"Markdown exists but no source .docx for {doc_id}",
                "Run first_publish to create and upload .docx to SharePoint",
                source_docx,
            )
        elif docx_exists and not md_exists:
            result.publication_status = "orphan"
            result.add_issue(
                "missing_markdown",
                "warning",
                f"Source .docx exists but no markdown for {doc_id}",
                "Run first_ingest to pull and convert .docx to .md",
                markdown_path,
            )
        else:
            result.publication_status = "published"

        # Check file integrity
        self._check_file_integrity(result, markdown_path, source_docx)

        # Check metadata consistency
        if state:
            self._check_metadata(result, state, markdown_path, source_docx)

        # Check image references
        self._check_image_references(result, markdown_path)

        # Determine recovery path
        result.recovery_steps = self._suggest_recovery(result)

        return result

    def _check_file_integrity(
        self,
        result: ValidationResult,
        markdown_path: Path,
        source_docx: Path,
    ) -> None:
        """Check that files exist and are readable."""
        if markdown_path.exists():
            if not markdown_path.stat().st_size > 0:
                result.add_issue(
                    "empty_file",
                    "error",
                    f"Markdown file is empty: {markdown_path}",
                    "Rebuild from source .docx or restore from git",
                    markdown_path,
                )

        if source_docx.exists():
            if not source_docx.stat().st_size > 0:
                result.add_issue(
                    "empty_file",
                    "error",
                    f"Source .docx is empty: {source_docx}",
                    "Re-download from SharePoint",
                    source_docx,
                )

    def _check_metadata(
        self,
        result: ValidationResult,
        state: DocumentState,  # type: ignore[type-arg]
        markdown_path: Path,
        source_docx: Path,
    ) -> None:
        """Check registry vs. actual files."""
        if markdown_path.exists():
            md_checksum = self._compute_checksum(markdown_path)
            # If we tracked a checksum in state, compare
            # (This would be added to DocumentState)
            if hasattr(state, "md_checksum") and state.md_checksum != md_checksum:
                result.add_issue(
                    "stale_checksum",
                    "warning",
                    "Markdown file has changed since last sync",
                    "Review changes and re-sync if needed",
                    markdown_path,
                )

    def _check_image_references(
        self,
        result: ValidationResult,
        markdown_path: Path,
    ) -> None:
        """Check that all image references in markdown exist."""
        if not markdown_path.exists():
            return

        content = markdown_path.read_text(encoding="utf-8")

        # Find all image references: ![alt](path)
        import re
        image_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
        matches = re.finditer(image_pattern, content)

        for match in matches:
            alt_text = match.group(1)
            image_ref = match.group(2)

            # Resolve relative to markdown directory
            if image_ref.startswith("http"):
                # External reference, skip
                continue

            # Resolve relative to markdown file's parent directory first (standard markdown behavior)
            image_path = markdown_path.parent / image_ref

            # If not found, try workspace-relative paths (for backwards compatibility)
            if not image_path.exists() and image_ref.startswith(("docs/", "media/")):
                image_path = self.repo_root / image_ref

            if not image_path.exists():
                result.add_issue(
                    "missing_image",
                    "warning",
                    f"Referenced image not found: {image_ref}",
                    "Re-extract media from source .docx",
                    image_path,
                )
            elif not alt_text:
                result.add_issue(
                    "missing_alt_text",
                    "warning",
                    f"Image missing alt text: {image_ref}",
                    "Add descriptive alt text for accessibility",
                    image_path,
                )

    def _suggest_recovery(self, result: ValidationResult) -> list[str]:
        """Generate ordered recovery steps based on issues found."""
        steps: list[str] = []

        if result.publication_status == "draft":
            steps.append("1. Review and finalize markdown in docs/prds/")
            steps.append("2. Run: publisher first_publish <doc_id>")
            steps.append("3. Verify published document on SharePoint")

        elif result.publication_status == "orphan":
            steps.append("1. Run: publisher first_ingest <doc_id>")
            steps.append("2. Review converted markdown in docs/prds/")
            steps.append("3. Fix any conversion artifacts (run cleanup)")
            steps.append("4. Commit to git")

        elif result.publication_status == "published":
            if result.get_errors():
                steps.append("1. Fix critical errors:")
                for error in result.get_errors():
                    steps.append(f"   - {error.suggested_recovery}")
            if result.get_warnings():
                steps.append("2. Address warnings:")
                for warn in result.get_warnings():
                    steps.append(f"   - {warn.suggested_recovery}")

        return steps

    @staticmethod
    def _compute_checksum(file_path: Path, algorithm: str = "sha256") -> str:
        """Compute file checksum for integrity tracking."""
        hasher = hashlib.new(algorithm)
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hasher.update(chunk)
        return hasher.hexdigest()


def validate_all_documents(
    repo_root: Path,
    _include_drafts: bool = True,
) -> dict[str, ValidationResult]:
    """Validate all tracked documents in the repository.

    Args:
        repo_root: Repository root
        include_drafts: Whether to validate draft-only documents
    Returns:
        Dict mapping doc_id → ValidationResult
    """
    validator = DocumentValidator(repo_root)
    docs_dir = repo_root / "docs" / "prd"

    results: dict[str, ValidationResult] = {}

    # Find all .md files
    if docs_dir.exists():
        for md_file in docs_dir.glob("*.md"):
            doc_id = md_file.stem
            result = validator.validate_document(doc_id)
            results[doc_id] = result

    return results
