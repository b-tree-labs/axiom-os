# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DocxFeedbackProvider — extract comments from .docx files.

Parses word/comments.xml inside the .docx ZIP archive to extract
reviewer comments. Pure Python, no external dependencies beyond stdlib.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from ...factory import PublisherFactory
from ...state import Comment
from ..base import FeedbackProvider

# Word XML namespaces
NAMESPACES = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "w15": "http://schemas.microsoft.com/office/word/2012/wordml",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}


class DocxFeedbackProvider(FeedbackProvider):
    """Extract reviewer comments from .docx files by parsing word/comments.xml."""

    def __init__(self, config: dict[str, Any] | None = None):
        pass

    def fetch_comments(self, artifact_ref: str) -> list[Comment]:
        """Fetch comments from a .docx file.

        Args:
            artifact_ref: Path to the .docx file on local filesystem.
        """
        docx_path = Path(artifact_ref)
        if not docx_path.exists() or docx_path.suffix != ".docx":
            return []

        return self._parse_comments(docx_path)

    def supports_inline_comments(self) -> bool:
        return True

    def mark_resolved(self, artifact_ref: str, comment_id: str) -> bool:
        # Word comments don't have a "resolved" state in the XML spec
        # This would require modifying the docx, which is Phase 2+
        return False

    def _parse_comments(self, docx_path: Path) -> list[Comment]:
        """Parse word/comments.xml from a .docx ZIP archive."""
        comments = []

        try:
            with zipfile.ZipFile(str(docx_path), "r") as zf:
                # Check if comments.xml exists
                if "word/comments.xml" not in zf.namelist():
                    return []

                comments_xml = zf.read("word/comments.xml")

                # Also try to read commentsExtended for resolved status
                extended_xml = None
                if "word/commentsExtended.xml" in zf.namelist():
                    extended_xml = zf.read("word/commentsExtended.xml")

        except (zipfile.BadZipFile, KeyError):
            return []

        # Parse comments XML
        try:
            root = ET.fromstring(comments_xml)
        except ET.ParseError:
            return []

        # Parse extended (for done/resolved status)
        resolved_ids: set[str] = set()
        if extended_xml:
            try:
                ext_root = ET.fromstring(extended_xml)
                for ext_comment in ext_root.findall(".//w15:commentEx", NAMESPACES):
                    para_id = ext_comment.get(f"{{{NAMESPACES['w15']}}}paraId", "")
                    done = ext_comment.get(f"{{{NAMESPACES['w15']}}}done", "0")
                    if done == "1" and para_id:
                        resolved_ids.add(para_id)
            except ET.ParseError:
                pass

        # Extract each comment
        for comment_elem in root.findall(".//w:comment", NAMESPACES):
            comment_id = comment_elem.get(f"{{{NAMESPACES['w']}}}id", "")
            author = comment_elem.get(f"{{{NAMESPACES['w']}}}author", "")
            date = comment_elem.get(f"{{{NAMESPACES['w']}}}date", "")

            # Extract text from all paragraphs in the comment
            text_parts = []
            for para in comment_elem.findall(".//w:p", NAMESPACES):
                para_text = ""
                for run in para.findall(".//w:r", NAMESPACES):
                    for t in run.findall("w:t", NAMESPACES):
                        if t.text:
                            para_text += t.text
                if para_text:
                    text_parts.append(para_text)

            text = "\n".join(text_parts)

            if text.strip():
                comments.append(Comment(
                    comment_id=comment_id,
                    author=author,
                    timestamp=date,
                    text=text.strip(),
                    context=None,  # Would need to cross-reference commentRangeStart in document.xml
                    resolved=comment_id in resolved_ids,
                    source="docx-comments",
                ))

        return comments


# Self-register with factory
PublisherFactory.register("feedback", "docx-comments", DocxFeedbackProvider)
