# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Generic document branding for PRESS branded output.

Branding is a set of generic inputs — org wordmark, brand color, doc
title/type/date, and an optional logo image path. It is supplied by the
caller (frontmatter, ``--brand`` overrides, or config); PRESS never hardcodes
a consumer name, domain, or color. The default color is a neutral slate so
unbranded output is still presentable.

A ``Branding`` instance knows how to emit the CSS (``@page`` header/footer
band + brand-colored rules) and the running-header HTML used by the
WeasyPrint PDF path.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# Neutral default — deliberately NOT any consumer's brand color.
DEFAULT_BRAND_COLOR = "#334155"

# Frontmatter keys that map onto Branding fields.
_FIELD_KEYS = (
    "title", "subtitle", "org", "doc_type", "date", "brand_color", "logo",
    "author", "status",
)


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown string into (frontmatter dict, body).

    Recognizes a leading ``---``-fenced YAML block. Returns an empty dict and
    the original text when no frontmatter is present or it fails to parse.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    # lines[0] is the opening fence; find the closing fence.
    for idx in range(1, len(lines)):
        if lines[idx].rstrip("\n") in ("---", "..."):
            block = "".join(lines[1:idx])
            body = "".join(lines[idx + 1 :])
            try:
                meta = yaml.safe_load(block) or {}
            except yaml.YAMLError:
                return {}, text
            if not isinstance(meta, dict):
                return {}, text
            return meta, body
    return {}, text


