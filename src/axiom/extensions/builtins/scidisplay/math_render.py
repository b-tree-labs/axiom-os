# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Math rendering pipeline — Sci Displays Pillar 1, math half.

Per ADR-039 + spec §5/§6:

  LaTeX (or quick-coded text) →
    quick-code expansion →
    SymPy parse (best-effort; non-fatal on failure) →
    {  matplotlib mathtext SVG/PNG  if image protocol available
       sympy.printing.pretty Unicode multi-line  otherwise }
    + provenance receipt stub (Phase A: local hash only; signing in Phase B)

The renderer writes SVG + PNG to a content-addressed cache under
``$AXI_STATE_DIR/scidisplay/math/`` so re-renders are free. The cache
key is the SHA-256 hex of the *expanded* LaTeX (post quick-code), which
is the canonical input form.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .quickcodes import expand_quickcodes

log = logging.getLogger(__name__)

_DEFAULT_DPI = 200
_DEFAULT_FONTSIZE = 14
_RECEIPT_PREFIX = "axiom://math/"


@dataclass(frozen=True)
class MathBlock:
    latex: str
    display: bool = True   # display style ($$..$$) vs inline ($..$)
    label: str = ""        # optional human label (eq. number, name)


@dataclass(frozen=True)
class MathRender:
    source: str               # original input (pre quick-code)
    expanded: str             # after quick-code expansion
    pretty_text: str          # always populated; the Unicode fallback
    svg_path: Path | None     # populated when matplotlib renders successfully
    png_path: Path | None     # populated when SVG render succeeded
    receipt_id: str           # local-only ID in Phase A; signed in Phase B
    sympy_parsed: bool        # whether SymPy could parse — affects compute path


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_dir() -> Path:
    """Return the on-disk render cache for math artifacts.

    Mirrors the Mermaid pattern (per spec §6.4) — content-addressed
    files in ``$AXI_STATE_DIR/scidisplay/math/`` so re-renders are
    skipped.
    """
    try:
        from axiom.infra.paths import get_user_state_dir

        base = get_user_state_dir() / "scidisplay" / "math"
    except Exception:
        base = Path.home() / ".axi" / "scidisplay" / "math"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _content_hash(expanded_latex: str) -> str:
    return hashlib.sha256(expanded_latex.encode("utf-8")).hexdigest()[:16]


def _receipt_id(content_hash: str) -> str:
    return f"{_RECEIPT_PREFIX}{content_hash}"


# ---------------------------------------------------------------------------
# Pretty (always available — text-only)
# ---------------------------------------------------------------------------


def render_pretty(latex_or_expanded: str) -> str:
    """Return a Unicode multi-line representation via SymPy.

    Tries to parse the input as LaTeX. On parser failure, falls back to
    the raw expanded text (which already has Greek + operator glyphs
    substituted by quick-code expansion). Never raises.
    """
    try:
        from sympy.parsing.latex import parse_latex
        from sympy.printing.pretty import pretty as _pretty

        expr = parse_latex(latex_or_expanded)
    except Exception:
        # SymPy can't parse it — return the quick-code-expanded source
        # so the user at least sees the Greek glyphs they typed.
        return latex_or_expanded

    try:
        return _pretty(expr, use_unicode=True)
    except Exception:
        return str(expr)


def parse_to_sympy(latex: str) -> Any | None:
    """Attempt to parse LaTeX to a SymPy expression. Returns ``None`` on
    any failure — callers decide whether to compute or just display."""
    try:
        from sympy.parsing.latex import parse_latex

        return parse_latex(latex)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# SVG / PNG via matplotlib (image-protocol path)
# ---------------------------------------------------------------------------


def _render_via_matplotlib(
    expanded_latex: str,
    out_svg: Path,
    out_png: Path,
    *,
    fontsize: int = _DEFAULT_FONTSIZE,
    dpi: int = _DEFAULT_DPI,
    display_mode: bool = True,
) -> bool:
    """Render via matplotlib mathtext. Returns True iff both files written.

    Errors (bad LaTeX, missing fonts, headless backend issue) are caught
    and return False — the caller falls back to the Unicode pretty path.
    """
    try:
        import matplotlib

        # Force a non-interactive backend; we never show() anything.
        matplotlib.use("Agg", force=False)
        import matplotlib.pyplot as plt
    except Exception as exc:
        log.warning("scidisplay math: matplotlib unavailable: %s", exc)
        return False

    # Mathtext requires `$...$`; matplotlib mathtext does NOT support
    # `\displaystyle` (full LaTeX construct, not in mathtext's grammar)
    # so we use the fontsize bump as a proxy for display style.
    body = expanded_latex.strip()
    # Strip any user-supplied $-wrapping; we control the wrapping below.
    if body.startswith("$$") and body.endswith("$$") and len(body) > 4:
        body = body[2:-2]
    elif body.startswith("$") and body.endswith("$") and len(body) > 2:
        body = body[1:-1]
    wrapped = f"${body}$"
    effective_fontsize = int(fontsize * 1.4) if display_mode else fontsize

    fig = plt.figure(figsize=(0.01, 0.01), dpi=dpi)
    try:
        # Render the math at the given fontsize on a transparent background.
        text = fig.text(
            0,
            0,
            wrapped,
            fontsize=effective_fontsize,
            usetex=False,  # mathtext, not full LaTeX (no system tex needed)
        )
        fig.canvas.draw()
        bbox = text.get_window_extent()
        width = (bbox.width / dpi) + 0.2
        height = (bbox.height / dpi) + 0.2
        fig.set_size_inches(max(width, 0.5), max(height, 0.3))
        fig.savefig(out_svg, format="svg", transparent=True, bbox_inches="tight", pad_inches=0.02)
        fig.savefig(out_png, format="png", transparent=True, bbox_inches="tight", pad_inches=0.02, dpi=dpi)
    except Exception as exc:
        log.warning("scidisplay math: render failed for %r: %s", body[:40], exc)
        return False
    finally:
        plt.close(fig)

    return out_svg.exists() and out_png.exists()


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------


def render_math(block: MathBlock) -> MathRender:
    """Render a math block end-to-end.

    Always returns a populated ``MathRender``. On any internal error,
    image fields are ``None`` and ``pretty_text`` carries the textual
    fallback so the caller has *something* to display.
    """
    expanded = expand_quickcodes(block.latex)
    digest = _content_hash(expanded)

    cache = _cache_dir()
    svg_path = cache / f"{digest}.svg"
    png_path = cache / f"{digest}.png"

    # Cache hit — skip the matplotlib pass entirely.
    if not (svg_path.exists() and png_path.exists()):
        ok = _render_via_matplotlib(
            expanded, svg_path, png_path, display_mode=block.display
        )
        if not ok:
            # Wipe partial outputs to keep the cache clean.
            for p in (svg_path, png_path):
                try:
                    if p.exists():
                        p.unlink()
                except OSError:
                    pass

    pretty = render_pretty(expanded)
    parsed = parse_to_sympy(expanded) is not None

    return MathRender(
        source=block.latex,
        expanded=expanded,
        pretty_text=pretty,
        svg_path=svg_path if svg_path.exists() else None,
        png_path=png_path if png_path.exists() else None,
        receipt_id=_receipt_id(digest),
        sympy_parsed=parsed,
    )


__all__ = [
    "MathBlock",
    "MathRender",
    "expand_quickcodes",  # re-export for convenience
    "parse_to_sympy",
    "render_math",
    "render_pretty",
]
