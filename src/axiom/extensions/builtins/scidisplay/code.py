# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Code rendering — Pillar 1 (code half) of Scientific Displays.

Per ADR-039 D12 + spec §6b:

- Lexer selection priority: explicit fence > path-hint > ``guess_lexer`` > plaintext.
- Render via Rich's ``Syntax`` widget on top of Pygments lexers.
- Themes via :mod:`axiom.extensions.builtins.scidisplay.themes`
  (``axiom-dark`` default, ``axiom-light``, ``axiom-high-contrast``).
- Line-number gutter when block > 10 lines.
- Language badge corner pill (when terminal supports box-drawing).
- Ligature-font advisory: shown once per session; suppressible via
  ``/hint suppress code-font``.
- ``write_file``-emitted blocks get routed through the existing
  ``chat/diff_render.py`` path so they appear as typed diffs.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .themes import (
    AxiomDarkTheme,
    AxiomHighContrastTheme,
    AxiomLightTheme,
    get_theme,
)

# Re-export themes for the package public API.
__all__ = [
    "AxiomDarkTheme",
    "AxiomHighContrastTheme",
    "AxiomLightTheme",
    "CodeBlock",
    "detect_language",
    "render_code_block",
    "should_show_ligature_advisory",
    "suppress_ligature_advisory",
]


_GUTTER_THRESHOLD = 10  # blocks larger than this get line numbers
_GUESS_CONFIDENCE_THRESHOLD = 0.10  # below this, fall back to plaintext


@dataclass(frozen=True)
class CodeBlock:
    body: str
    language: str | None = None       # explicit hint from fence info-string
    path_hint: str | None = None      # filename hint when from a write_file action
    diff_against: Path | None = None  # set by chat surface when block is an edit
    extra: dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Lexer selection
# ----------------------------------------------------------------------------


def _lexer_from_path(path: str) -> str | None:
    """Map a path's extension to a Pygments short-name. Returns None for unknown."""
    try:
        from pygments.lexers import get_lexer_for_filename

        lexer = get_lexer_for_filename(Path(path).name, code="")
        return lexer.aliases[0] if lexer.aliases else lexer.name
    except Exception:
        return None


def _lexer_from_explicit(language: str) -> str | None:
    """Map an explicit fence info-string (``python``, ``ts``, ``rs``, ...)
    to a known Pygments short-name. Returns None if we can't resolve it,
    so the caller can fall through to path hint / guess."""
    try:
        from pygments.lexers import get_lexer_by_name

        lexer = get_lexer_by_name(language)
        return lexer.aliases[0] if lexer.aliases else lexer.name
    except Exception:
        return None


def _lexer_from_guess(body: str) -> str | None:
    """Pygments built-in body-shape guesser. Returns None if confidence is
    below threshold (we'd rather render plaintext than mislabel)."""
    try:
        from pygments.lexers import guess_lexer

        lexer = guess_lexer(body)
        # Pygments' analyse_text returns 0..1 confidence; we accept anything
        # above the threshold and require an alias (some odd lexers have none).
        confidence = lexer.analyse_text(body) if hasattr(lexer, "analyse_text") else 1.0
        if confidence < _GUESS_CONFIDENCE_THRESHOLD:
            return None
        return lexer.aliases[0] if lexer.aliases else lexer.name
    except Exception:
        return None


def detect_language(block: CodeBlock) -> str:
    """Resolve the lexer name per the priority chain.

    Returns the Pygments short-name (``python``, ``rust``, etc.) or
    ``"text"`` when nothing matches with sufficient confidence.
    """
    if block.language:
        explicit = _lexer_from_explicit(block.language)
        if explicit:
            return explicit
    if block.path_hint:
        path_match = _lexer_from_path(block.path_hint)
        if path_match:
            return path_match
    guess = _lexer_from_guess(block.body)
    if guess:
        return guess
    return "text"


# ----------------------------------------------------------------------------
# Rendering
# ----------------------------------------------------------------------------


