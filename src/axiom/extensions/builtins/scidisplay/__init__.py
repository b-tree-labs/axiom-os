# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Scientific Displays — Pillar 1 (math + code rendering); Pillars 2-3 land later.

Per ADR-039 + spec-scientific-displays.md. Phase A scope:

- Code rendering (this commit set): Pygments-via-Rich, Axiom themes,
  language-badge + line-number gutter, ligature-font advisory,
  diff-aware integration.
- Math rendering (next commit set): LaTeX → matplotlib mathtext SVG →
  image-protocol display + Unicode pretty-print fallback + quick-codes.

The extension is AEOS-conformant (`builtin = true`); see
``axiom-extension.toml`` for the manifest.
"""

from .code import (
    AxiomDarkTheme,
    AxiomHighContrastTheme,
    AxiomLightTheme,
    CodeBlock,
    detect_language,
    render_code_block,
)

__all__ = [
    "AxiomDarkTheme",
    "AxiomHighContrastTheme",
    "AxiomLightTheme",
    "CodeBlock",
    "detect_language",
    "render_code_block",
]
