# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Render Mermaid diagrams in markdown to PNG images before pandoc conversion.

Pre-processes a .md file: finds ```mermaid``` code blocks, renders them
locally via mmdc (mermaid-cli) or falls back to mermaid.ink API.

Provider order:
1. Local mmdc (mermaid-cli) — no network, no rate limits, best quality
2. mermaid.ink API — fallback if mmdc not installed

Usage:
    from axiom.extensions.builtins.publishing.mermaid_renderer import render_mermaid_blocks

    processed_md = render_mermaid_blocks(md_content, output_dir)
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_MERMAID_INK_BASE = "https://mermaid.ink/img/pako:"
_CACHE_DIR_NAME = "mermaid_cache"


def _render_diagram(code: str, output_dir: Path, index: int) -> Path | None:
    """Render a single mermaid diagram to PNG. Prefers local mmdc over mermaid.ink."""
    # Use content hash for caching
    code_hash = hashlib.md5(code.encode()).hexdigest()[:8]
    cache_dir = output_dir / _CACHE_DIR_NAME
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / f"diagram_{code_hash}.png"

    if cached.exists() and cached.stat().st_size > 100:
        return cached

    # Clean up common syntax issues
    code = _sanitize_mermaid(code)

    # Try local mmdc first (no network, no rate limits)
    result = _render_with_mmdc(code, cached)
    if result:
        return result

    # Fall back to mermaid.ink API
    width = 1200
    if "gantt" in code.lower() or code.count("subgraph") > 2 or code.count("-->") > 15:
        width = 1800

    payload = json.dumps({
        "code": code,
        "mermaid": {"theme": "default"},
        "width": width,
    })
    compressed = zlib.compress(payload.encode(), 9)
    encoded = base64.urlsafe_b64encode(compressed).decode().rstrip("=")

    url = f"{_MERMAID_INK_BASE}{encoded}"

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Axiom PRESS)",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            content = resp.read()

        if len(content) < 100:
            first_line = code.split("\n", maxsplit=1)[0][:60]
            logger.warning("Mermaid render returned tiny image for diagram %d (%s...)", index, first_line)
            return None

        cached.write_bytes(content)
        logger.info("Rendered diagram %d (%d bytes) → %s", index, len(content), cached.name)
        return cached

    except urllib.error.HTTPError as e:
        first_line = code.split("\n", maxsplit=1)[0][:60]
        logger.debug(
            "Mermaid.ink fallback also failed for diagram %d (%s...): %s",
            index, first_line, e,
        )
        return None
    except Exception as e:
        first_line = code.split("\n", maxsplit=1)[0][:60]
        logger.debug("Mermaid render failed for diagram %d (%s...): %s", index, first_line, e)
        return None


def _render_with_mmdc(code: str, output_path: Path) -> Path | None:
    """Render a Mermaid diagram using local mmdc (mermaid-cli).

    Returns the output path on success, None if mmdc is not installed
    or rendering fails.
    """
    mmdc = shutil.which("mmdc")
    if not mmdc:
        return None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".mmd", delete=False, encoding="utf-8",
        ) as f:
            f.write(code)
            input_path = Path(f.name)

        result = subprocess.run(
            [mmdc, "-i", str(input_path), "-o", str(output_path),
             "-b", "white", "-s", "2"],
            capture_output=True, text=True, timeout=30,
        )

        input_path.unlink(missing_ok=True)

        if result.returncode == 0 and output_path.exists() and output_path.stat().st_size > 100:
            logger.info("Rendered diagram via mmdc → %s", output_path.name)
            return output_path

        if result.stderr:
            logger.warning("mmdc failed for diagram → %s: %s", output_path.name, result.stderr.strip()[:150])
        return None

    except Exception as e:
        logger.warning("mmdc exception: %s", e)
        return None


def _sanitize_mermaid(code: str) -> str:
    """Fix common syntax issues that cause mermaid.ink 400 errors."""
    # Remove HTML-style comments that mermaid.ink doesn't support
    code = re.sub(r"%%\{.*?\}%%", "", code)
    # Fix unescaped special chars in node labels
    # Replace problematic Unicode that mermaid.ink chokes on
    code = code.replace("\u2014", "--")  # em dash → double dash
    code = code.replace("\u2013", "-")   # en dash → dash
    code = code.replace("\u2018", "'")   # smart quote → straight
    code = code.replace("\u2019", "'")
    code = code.replace("\u201c", '"')
    code = code.replace("\u201d", '"')
    # Strip leading/trailing whitespace per line (indentation can break rendering)
    lines = [line.rstrip() for line in code.splitlines()]
    return "\n".join(lines)


@dataclass
class MermaidResult:
    """Result of rendering all mermaid blocks in a document."""
    content: str  # Processed markdown
    total: int = 0  # Total mermaid blocks found
    rendered: int = 0  # Successfully rendered
    failed: int = 0  # Failed to render
    failures: list = field(default_factory=list)  # List of (index, first_line, error) tuples

    @property
    def all_succeeded(self) -> bool:
        return self.failed == 0


def render_mermaid_blocks(md_content: str, output_dir: Path) -> MermaidResult:
    """Replace ```mermaid``` code blocks with rendered PNG image references.

    Args:
        md_content: Raw markdown content
        output_dir: Directory to save rendered images

    Returns:
        MermaidResult with processed content and render status.
        Caller should check result.all_succeeded before publishing.
    """
    pattern = re.compile(r"```mermaid\n(.*?)```", re.DOTALL)
    result = MermaidResult(content=md_content)

    blocks = list(pattern.finditer(md_content))
    result.total = len(blocks)

    if not blocks:
        return result

    failures = []

    def replace_block(match, _counter=None):  # noqa: B006
        if _counter is None:
            _counter = [0]
        code = match.group(1).strip()
        _counter[0] += 1
        idx = _counter[0]
        first_line = code.split("\n")[0][:60]

        img_path = _render_diagram(code, output_dir, idx)
        if img_path:
            result.rendered += 1
            return f"![]({img_path})"
        else:
            result.failed += 1
            failures.append((idx, first_line))
            # Keep as code block (not published — caller will block)
            return f"```\n{code}\n```"

    # Reset counter for each call
    replace_block.__defaults__ = ([0],)
    result.content = pattern.sub(replace_block, md_content)
    result.failures = failures

    return result