def render_code_block(
    block: CodeBlock,
    *,
    theme: str = "axiom-dark",
) -> str:
    """Render the block to a terminal-ready ANSI string.

    - Resolves the lexer via :func:`detect_language`.
    - Uses Rich's ``Syntax`` widget with our Pygments theme.
    - Adds a line-number gutter when the block is longer than
      :data:`_GUTTER_THRESHOLD` lines.
    - Caller is responsible for the language-badge + ligature-advisory
      placement (those are surface-specific — chat surface vs cmd-line
      lay them out differently).
    """
    try:
        from io import StringIO

        from rich.console import Console
        from rich.syntax import Syntax
    except ImportError:
        # Rich is shipped via the chat extension's deps; if absent
        # (unusual install), degrade to plain text + a hint.
        return block.body + "\n  (axiom-scidisplay: install rich for highlighted output)\n"

    lexer_name = detect_language(block)
    line_count = block.body.count("\n") + 1
    show_gutter = line_count > _GUTTER_THRESHOLD

    theme_cls = get_theme(theme)
    syntax = Syntax(
        block.body.rstrip("\n"),
        lexer_name,
        theme=theme_cls,
        line_numbers=show_gutter,
        word_wrap=False,
        background_color=theme_cls.background_color,
    )

    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        force_interactive=False,
        color_system="truecolor",
        record=False,
        legacy_windows=False,
    )
    console.print(syntax)
    return buf.getvalue()


def render_language_badge(language: str, line_count: int) -> str:
    """Tiny corner pill: ``language · N`` lines.

    Caller decides where to put it; we only produce the string. Chat-surface
    layout in ``chat/fullscreen.py`` is responsible for box-drawing borders.
    """
    return f"{language} · {line_count}"


# ----------------------------------------------------------------------------
# Ligature advisory (one-time-per-session, suppressible)
# ----------------------------------------------------------------------------


def _hints_path() -> Path:
    try:
        from axiom.infra.paths import get_user_state_dir

        base = get_user_state_dir() / "scidisplay"
        base.mkdir(parents=True, exist_ok=True)
        return base / "hints.json"
    except Exception:
        # Fallback — keep advisory state out of the way.
        return Path.home() / ".axi" / "scidisplay" / "hints.json"


def _load_hints() -> dict[str, Any]:
    p = _hints_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_hints(d: dict[str, Any]) -> None:
    p = _hints_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, sort_keys=True))
    except Exception:
        pass  # advisory state is best-effort


_LIGATURE_HINT_KEY = "code_font_advisory"
_LIGATURE_HINT_TEXT = (
    "  Tip: install JetBrains Mono / Fira Code / Cascadia Code for code "
    "ligatures (=> != == >=).\n"
    "       Hide future tips with: /hint suppress code-font"
)


def should_show_ligature_advisory() -> bool:
    """True when (a) advisory not suppressed AND (b) not yet shown this session.

    'Session' here = the lifetime of the current Python process; tracked
    via env var ``_AXIOM_SCIDISPLAY_FONT_HINT_SHOWN`` so threads / re-imports
    don't double-fire.
    """
    hints = _load_hints()
    if hints.get(_LIGATURE_HINT_KEY) == "suppressed":
        return False
    if os.environ.get("_AXIOM_SCIDISPLAY_FONT_HINT_SHOWN") == "1":
        return False
    return True


def mark_ligature_advisory_shown() -> None:
    """Record that the advisory was shown this process — caller does this
    immediately after rendering the tip text."""
    os.environ["_AXIOM_SCIDISPLAY_FONT_HINT_SHOWN"] = "1"


def suppress_ligature_advisory() -> None:
    """Persist the suppression so future processes don't show the tip."""
    hints = _load_hints()
    hints[_LIGATURE_HINT_KEY] = "suppressed"
    _save_hints(hints)


def ligature_advisory_text() -> str:
    """The advisory string. Caller decides whether to print it."""
    return _LIGATURE_HINT_TEXT
