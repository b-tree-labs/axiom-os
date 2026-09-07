# Copyright (c) 2026 B-Tree Ventures, LLC
# SPDX-License-Identifier: Apache-2.0

"""Scientific Displays — Pillar 1 (math + code rendering); Pillars 2-3 land later.

Per ADR-039 + spec-scientific-displays.md. Phase A scope:

- Code rendering (this commit set): Pygments-via-Rich, Axiom themes,
  language-badge + line-number gutter, ligature-font advisory,
  diff-aware integration.
- Math rendering (next commit set): LaTeX → matplotlib mathtext SVG →
  image-protocol display + Unicode pretty-print fallback + quick-codes.
- Chart spec (``chart_spec`` / ``channel_catalog`` / ``chart_validation``):
  the declarative document a deterministic renderer draws, its open kind and
  window-shape registries, and the resolver interface a spec is checked
  against. Schema only; the renderer is not here.

The extension is AEOS-conformant (`builtin = true`); see
``axiom-extension.toml`` for the manifest.
"""

from .channel_catalog import ChannelInfo, ChannelResolver, ResolverUnavailable
from .chart_spec import (
    SCHEMA_VERSION,
    ChartKind,
    ChartSpec,
    ChartSpecError,
    Transform,
    UnregisteredKindError,
    Window,
    WindowBasis,
    parse_document,
    parse_json,
    register_kind,
    register_window_basis,
    registered_kinds,
    registered_window_bases,
)
from .chart_validation import ValidationReport, ValidationStanding, validate_channels
from .code import (
    AxiomDarkTheme,
    AxiomHighContrastTheme,
    AxiomLightTheme,
    CodeBlock,
    detect_language,
    render_code_block,
)

__all__ = [
    "SCHEMA_VERSION",
    "AxiomDarkTheme",
    "AxiomHighContrastTheme",
    "AxiomLightTheme",
    "ChannelInfo",
    "ChannelResolver",
    "ChartKind",
    "ChartSpec",
    "ChartSpecError",
    "CodeBlock",
    "ResolverUnavailable",
    "Transform",
    "UnregisteredKindError",
    "ValidationReport",
    "ValidationStanding",
    "Window",
    "WindowBasis",
    "detect_language",
    "parse_document",
    "parse_json",
    "register_kind",
    "register_window_basis",
    "registered_kinds",
    "registered_window_bases",
    "render_code_block",
    "validate_channels",
]
