# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cleanup framework for pandoc conversion artifacts.

This module provides automated fixes for common conversion bugs that occur when
converting from .docx → .md via pandoc. Fixes are provider-agnostic and work
on markdown files directly, supported by semantic understanding via the Document model.

Fixes applied:
1. Broken SharePoint URLs → text references
2. Missing image alt text → inferred from context
3. Blockquote mess (> >) → clean lists or blockquotes
4. Inline pixel styles → removed
5. Diagram rendering failures → replaced with mermaid or note
"""

from __future__ import annotations

import re
from pathlib import Path


class MarkdownCleanup:
    """Cleans up markdown files with pandoc conversion artifacts.

    Works on markdown text directly, identifying and fixing:
    - Broken URLs (particularly SharePoint links that fail conversion)
    - Missing image alt text
    - Malformed blockquotes and nested quotes
    - Inline pixel styles from Word
    - Failed diagram rendering
    """

    def __init__(self, strict: bool = False):
        """Initialize cleanup engine.

        Args:
            strict: If True, fail on first error; else collect warnings
        """
        self.strict = strict
        self.fixes_applied: dict[str, int] = {}
        self.warnings: list[str] = []

    def clean_file(self, markdown_path: Path) -> str:
        """Clean a markdown file in-place and return cleaned content.

        Args:
            markdown_path: Path to .md file to clean
        Returns:
            Cleaned markdown content
        """
        if not markdown_path.exists():
            raise FileNotFoundError(f"Markdown file not found: {markdown_path}")

        content = markdown_path.read_text(encoding="utf-8")
        cleaned = self.clean_content(content)

        # Only write if changes were made
        if cleaned != content:
            markdown_path.write_text(cleaned, encoding="utf-8")

        return cleaned

    def clean_content(self, content: str) -> str:
        """Apply all cleanup fixes to markdown content.

        Args:
            content: Raw markdown content
        Returns:
            Cleaned markdown content
        """
        # Apply fixes in order
        content = self._fix_html_images(content)
        content = self._fix_word_toc(content)
        content = self._fix_broken_urls(content)
        content = self._fix_blockquotes(content)
        content = self._fix_inline_styles(content)
        content = self._fix_diagram_failures(content)
        content = self._add_image_alt_text(content)

        return content

    def _fix_html_images(self, content: str) -> str:
        """Convert HTML img tags to markdown image syntax.

        Pandoc sometimes outputs raw HTML <img> tags instead of markdown ![alt](path).
        This is especially common for images with sizing attributes.

        Pattern: <img src="path/to/image.jpg" ... />
                 OR spans multiple lines with styling

        Replace with: ![Alt text](path/to/image.jpg)
        """
        # Match HTML img tags - handle single and multi-line cases
        # This pattern matches: <img src="..." ... />
        img_pattern = r'<img\s+src="([^"]+)"[^>]*?(?:alt="([^"]*)")?[^>]*?/?>'

        def replace_img(match):
            src = match.group(1)
            alt_text = match.group(2) if match.group(2) else ""

            # If no alt text provided, infer from filename
            if not alt_text:
                filename = Path(src).stem
                # Convert snake_case or kebab-case to Title Case
                alt_text = filename.replace('_', ' ').replace('-', ' ').title()

            return f"![{alt_text}]({src})"

        new_content = re.sub(img_pattern, replace_img, content, flags=re.DOTALL)

        if new_content != content:
            # Count matches (may span multiple lines)
            match_count = len(re.findall(img_pattern, content, flags=re.DOTALL))
            self.fixes_applied["html_images"] = match_count

        return new_content

    def _fix_word_toc(self, content: str) -> str:
        """Clean up Word-generated Table of Contents for readability.

        Word TOCs have complex nested brackets and page numbers. This reformats them
        as clean markdown bullet lists while preserving anchor links.

        Before: [Product Requirements Document [2](#_Toc263527449)](#_Toc263527449)
        After:  - [Product Requirements Document](#_Toc263527449)

        The anchors allow markdown navigation to work.
        """
        lines = content.split('\n')

        # Find TOC section
        toc_start = -1
        toc_end = -1
        for i, line in enumerate(lines):
            if '**Table of Contents**' in line:
                toc_start = i + 1
            elif toc_start >= 0 and line.strip().startswith('##'):
                toc_end = i
                break

        if toc_start < 0 or toc_end < 0:
            return content

        # Process TOC lines: join multi-line entries and clean them up
        toc_lines = []
        i = toc_start

        while i < toc_end:
            line = lines[i]

            # Skip empty lines
            if not line.strip():
                i += 1
                continue

            # Skip lines that are already properly formatted markdown TOC entries
            # Markdown format: - [text](#anchor)
            if line.strip().startswith('- [') and line.rstrip().endswith(')'):
                toc_lines.append(line)
                i += 1
                continue

            # Accumulate multi-line TOC entries (start with [ but don't end with ))
            # This handles Word-generated messy TOCs with nested brackets
            entry = line
            while i < toc_end - 1 and not entry.rstrip().endswith('))'):
                i += 1
                entry += ' ' + lines[i].strip()

            # Clean this entry
            cleaned = self._clean_single_toc_entry(entry)
            if cleaned:
                toc_lines.append(cleaned)

            i += 1

        # Rebuild content
        if toc_lines:
            new_lines = lines[:toc_start] + toc_lines + [''] + lines[toc_end:]
            new_content = '\n'.join(new_lines)

            if new_content != content:
                self.fixes_applied["word_toc"] = len(toc_lines)

            return new_content

        return content

    def _clean_single_toc_entry(self, entry: str) -> str:
        """Clean one TOC entry.

        Input:  [Text [page](#anchor)](anchor)
        Output: - [Text](#anchor)
        """
        entry = entry.strip()
        if not entry or not entry.startswith('['):
            return entry

        # Extract text and final anchor using regex
        # Pattern: ](final_anchor)) at the very end
        # Everything starting with [ and ending with )]

        # Find the last ](#...) pattern - that's the final anchor
        match = re.search(r'\]\(([#_\w-]+)\)\s*$', entry)
        if not match:
            return entry

        final_anchor = match.group(1)

        # Get text: from opening [ to before [page] section
        # Text is between [ and either [ (for page) or ] (if no page)
        text_match = re.match(r'\[([^\[\]]+)(?:\s*\[\d+\])?', entry)
        if not text_match:
            return entry

        text = text_match.group(1).strip()

        if text and final_anchor:
            return f"- [{text}]({final_anchor})"

        return entry

    def _fix_broken_urls(self, content: str) -> str:
        """Fix broken SharePoint and 365 URLs.

        SharePoint URLs often fail conversion; replace with simple text reference.
        Pattern: https://.*sharepoint.com/.*
        """
        # Pattern for SharePoint URLs (can be very long)
        sharepoint_pattern = r'\[([^\]]+)\]\(https://[a-zA-Z0-9\-\.]*sharepoint\.com/[^\)]+\)'

        def replace_url(match: re.Match[str]) -> str:
            link_text = match.group(1)
            # Convert to plain text reference
            return f"{link_text} (Published in SharePoint)"

        new_content = re.sub(sharepoint_pattern, replace_url, content)

        if new_content != content:
            self.fixes_applied["broken_urls"] = len(re.findall(sharepoint_pattern, content))

        return new_content

    def _fix_blockquotes(self, content: str) -> str:
        """Fix nested blockquotes and malformed quote blocks.

        Pandoc sometimes produces:
            > > nested quotes
            > > which should be a list

        Replace with:
            - Clean bullet list
            OR
            > Single blockquote
        """
        # Pattern: multiple > at start of line (> > > or > >)
        nested_quote_pattern = r'^(>\s+){2,}(.+)$'

        lines = content.split('\n')
        fixed_lines = []
        in_nested_block = False
        buffer: list[str] = []

        for line in lines:
            if re.match(nested_quote_pattern, line):
                if not in_nested_block:
                    in_nested_block = True
                # Extract content (everything after > >)
                text = re.sub(r'^(>\s+)+', '', line).strip()
                buffer.append(text)
            else:
                if in_nested_block and buffer:
                    # Convert buffered lines to bullet list
                    for item in buffer:
                        fixed_lines.append(f"- {item}")
                    buffer = []
                    in_nested_block = False
                fixed_lines.append(line)

        # Flush buffer if needed
        if in_nested_block and buffer:
            for item in buffer:
                fixed_lines.append(f"- {item}")

        new_content = '\n'.join(fixed_lines)

        if new_content != content:
            self.fixes_applied["blockquotes"] = len(buffer) if buffer else 0

        return new_content

    def _fix_inline_styles(self, content: str) -> str:
        """Remove inline pixel styles from HTML/Word conversion artifacts.

        Pattern: style="width:6.5in;height:4in;..."
        """
        # Match style attributes in markdown (rare but possible)
        style_pattern = r'\s*style="[^"]*?"'
        new_content = re.sub(style_pattern, '', content)

        if new_content != content:
            self.fixes_applied["inline_styles"] = len(re.findall(style_pattern, content))

        return new_content

    def _fix_diagram_failures(self, content: str) -> str:
        """Replace failed diagram rendering with mermaid placeholder or note.

        Pattern: ⚠️ Diagram rendering failed
                 [Diagram: Order State Machine] (image path if present)

        Replace with:
            ```mermaid
            graph TD
                A[Pending] --> B[Processing]
                B --> C[Completed]
            ```
        """
        # Match diagram rendering failure notices
        fail_pattern = r'⚠️\s*Diagram rendering failed[^\n]*'

        def replace_diagram(match: re.Match[str]) -> str:
            # Extract any nearby diagram title
            return """```mermaid
graph TD
    A["(Diagram rendered from Word<br/>See source document for details)"]
```"""

        new_content = re.sub(fail_pattern, replace_diagram, content)

        if new_content != content:
            self.fixes_applied["diagram_failures"] = len(re.findall(fail_pattern, content))

        return new_content

    def _add_image_alt_text(self, content: str) -> str:
        """Add or improve image alt text.

        Pattern: ![](media/image.png)
                  becomes
                 ![Descriptive text](media/image.png)

        This is a best-effort heuristic based on context.
        """
        # Pattern: image markdown without alt text
        image_pattern = r'!\[\]\(([^)]+)\)'

        def add_alt(match: re.Match[str]) -> str:
            image_path = match.group(1)
            # Generate alt text from filename (before extension)
            filename = Path(image_path).stem
            # Convert snake_case or kebab-case to Title Case
            alt_text = filename.replace('_', ' ').replace('-', ' ').title()
            return f"![{alt_text}]({image_path})"

        new_content = re.sub(image_pattern, add_alt, content)

        if new_content != content:
            self.fixes_applied["image_alt_text"] = len(re.findall(image_pattern, content))

        return new_content

    def get_summary(self) -> str:
        """Return a summary of fixes applied.

        Returns:
            Human-readable summary of cleanup actions
        """
        lines = ["Cleanup Summary:"]
        for fix_type, count in self.fixes_applied.items():
            lines.append(f"  - {fix_type}: {count} fixes")

        if self.warnings:
            lines.append("\nWarnings:")
            lines.extend(f"  - {w}" for w in self.warnings)

        return "\n".join(lines)


def apply_cleanup(markdown_path: Path) -> dict[str, int]:
    """One-shot cleanup function for a markdown file.

    Args:
        markdown_path: Path to .md file
    Returns:
        Dict of fixes applied
    """
    cleaner = MarkdownCleanup()
    cleaner.clean_file(markdown_path)
    return cleaner.fixes_applied
