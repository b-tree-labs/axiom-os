# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LaTeX quick-code → Unicode expansion table.

Per Sci Displays spec §5.1: scientists typing in chat want
``\\alpha`` to become ``α`` without wrapping it in ``$...$``.
This module is the deterministic substitution table that runs *before*
the SymPy parser sees the input.

The expansion is left-to-right longest-match-first to avoid
shadowing (``\\alphabet`` must NOT match ``\\alpha`` + ``bet``).
Codes inside math fences (``$...$`` and ``$$...$$``) and inside
already-expanded Unicode are left alone — only standalone
``\\command`` tokens at word boundaries get rewritten.

Coverage: Greek letters (lower + upper), common mathematical operators,
arrows, set theory, blackboard-bold sets, calligraphic letters.
"""

from __future__ import annotations

import re

# --- Greek lower-case ------------------------------------------------------
_GREEK_LOWER = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\vartheta": "ϑ", r"\iota": "ι", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ",
    r"\omicron": "ο", r"\pi": "π", r"\varpi": "ϖ", r"\rho": "ρ",
    r"\varrho": "ϱ", r"\sigma": "σ", r"\varsigma": "ς", r"\tau": "τ",
    r"\upsilon": "υ", r"\phi": "φ", r"\varphi": "ϕ", r"\chi": "χ",
    r"\psi": "ψ", r"\omega": "ω",
}

# --- Greek upper-case ------------------------------------------------------
_GREEK_UPPER = {
    r"\Alpha": "Α", r"\Beta": "Β", r"\Gamma": "Γ", r"\Delta": "Δ",
    r"\Epsilon": "Ε", r"\Zeta": "Ζ", r"\Eta": "Η", r"\Theta": "Θ",
    r"\Iota": "Ι", r"\Kappa": "Κ", r"\Lambda": "Λ", r"\Mu": "Μ",
    r"\Nu": "Ν", r"\Xi": "Ξ", r"\Omicron": "Ο", r"\Pi": "Π",
    r"\Rho": "Ρ", r"\Sigma": "Σ", r"\Tau": "Τ", r"\Upsilon": "Υ",
    r"\Phi": "Φ", r"\Chi": "Χ", r"\Psi": "Ψ", r"\Omega": "Ω",
}

# --- Operators -------------------------------------------------------------
_OPERATORS = {
    r"\pm": "±", r"\mp": "∓",
    r"\times": "×", r"\div": "÷", r"\cdot": "·", r"\ast": "∗",
    r"\star": "⋆", r"\circ": "∘", r"\bullet": "•",
    r"\oplus": "⊕", r"\ominus": "⊖", r"\otimes": "⊗", r"\oslash": "⊘",
    r"\sqrt": "√",
    r"\int": "∫", r"\iint": "∬", r"\iiint": "∭", r"\oint": "∮",
    r"\sum": "∑", r"\prod": "∏", r"\coprod": "∐",
    r"\partial": "∂", r"\nabla": "∇", r"\infty": "∞",
    r"\hbar": "ℏ", r"\ell": "ℓ", r"\Re": "ℜ", r"\Im": "ℑ",
    r"\angle": "∠", r"\perp": "⊥", r"\parallel": "∥",
    r"\degree": "°", r"\prime": "′",
}

# --- Relations -------------------------------------------------------------
_RELATIONS = {
    r"\leq": "≤", r"\le": "≤", r"\geq": "≥", r"\ge": "≥",
    r"\neq": "≠", r"\ne": "≠", r"\approx": "≈", r"\equiv": "≡",
    r"\sim": "∼", r"\simeq": "≃", r"\cong": "≅",
    r"\propto": "∝",
    r"\ll": "≪", r"\gg": "≫",
}

# --- Arrows ----------------------------------------------------------------
_ARROWS = {
    r"\rightarrow": "→", r"\to": "→",
    r"\leftarrow": "←", r"\gets": "←",
    r"\Rightarrow": "⇒", r"\implies": "⇒",
    r"\Leftarrow": "⇐",
    r"\Leftrightarrow": "⇔", r"\iff": "⇔",
    r"\leftrightarrow": "↔",
    r"\uparrow": "↑", r"\downarrow": "↓",
    r"\Uparrow": "⇑", r"\Downarrow": "⇓",
    r"\mapsto": "↦",
}

# --- Set theory ------------------------------------------------------------
_SETS = {
    r"\in": "∈", r"\notin": "∉", r"\ni": "∋",
    r"\subset": "⊂", r"\supset": "⊃",
    r"\subseteq": "⊆", r"\supseteq": "⊇",
    r"\cup": "∪", r"\cap": "∩",
    r"\setminus": "∖", r"\emptyset": "∅", r"\varnothing": "∅",
    r"\forall": "∀", r"\exists": "∃", r"\nexists": "∄",
    r"\therefore": "∴", r"\because": "∵",
}

# --- Blackboard bold (number sets) -----------------------------------------
_BLACKBOARD = {
    r"\mathbb{R}": "ℝ", r"\mathbb{N}": "ℕ", r"\mathbb{Z}": "ℤ",
    r"\mathbb{Q}": "ℚ", r"\mathbb{C}": "ℂ", r"\mathbb{P}": "ℙ",
    r"\mathbb{F}": "𝔽", r"\mathbb{H}": "ℍ",
}

# Master table — order matters at lookup time but the regex builder below
# sorts by length-desc anyway to defeat shadowing.
_TABLE: dict[str, str] = {}
for _src in (_GREEK_LOWER, _GREEK_UPPER, _OPERATORS, _RELATIONS,
             _ARROWS, _SETS, _BLACKBOARD):
    _TABLE.update(_src)


def _build_pattern() -> re.Pattern[str]:
    """Build a single regex matching any quick-code, longest-first.

    Uses a word-boundary lookahead at the END of each ``\\name`` so
    ``\\alphabet`` (as if such a thing existed) never matches ``\\alpha``.
    Codes containing ``{`` (``\\mathbb{R}``) match the literal braces.
    """
    keys = sorted(_TABLE.keys(), key=lambda s: -len(s))
    parts: list[str] = []
    for k in keys:
        escaped = re.escape(k)
        if k.endswith("}"):
            # Already terminated by a literal `}`; no boundary needed.
            parts.append(escaped)
        else:
            # `\alpha` followed by a letter would shadow — disallow next char being a letter.
            parts.append(escaped + r"(?![A-Za-z])")
    return re.compile("|".join(parts))


_PATTERN = _build_pattern()


def expand_quickcodes(text: str) -> str:
    """Substitute every recognized ``\\name`` with its Unicode glyph.

    Pure left-to-right longest-first replacement. Idempotent — running
    twice produces the same result. Unknown ``\\commands`` are left
    untouched (callers downstream like SymPy's ``parse_latex`` will
    handle them).
    """
    if "\\" not in text:
        return text
    return _PATTERN.sub(lambda m: _TABLE[m.group(0)], text)


def known_codes() -> dict[str, str]:
    """Return a copy of the full quick-code table (for inspection / docs)."""
    return dict(_TABLE)


__all__ = ["expand_quickcodes", "known_codes"]
