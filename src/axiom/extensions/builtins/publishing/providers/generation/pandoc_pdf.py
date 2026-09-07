# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""PandocPdfProvider — branded PDF from markdown.

Pipeline: markdown --(pandoc)--> HTML fragment --(branding CSS + cover +
running header)--> WeasyPrint --> PDF.

Branding is generic (org wordmark, brand color, title/type/date, optional
logo path). It is read from the document's YAML frontmatter and may be
overridden via ``options.metadata["brand"]`` (e.g. a ``--brand`` config or a
logo path supplied by the caller). No consumer name or color is hardcoded.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ...branding import Branding, parse_frontmatter
from ...factory import PublisherFactory
from ..base import (
    GenerationOptions,
    GenerationProvider,
    GenerationResult,
)


class PandocPdfProvider(GenerationProvider):
    """Generate branded .pdf files from markdown (pandoc → HTML → WeasyPrint)."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        # Caller-supplied brand defaults (lowest precedence below frontmatter).
        self.brand_defaults: dict[str, Any] = config.get("brand", {}) or {}
        self.pandoc_path = shutil.which("pandoc")

    def generate(
        self, source_path: Path, output_path: Path, options: GenerationOptions
    ) -> GenerationResult:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        if not self.pandoc_path:
            raise RuntimeError(
                "pandoc not found. Install with: brew install pandoc (macOS) "
                "or apt install pandoc (Linux)"
            )
        try:
            from weasyprint import HTML
        except Exception as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "weasyprint not installed. Install with: pip install weasyprint"
            ) from exc

        raw = source_path.read_text(encoding="utf-8")
        meta, _body = parse_frontmatter(raw)

        # Branding precedence: frontmatter < provider config < per-call override.
        overrides = dict(self.brand_defaults)
        overrides.update((options.metadata or {}).get("brand", {}) or {})
        branding = Branding.from_metadata(meta, overrides=overrides)

        body_html = self._markdown_to_html(source_path, options, warnings)

        # Strip a leading H1 that duplicates the cover title, if present.
        if branding.title:
            body_html = self._drop_duplicate_title(body_html, branding.title)

        full_html = self._assemble_html(branding, body_html)

        # base_url lets WeasyPrint resolve a relative logo path + local images.
        base_url = str(source_path.parent)
        HTML(string=full_html, base_url=base_url).write_pdf(str(output_path))

        if not output_path.exists():
            raise RuntimeError(f"WeasyPrint failed to produce {output_path}")

        return GenerationResult(
            output_path=output_path,
            format="pdf",
            size_bytes=output_path.stat().st_size,
            warnings=warnings,
        )

    def _markdown_to_html(
        self, source: Path, options: GenerationOptions, warnings: list[str]
    ) -> str:
        """Render markdown body to an HTML fragment via pandoc (gfm)."""
        cmd = [
            self.pandoc_path,
            # Disable `$...$` LaTeX-math parsing: documents routinely contain
            # currency ("$50–60K"), and pandoc would otherwise treat the span
            # between two `$` as math — stripping spaces, dropping `**` bold,
            # and turning hyphens into minus signs.
            "-f",
            "gfm-tex_math_dollars",
            "-t",
            "html",
            str(source),
        ]
        if options.toc or options.toc:  # toc default True
            cmd.extend(["--toc", f"--toc-depth={options.toc_depth or 3}"])
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(source.parent),
                timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("pandoc timed out converting markdown to HTML") from exc
        if result.returncode != 0:
            warnings.append(f"pandoc stderr: {result.stderr[:200]}")
        return self._nowrap_prices(result.stdout)

    @staticmethod
    def _nowrap_prices(body_html: str) -> str:
        """Wrap currency tokens (``$25–30K``, ``$7–10K ea``) in a no-wrap span.

        Auto table layout otherwise breaks short price cells mid-value when a
        long neighbour (a SKU/URL) takes the column width. Keeping the whole
        currency token together fixes that without per-column styling.
        """
        price_re = re.compile(
            r"~?\$\d[\d.,]*(?:[–-]\d[\d.,]*)?\s*K?(?:\s*ea\b)?"
        )

        def repl(m: re.Match) -> str:
            token = m.group(0).replace(" ", chr(0xA0))  # nbsp inside the token
            return f'<span class="num">{token}</span>'

        return price_re.sub(repl, body_html)

    @staticmethod
    def _drop_duplicate_title(body_html: str, title: str) -> str:
        """Remove a leading ``<h1>`` whose text matches the cover title."""
        norm = re.sub(r"\s+", " ", title).strip().lower()
        match = re.match(r"\s*<h1[^>]*>(.*?)</h1>", body_html, re.IGNORECASE | re.DOTALL)
        if match:
            inner = re.sub(r"<[^>]+>", "", match.group(1))
            if re.sub(r"\s+", " ", inner).strip().lower() == norm:
                return body_html[match.end():]
        return body_html

    @staticmethod
    def _assemble_html(branding: Branding, body_html: str) -> str:
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{branding.to_css()}</style></head><body>"
            f"{branding.header_html()}"
            f"{branding.cover_html()}"
            f"{body_html}"
            "</body></html>"
        )

    def rewrite_links(self, artifact_path: Path, link_map: dict[str, str]) -> None:
        """PDF is a terminal artifact; link rewriting happens upstream in md."""
        return

    def get_output_extension(self) -> str:
        return ".pdf"

    def supports_watermark(self) -> bool:
        return False


# Self-register with factory
PublisherFactory.register("generation", "pandoc-pdf", PandocPdfProvider)
