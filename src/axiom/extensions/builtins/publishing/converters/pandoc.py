# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pandoc-based converter for Publisher — handles docx↔md, latex↔md, html↔md, etc."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .base import BaseConverter, ConversionOptions, ConversionResult


class PandocConverter(BaseConverter):
    """Converter using pandoc for multi-format document conversion.

    Supported conversions:
    - docx → md (with --extract-media)
    - md → docx
    - md ↔ latex
    - md ↔ html
    - And many others via pandoc

    Requirements:
        pandoc (https://pandoc.org/) - Install via: brew install pandoc
    """

    def __init__(self):
        self.pandoc_path = shutil.which("pandoc")
        if not self.pandoc_path:
            raise RuntimeError(
                "Pandoc not found. Install via: brew install pandoc (macOS) "
                "or pandoc.org for other platforms"
            )

    def get_format_name(self) -> str:
        return "Pandoc"

    def supports_conversion(self, source_format: str, target_format: str) -> bool:
        """Check if pandoc supports this conversion.

        Common supported formats: docx, md, latex, tex, html, pdf, odt, rst, ...
        """
        if source_format == target_format:
            return False
        # Pandoc supports most combinations, so keep it simple
        supported = {
            "docx", "md", "markdown", "gfm", "latex", "tex", "html",
            "pdf", "odt", "rst", "asciidoc", "json"
        }
        return source_format in supported and target_format in supported

    def convert(
        self,
        source_path: Path,
        output_path: Path,
        source_format: str,
        target_format: str,
        options: ConversionOptions | None = None,
    ) -> ConversionResult:
        """Convert document via pandoc.

        Args:
            source_path: Path to source file
            output_path: Path for output file
            source_format: Source format (docx, md, latex, html, etc.)
            target_format: Target format (md, docx, pdf, etc.)
            options: Conversion options
        Returns:
            ConversionResult with output path, extracted media, comments, warnings
        """
        options = options or ConversionOptions()
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Build pandoc command
        cmd = [
            self.pandoc_path,
            # Disable `$...$` math so currency ("$50–60K") survives intact.
            "-f",
            "markdown-tex_math_dollars"
            if source_format in ("gfm", "md", "markdown")
            else source_format,
            "-t", target_format if target_format != "gfm" else "gfm",
            "-o", str(output_path),
            str(source_path),
        ]

        # Extract media for docx → md conversions
        media_extracted: list[Path] = []
        if source_format == "docx" and options.extract_media:
            media_dir = options.media_dir or output_path.parent / f"{output_path.stem}_media"
            media_dir.mkdir(parents=True, exist_ok=True)
            cmd.extend(["--extract-media", str(media_dir)])
            # Media extraction happens during conversion; collect results after

        # Run pandoc
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"Pandoc conversion failed: {e.stderr or e.stdout}"
            ) from e

        # Collect extracted media
        if options.extract_media and source_format == "docx":
            media_dir = options.media_dir or output_path.parent / f"{output_path.stem}_media"
            if media_dir.exists():
                media_extracted = list(media_dir.rglob("*"))
                media_extracted = [p for p in media_extracted if p.is_file()]

        # Extract comments if source is docx
        comments: list[dict] = []
        if source_format == "docx" and options.preserve_comments:
            comments = self.extract_comments(source_path)

        # Extract metadata
        metadata = self.extract_metadata(source_path)

        warnings = []
        if result.stderr:
            warnings = result.stderr.strip().split("\n")

        return ConversionResult(
            output_path=output_path,
            source_format=source_format,
            target_format=target_format,
            size_bytes=output_path.stat().st_size if output_path.exists() else 0,
            media_extracted=media_extracted,
            comments=comments,
            warnings=warnings,
            metadata=metadata,
        )

    def extract_media(self, source_path: Path, media_dir: Path) -> list[Path]:
        """Extract images from docx using pandoc.

        Args:
            source_path: Path to .docx file
            media_dir: Directory to extract media into
        Returns:
            List of extracted media file paths
        """
        media_dir.mkdir(parents=True, exist_ok=True)

        # Run pandoc with media extraction
        cmd = [
            self.pandoc_path,
            "-f", "docx",
            "-t", "markdown",
            "-o", "/dev/null",  # Discard markdown output
            "--extract-media", str(media_dir),
            str(source_path),
        ]

        try:
            subprocess.run(cmd, capture_output=True, check=True)
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Media extraction failed: {e.stderr}") from e

        # Collect extracted files
        extracted = list(media_dir.rglob("*"))
        return [p for p in extracted if p.is_file()]

    def extract_comments(self, source_path: Path) -> list[dict]:
        """Extract comments from Word document via XML parsing.

        Word stores comments in word/comments.xml within the .docx zip.
        Args:
            source_path: Path to .docx file
        Returns:
            List of comment dicts
        """
        if not source_path.exists() or source_path.suffix.lower() != ".docx":
            return []

        comments: list[dict] = []
        try:
            with zipfile.ZipFile(source_path, "r") as docx:
                # Check if comments.xml exists
                if "word/comments.xml" not in docx.namelist():
                    return []

                # Parse comments.xml
                xml_content = docx.read("word/comments.xml")
                root = ET.fromstring(xml_content)

                # Word comment namespace
                NS = {
                    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
                    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
                    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
                }

                # Extract each comment element
                for comment_elem in root.findall(".//w:comment", NS):
                    comment_id = comment_elem.get(f"{{{NS['w']}}}id", "")
                    author = comment_elem.get(f"{{{NS['w']}}}author", "Unknown")
                    date = comment_elem.get(f"{{{NS['w']}}}date", "")
                    initials = comment_elem.get(f"{{{NS['w']}}}initials", "")

                    # Extract comment text from all runs
                    text_parts = []
                    for t in comment_elem.findall(".//w:t", NS):
                        if t.text:
                            text_parts.append(t.text)

                    text = "".join(text_parts)

                    comments.append({
                        "comment_id": comment_id,
                        "author": author,
                        "initials": initials,
                        "timestamp": date,
                        "text": text,
                        "resolved": False,
                        "source": "word_comments",
                    })
        except (zipfile.BadZipFile, ET.ParseError):
            # Silently return empty list if parsing fails
            pass

        return comments

    def extract_metadata(self, source_path: Path) -> dict[str, Any]:
        """Extract document metadata (title, author, created, modified, etc.).

        For .docx files, reads from docProps/core.xml.
        For markdown, returns empty dict (no standard metadata).

        Args:
            source_path: Path to source file
        Returns:
            Dict with metadata
        """
        if not source_path.exists():
            return {}

        metadata: dict[str, Any] = {}

        if source_path.suffix.lower() == ".docx":
            try:
                with zipfile.ZipFile(source_path, "r") as docx:
                    if "docProps/core.xml" in docx.namelist():
                        xml_content = docx.read("docProps/core.xml")
                        root = ET.fromstring(xml_content)

                        # Standard Dublin Core namespace
                        NS = {
                            "dc": "http://purl.org/dc/elements/1.1/",
                            "dcterms": "http://purl.org/dc/terms/",
                            "cp": "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
                        }

                        # Extract common properties
                        for key, xpath in {
                            "title": "./dc:title",
                            "creator": "./dc:creator",
                            "subject": "./dc:subject",
                            "description": "./dc:description",
                            "created": "./dcterms:created",
                            "modified": "./dcterms:modified",
                        }.items():
                            elem = root.find(xpath, NS)
                            if elem is not None and elem.text:
                                metadata[key] = elem.text
            except (zipfile.BadZipFile, ET.ParseError):
                pass

        return metadata
