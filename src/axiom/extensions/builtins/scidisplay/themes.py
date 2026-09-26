# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom-branded Pygments themes for code rendering.

Three themes per ADR-039 D12 + spec §6b.3, anchored to the brand
palette from ``project_axiom_labs_brand``:

- ``axiom-dark`` (default) — graphite ``#2e2e2e`` background, off-white
  ``#f4f1ec`` default text, UT burnt-orange ``#BF5700`` keyword accent.
- ``axiom-light`` — inverted palette for bright environments / printable.
- ``axiom-high-contrast`` — pure black / white + saturated orange for
  WCAG AAA accessibility.

Token color choices follow the spec §6b.3 mapping table:

- Keywords get the brand orange — language structure is the brand-anchor.
- Function names: light blue (``#7fb3d5``), calm complement to accent.
- Class names: warm tan (``#dfa86a``), lower saturation than accent so
  classes don't compete visually.
- Strings: muted sage (``#a0c89a``), highly frequent so deliberately calm.
- Numbers: pink-mauve (``#dca3c2``), distinct from strings at a glance.
- Comments: mid-grey italic, de-emphasized but readable.
- Operators: default text color — never accent-color (creates noise on
  math-heavy code).
- Errors: high-contrast red on red-bg.

Themes publish as Pygments style classes via the package's
``pygments.styles`` entry point so the same themes work in ``bat``,
GitHub Codespaces, IPython, and any other Pygments consumer.
"""

from __future__ import annotations

from pygments.style import Style
from pygments.token import (
    Comment,
    Error,
    Generic,
    Keyword,
    Literal,
    Name,
    Number,
    Operator,
    Punctuation,
    String,
    Text,
    Token,
    Whitespace,
)

# --- Brand palette ----------------------------------------------------------

BRAND_GRAPHITE = "#2e2e2e"
BRAND_OFF_WHITE = "#f4f1ec"
BRAND_BURNT_ORANGE = "#BF5700"

# --- Token-color choices (spec §6b.3) ---------------------------------------

_BLUE = "#7fb3d5"          # function names — calm complement
_TAN = "#dfa86a"           # class names — warmer, lower saturation
_SAGE = "#a0c89a"          # strings — calm, frequent
_PINK = "#dca3c2"          # numbers — distinct from strings
_GREY = "#7d7d7d"          # comments — de-emphasized
_ERROR_FG = "#ff5555"
_ERROR_BG = "#3d2020"


class AxiomDarkTheme(Style):
    """Default theme. Graphite background; brand orange on keywords."""

    name = "axiom-dark"
    background_color = BRAND_GRAPHITE
    highlight_color = "#3d3d3d"

    styles = {
        Token: BRAND_OFF_WHITE,
        Whitespace: BRAND_OFF_WHITE,
        Text: BRAND_OFF_WHITE,
        Comment: f"italic {_GREY}",
        Comment.Preproc: f"italic {_GREY}",
        Comment.Special: f"italic bold {_GREY}",
        Keyword: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Constant: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Declaration: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Namespace: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Pseudo: BRAND_BURNT_ORANGE,
        Keyword.Reserved: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Type: f"{_TAN}",
        Operator: BRAND_OFF_WHITE,
        Operator.Word: f"bold {BRAND_BURNT_ORANGE}",
        Punctuation: BRAND_OFF_WHITE,
        Name: BRAND_OFF_WHITE,
        Name.Function: _BLUE,
        Name.Function.Magic: f"italic {_BLUE}",
        Name.Class: _TAN,
        Name.Decorator: f"italic {_TAN}",
        Name.Builtin: BRAND_BURNT_ORANGE,
        Name.Builtin.Pseudo: BRAND_BURNT_ORANGE,
        Name.Constant: f"bold {_PINK}",
        Name.Tag: f"bold {BRAND_BURNT_ORANGE}",
        Name.Attribute: _BLUE,
        Name.Variable: BRAND_OFF_WHITE,
        Name.Namespace: _TAN,
        Name.Exception: f"bold {_ERROR_FG}",
        Number: _PINK,
        Number.Float: _PINK,
        Number.Hex: _PINK,
        Number.Integer: _PINK,
        Number.Oct: _PINK,
        String: _SAGE,
        String.Affix: _SAGE,
        String.Char: _SAGE,
        String.Doc: f"italic {_GREY}",
        String.Escape: f"bold {_PINK}",
        String.Interpol: f"bold {_PINK}",
        Literal: _SAGE,
        Generic.Heading: f"bold {BRAND_OFF_WHITE}",
        Generic.Subheading: f"bold {_TAN}",
        Generic.Deleted: _ERROR_FG,
        Generic.Inserted: _SAGE,
        Generic.Strong: f"bold {BRAND_OFF_WHITE}",
        Generic.Emph: f"italic {BRAND_OFF_WHITE}",
        Generic.Output: _GREY,
        Generic.Prompt: f"bold {_BLUE}",
        Error: f"bg:{_ERROR_BG} {_ERROR_FG}",
    }


class AxiomLightTheme(Style):
    """Light variant for bright environments / printable code blocks."""

    name = "axiom-light"
    background_color = BRAND_OFF_WHITE
    highlight_color = "#e8e3d8"

    styles = {
        Token: BRAND_GRAPHITE,
        Whitespace: BRAND_GRAPHITE,
        Text: BRAND_GRAPHITE,
        Comment: "italic #6a6a6a",
        Comment.Preproc: "italic #6a6a6a",
        Comment.Special: "italic bold #6a6a6a",
        Keyword: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Constant: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Declaration: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Namespace: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Pseudo: BRAND_BURNT_ORANGE,
        Keyword.Reserved: f"bold {BRAND_BURNT_ORANGE}",
        Keyword.Type: "#8a5a30",
        Operator: BRAND_GRAPHITE,
        Operator.Word: f"bold {BRAND_BURNT_ORANGE}",
        Punctuation: BRAND_GRAPHITE,
        Name: BRAND_GRAPHITE,
        Name.Function: "#1a5c8a",
        Name.Function.Magic: "italic #1a5c8a",
        Name.Class: "#8a5a30",
        Name.Decorator: "italic #8a5a30",
        Name.Builtin: BRAND_BURNT_ORANGE,
        Name.Builtin.Pseudo: BRAND_BURNT_ORANGE,
        Name.Constant: "bold #8a3a5a",
        Name.Tag: f"bold {BRAND_BURNT_ORANGE}",
        Name.Attribute: "#1a5c8a",
        Name.Variable: BRAND_GRAPHITE,
        Name.Namespace: "#8a5a30",
        Name.Exception: "bold #b03030",
        Number: "#8a3a5a",
        String: "#3a6a30",
        String.Doc: "italic #6a6a6a",
        String.Escape: "bold #8a3a5a",
        String.Interpol: "bold #8a3a5a",
        Literal: "#3a6a30",
        Generic.Heading: f"bold {BRAND_GRAPHITE}",
        Generic.Deleted: "#b03030",
        Generic.Inserted: "#3a6a30",
        Generic.Strong: f"bold {BRAND_GRAPHITE}",
        Generic.Emph: f"italic {BRAND_GRAPHITE}",
        Generic.Output: "#6a6a6a",
        Generic.Prompt: "bold #1a5c8a",
        Error: "bg:#fadcdc #b03030",
    }


class AxiomHighContrastTheme(Style):
    """WCAG AAA accessibility variant. Pure black/white + saturated orange."""

    name = "axiom-high-contrast"
    background_color = "#000000"
    highlight_color = "#1a1a1a"

    _HC_ORANGE = "#FF6B1A"
    _HC_CYAN = "#00ddff"
    _HC_YELLOW = "#ffe000"
    _HC_GREEN = "#00ff7f"
    _HC_PINK = "#ff7fcc"
    _HC_GREY = "#cccccc"

    styles = {
        Token: "#ffffff",
        Whitespace: "#ffffff",
        Text: "#ffffff",
        Comment: f"italic {_HC_GREY}",
        Comment.Preproc: f"italic {_HC_GREY}",
        Keyword: f"bold {_HC_ORANGE}",
        Keyword.Constant: f"bold {_HC_ORANGE}",
        Keyword.Declaration: f"bold {_HC_ORANGE}",
        Keyword.Namespace: f"bold {_HC_ORANGE}",
        Keyword.Type: _HC_YELLOW,
        Operator: "#ffffff",
        Operator.Word: f"bold {_HC_ORANGE}",
        Punctuation: "#ffffff",
        Name: "#ffffff",
        Name.Function: _HC_CYAN,
        Name.Class: _HC_YELLOW,
        Name.Decorator: f"italic {_HC_YELLOW}",
        Name.Builtin: _HC_ORANGE,
        Name.Constant: f"bold {_HC_PINK}",
        Name.Tag: f"bold {_HC_ORANGE}",
        Name.Attribute: _HC_CYAN,
        Name.Exception: f"bold {_ERROR_FG}",
        Number: _HC_PINK,
        String: _HC_GREEN,
        String.Doc: f"italic {_HC_GREY}",
        String.Escape: f"bold {_HC_PINK}",
        Literal: _HC_GREEN,
        Generic.Deleted: _ERROR_FG,
        Generic.Inserted: _HC_GREEN,
        Generic.Output: _HC_GREY,
        Error: f"bg:{_ERROR_BG} bold {_ERROR_FG}",
    }


THEMES: dict[str, type[Style]] = {
    "axiom-dark": AxiomDarkTheme,
    "axiom-light": AxiomLightTheme,
    "axiom-high-contrast": AxiomHighContrastTheme,
}


def get_theme(name: str = "axiom-dark") -> type[Style]:
    """Return a theme class by name. Falls back to axiom-dark on unknown name."""
    return THEMES.get(name, AxiomDarkTheme)


__all__ = [
    "AxiomDarkTheme",
    "AxiomHighContrastTheme",
    "AxiomLightTheme",
    "BRAND_BURNT_ORANGE",
    "BRAND_GRAPHITE",
    "BRAND_OFF_WHITE",
    "THEMES",
    "get_theme",
]