@dataclass
class Branding:
    """Generic branding inputs for a published document."""

    title: str = ""
    subtitle: str = ""
    org: str = ""
    doc_type: str = ""
    date: str = ""
    brand_color: str = DEFAULT_BRAND_COLOR
    logo: str | None = None
    author: str = ""
    status: str = ""

    @classmethod
    def from_metadata(
        cls,
        meta: dict[str, Any] | None,
        overrides: dict[str, Any] | None = None,
    ) -> Branding:
        """Build branding from frontmatter metadata + explicit overrides.

        Overrides win over frontmatter. Unknown keys are ignored.
        """
        meta = meta or {}
        merged: dict[str, Any] = {}
        for key in _FIELD_KEYS:
            if meta.get(key) not in (None, ""):
                merged[key] = meta[key]
        for key, val in (overrides or {}).items():
            if val not in (None, ""):
                merged[key] = val
        if not merged.get("brand_color"):
            merged["brand_color"] = DEFAULT_BRAND_COLOR
        return cls(
            title=str(merged.get("title", "")),
            subtitle=str(merged.get("subtitle", "")),
            org=str(merged.get("org", "")),
            doc_type=str(merged.get("doc_type", "")),
            date=str(merged.get("date", "")),
            brand_color=str(merged.get("brand_color", DEFAULT_BRAND_COLOR)),
            logo=(str(merged["logo"]) if merged.get("logo") else None),
            author=str(merged.get("author", "")),
            status=str(merged.get("status", "")),
        )

    # ── Rendering helpers (WeasyPrint HTML+CSS path) ──

    def header_html(self) -> str:
        """Running header markup: optional logo + org wordmark + doc title."""
        org = html.escape(self.org)
        title = html.escape(self.title)
        doc_type = html.escape(self.doc_type)
        logo_html = ""
        if self.logo and Path(self.logo).exists():
            logo_html = (
                f'<img class="brand-logo" src="{html.escape(self.logo)}" '
                f'alt="{org} logo" />'
            )
        wordmark = f'<span class="brand-org">{org}</span>' if org else ""
        title_line = title
        if doc_type and title:
            title_line = f"{doc_type}: {title}"
        elif doc_type:
            title_line = doc_type
        return (
            '<div class="brand-header">'
            f'{logo_html}'
            f'<div class="brand-header-text">{wordmark}'
            f'<span class="brand-title">{html.escape(title_line)}</span></div>'
            '</div>'
        )

    def to_css(self) -> str:
        """Full CSS: brand-colored header/footer band, page numbers, date."""
        color = self.brand_color
        footer_left = html.escape(self.org or self.title)
        footer_date = html.escape(" · ".join(b for b in (self.date, self.status) if b))
        return f"""
@page {{
    size: letter;
    margin: 2.4cm 2cm 2.2cm 2cm;
    @top-left {{
        content: element(brandHeader);
    }}
    @bottom-left {{
        content: "{footer_left}";
        font-size: 8pt;
        color: #555;
        border-top: 2pt solid {color};
        width: 100%;
        padding-top: 4pt;
    }}
    @bottom-center {{
        content: "{footer_date}";
        font-size: 8pt;
        color: #555;
        padding-top: 4pt;
        white-space: nowrap;
    }}
    @bottom-right {{
        content: "Page " counter(page) " of " counter(pages);
        font-size: 8pt;
        color: #555;
        padding-top: 4pt;
    }}
}}

.brand-header {{
    position: running(brandHeader);
    display: flex;
    align-items: center;
    gap: 8pt;
    width: 100%;
    border-bottom: 3pt solid {color};
    padding-bottom: 4pt;
    font-size: 8.5pt;
}}
.brand-logo {{
    height: 22pt;
    width: auto;
}}
.brand-header-text {{
    display: flex;
    flex-direction: column;
    line-height: 1.2;
}}
.brand-org {{
    font-weight: 700;
    color: {color};
    letter-spacing: 0.2pt;
}}
.brand-title {{
    color: #444;
}}
.brand-author {{
    font-size: 7pt;
    color: #777;
}}

body {{
    font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
    font-size: 10.5pt;
    line-height: 1.45;
    color: #1a1a1a;
}}
h1, h2, h3, h4 {{
    color: {color};
    line-height: 1.2;
}}
h1 {{
    font-size: 15pt;
    border-bottom: 2pt solid {color};
    padding-bottom: 3pt;
}}
h2 {{ font-size: 12.5pt; }}
h3 {{ font-size: 11pt; }}
a {{ color: {color}; text-decoration: none; }}
code, pre {{
    font-family: "SFMono-Regular", Menlo, Consolas, monospace;
    font-size: 9pt;
    background: #f5f5f5;
}}
pre {{
    padding: 6pt;
    border-left: 3pt solid {color};
    overflow-wrap: break-word;
    white-space: pre-wrap;
}}
table {{ border-collapse: collapse; width: 100%; font-size: 9pt; table-layout: auto; }}
th, td {{
    border: 0.5pt solid #ccc;
    padding: 3pt 5pt;
    text-align: left;
    vertical-align: top;
}}
/* Only let genuinely long tokens (SKUs, URLs) break — not short row labels. */
td code, td a {{ overflow-wrap: anywhere; }}
td code {{ font-size: 8pt; background: none; }}
/* keep currency/numbers from breaking mid-value (see PandocPdfProvider._nowrap_prices) */
.num {{ white-space: nowrap; }}
th {{ background: {color}; color: #fff; overflow-wrap: normal; }}
blockquote {{
    border-left: 3pt solid {color};
    margin-left: 0;
    padding-left: 10pt;
    color: #444;
}}

/* Title block rendered at top of first page */
.brand-cover {{ margin-bottom: 14pt; }}
.brand-cover .cover-org {{
    color: {color};
    font-weight: 700;
    font-size: 11pt;
    text-transform: uppercase;
    letter-spacing: 0.5pt;
}}
.brand-cover .cover-title {{ font-size: 18pt; font-weight: 700; margin: 4pt 0 2pt; }}
.brand-cover .cover-subtitle {{ font-size: 13pt; color: #555; }}
.brand-cover .cover-meta {{ font-size: 9.5pt; color: #666; margin-top: 6pt; }}
.brand-cover .cover-author {{ font-size: 8pt; color: #999; font-weight: 400; margin-top: 8pt; }}
""".strip()

    def cover_html(self) -> str:
        """Cover/title block placed at the top of the document body."""
        parts = ['<div class="brand-cover">']
        if self.org:
            parts.append(f'<div class="cover-org">{html.escape(self.org)}</div>')
        if self.title:
            parts.append(f'<div class="cover-title">{html.escape(self.title)}</div>')
        if self.subtitle:
            parts.append(
                f'<div class="cover-subtitle">{html.escape(self.subtitle)}</div>'
            )
        meta_bits = [b for b in (self.doc_type, self.date, self.status) if b]
        if meta_bits:
            parts.append(
                f'<div class="cover-meta">{html.escape("  ·  ".join(meta_bits))}</div>'
            )
        if self.author:
            parts.append(
                f'<div class="cover-author">Prepared by {html.escape(self.author)}</div>'
            )
        parts.append("</div>")
        return "".join(parts)
