# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Cross-cutting text utilities — pluralize, bar, and other formatting helpers."""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Prose helpers
# ---------------------------------------------------------------------------


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Render '<count> <noun>' with correct grammar.

    >>> pluralize(1, 'message')
    '1 message'
    >>> pluralize(7, 'message')
    '7 messages'
    >>> pluralize(2, 'child', 'children')
    '2 children'
    """
    word = singular if count == 1 else (plural if plural is not None else singular + "s")
    return f"{count} {word}"


def bar(pct: float, width: int = 20) -> str:
    """Render a Unicode progress bar of the given width.

    Args:
        pct: Percentage filled (0–100). Values outside range are clamped.
        width: Total number of characters in the bar.

    >>> bar(50.0, width=4)
    '██░░'
    """
    pct = max(0.0, min(100.0, pct))
    filled = int(pct * width / 100)
    return "█" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# T4.1 — Cherenkov left-gutter rail
# ---------------------------------------------------------------------------

GUTTER_CHAR = "▎"  # U+258E LEFT VERTICAL BLOCK (1/3 width)

# Proper nouns that sentence-case must not lowercase
_PROPER_NOUNS: frozenset[str] = frozenset({
    "Axi", "Axiom", "Neut", "Neutron", "Vega", "Keplo", "Vyzier",
    "Anthropic", "OpenAI", "Claude", "GPT",
})


def gutter(text: str, *, color: str = "") -> str:
    """Prefix *text* with a Cherenkov-blue left rail.

    Used on welcome lines and surface headers to anchor visual identity.
    `color` defaults to the active branding accent (ACCENT_BLUE).
    """
    from axiom.setup.renderer import _c, _Colors, _use_color

    if not _use_color():
        return f"{GUTTER_CHAR} {text}"
    rail_color = color or _Colors.ACCENT_BLUE
    rail = _c(rail_color, GUTTER_CHAR)
    return f"{rail} {text}"


# ---------------------------------------------------------------------------
# T4.2 — header() helper
# ---------------------------------------------------------------------------


def header(label: str) -> str:
    """Render a surface header in sentence case, bold, no trailing colon.

    Strips a trailing ':', sentence-cases the label (first word capitalized,
    subsequent words lowercased unless they are known proper nouns), and
    wraps the result in BOLD when color is enabled.

    >>> header('Tokens')
    'Tokens'           # (plain, no color)
    >>> header('Health Check:')
    'Health check'     # (plain, no color)
    """
    from axiom.setup.renderer import _c, _Colors

    label = label.rstrip(":")
    words = label.split()
    if len(words) > 1:
        result_words = [words[0]]
        for w in words[1:]:
            result_words.append(w if w in _PROPER_NOUNS else w.lower())
        label = " ".join(result_words)

    return _c(_Colors.BOLD, label)


# ---------------------------------------------------------------------------
# T4.3 — surface_block() helper
# ---------------------------------------------------------------------------


def surface_block(lines: list[str]) -> str:
    """Render a slash-command output block with the contract:

    - exactly ONE leading blank line
    - ZERO trailing blank lines
    - the next render call (or REPL prompt) provides the separation
    """
    return "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# T4.8 — code_block_lines() helper
# ---------------------------------------------------------------------------


def code_block_lines(body: str, lang: str = "") -> list[str]:
    """Return a framed list of lines for a code block.

    Output format::

        ┌─ python
        │ x = 1
        │ y = 2

    The header uses DIM; body text is NOT dimmed (the rule frames it).
    """
    from axiom.setup.renderer import _c, _Colors, _use_color

    lang_label = lang or "code"
    if _use_color():
        header_line = _c(_Colors.DIM, f"┌─ {lang_label}")
        rule = _c(_Colors.DIM, "│ ")
    else:
        header_line = f"┌─ {lang_label}"
        rule = "│ "

    result = [header_line]
    for line in body.splitlines():
        result.append(f"{rule}{line}")
    return result


__all__ = [
    "pluralize",
    "bar",
    "GUTTER_CHAR",
    "gutter",
    "header",
    "surface_block",
    "code_block_lines",
]
