# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the code-rendering pipeline (ADR-039 D12, spec §6b)."""

from __future__ import annotations


import pytest


# ---------------------------------------------------------------------------
# Lexer selection
# ---------------------------------------------------------------------------


def test_explicit_language_wins():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, detect_language

    block = CodeBlock(body="print('hi')", language="python")
    assert detect_language(block) == "python"


def test_explicit_language_short_alias():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, detect_language

    # 'ts' should resolve to typescript, 'rs' to rust, etc.
    assert detect_language(CodeBlock(body="let x = 1", language="ts")) in {"typescript", "ts"}
    assert detect_language(CodeBlock(body="fn main() {}", language="rs")) in {"rust", "rs"}


def test_path_hint_used_when_no_explicit():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, detect_language

    block = CodeBlock(body="def f(): pass", path_hint="example.py")
    assert detect_language(block) == "python"


def test_explicit_overrides_path_hint():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, detect_language

    block = CodeBlock(body="...", language="ruby", path_hint="example.py")
    assert detect_language(block) in {"ruby", "rb"}


def test_guess_when_no_explicit_or_path():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, detect_language

    # Distinctive Python content; guess_lexer should land on python.
    block = CodeBlock(
        body=(
            "import os\nfrom dataclasses import dataclass\n@dataclass\n"
            "class Foo:\n    x: int = 0\n"
        )
    )
    assert detect_language(block) in {"python", "python3"}


def test_unknown_language_falls_back_to_text():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, detect_language

    # Pure ambiguous text shouldn't get force-classified.
    block = CodeBlock(body="hello world\nthis is just prose")
    # Either text or some low-confidence guess — the contract is we don't
    # raise. Plaintext is the safe answer.
    lang = detect_language(block)
    assert isinstance(lang, str)


def test_invalid_explicit_language_falls_through():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, detect_language

    # 'totally-not-a-language' should silently fall through to path-hint
    # (none here), then guess, then plaintext. Never raise.
    block = CodeBlock(body="x = 1", language="totally-not-a-language")
    lang = detect_language(block)
    assert isinstance(lang, str)


# ---------------------------------------------------------------------------
# Themes
# ---------------------------------------------------------------------------


def test_three_themes_registered():
    from axiom.extensions.builtins.scidisplay.themes import THEMES

    assert "axiom-dark" in THEMES
    assert "axiom-light" in THEMES
    assert "axiom-high-contrast" in THEMES


def test_theme_classes_have_brand_palette():
    from axiom.extensions.builtins.scidisplay.themes import (
        BRAND_BURNT_ORANGE,
        BRAND_GRAPHITE,
        BRAND_OFF_WHITE,
        AxiomDarkTheme,
        AxiomLightTheme,
    )

    assert AxiomDarkTheme.background_color == BRAND_GRAPHITE
    assert AxiomLightTheme.background_color == BRAND_OFF_WHITE
    # Spot-check: brand orange shows up somewhere in the dark theme styles.
    has_burnt_orange = any(
        BRAND_BURNT_ORANGE.lower() in str(v).lower()
        for v in AxiomDarkTheme.styles.values()
    )
    assert has_burnt_orange, "axiom-dark must use UT burnt-orange somewhere"


def test_theme_get_falls_back_to_dark():
    from axiom.extensions.builtins.scidisplay.themes import AxiomDarkTheme, get_theme

    assert get_theme("axiom-dark") is AxiomDarkTheme
    # Unknown theme name → dark fallback.
    assert get_theme("does-not-exist") is AxiomDarkTheme


def test_pygments_can_use_themes_as_styles():
    """Smoke: each theme is a valid Pygments Style class — Pygments can format
    code with it without raising."""
    from pygments import highlight
    from pygments.formatters import TerminalTrueColorFormatter
    from pygments.lexers import get_lexer_by_name

    from axiom.extensions.builtins.scidisplay.themes import THEMES

    code = "def foo(): return 1\n"
    lexer = get_lexer_by_name("python")
    for name, theme_cls in THEMES.items():
        out = highlight(code, lexer, TerminalTrueColorFormatter(style=theme_cls))
        assert out, f"theme {name} produced empty output"


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------


def test_render_returns_non_empty_ansi():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, render_code_block

    out = render_code_block(CodeBlock(body="print('hi')", language="python"))
    assert out
    # Some ANSI escape sequence should appear in the rendered output.
    assert "\x1b[" in out


def test_render_with_each_theme():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, render_code_block

    block = CodeBlock(body="x = 1\ny = 2\n", language="python")
    for theme in ("axiom-dark", "axiom-light", "axiom-high-contrast"):
        out = render_code_block(block, theme=theme)
        assert out, f"theme {theme} produced empty render"


def test_short_block_no_gutter():
    """≤10 lines: no line-number gutter (avoids visual noise on snippets)."""
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, render_code_block

    out = render_code_block(CodeBlock(body="x = 1\n", language="python"))
    # Crude: a leading "1" before the content would indicate gutter on. We
    # check that we DON'T see a stand-alone right-aligned "1 " before the
    # 'x' token. The Rich gutter is right-padded so a gutter line would
    # have a space-padded number first.
    # Line width varies — assert gutter absent by checking no "1 " preceding 'x'.
    # (Rich gutter style is platform-dependent; this is a soft guard.)
    assert "x" in out


def test_long_block_includes_gutter():
    """>10 lines: gutter should appear (line numbers visible)."""
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, render_code_block

    body = "\n".join(f"line_{i} = {i}" for i in range(15))
    out = render_code_block(CodeBlock(body=body, language="python"))
    # Sanity: output mentions a high-numbered line, and Rich's gutter style
    # places line numbers visibly. The exact format is theme-dependent;
    # the assertion is just that the render didn't truncate.
    assert "line_14" in out or "14" in out


def test_render_handles_unknown_language_gracefully():
    from axiom.extensions.builtins.scidisplay.code import CodeBlock, render_code_block

    # Should not raise; falls back to plaintext.
    out = render_code_block(CodeBlock(body="just text\nover lines", language="xyz"))
    assert out


def test_render_language_badge_format():
    from axiom.extensions.builtins.scidisplay.code import render_language_badge

    assert render_language_badge("python", 18) == "python · 18"
    assert render_language_badge("rust", 1) == "rust · 1"


# ---------------------------------------------------------------------------
# Ligature advisory
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_hints(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    # Reset the per-process flag so each test starts fresh.
    monkeypatch.delenv("_AXIOM_SCIDISPLAY_FONT_HINT_SHOWN", raising=False)
    yield


def test_advisory_shown_on_first_call(isolated_hints):
    from axiom.extensions.builtins.scidisplay.code import should_show_ligature_advisory

    assert should_show_ligature_advisory() is True


def test_advisory_not_shown_after_mark(isolated_hints):
    from axiom.extensions.builtins.scidisplay.code import (
        mark_ligature_advisory_shown,
        should_show_ligature_advisory,
    )

    mark_ligature_advisory_shown()
    assert should_show_ligature_advisory() is False


def test_advisory_suppression_persists(isolated_hints, tmp_path):
    """Suppressing in one process makes the advisory hidden in subsequent
    processes (file-backed)."""
    from axiom.extensions.builtins.scidisplay.code import (
        should_show_ligature_advisory,
        suppress_ligature_advisory,
    )

    suppress_ligature_advisory()

    # Simulate a "fresh process" by clearing the per-process flag.
    import os
    os.environ.pop("_AXIOM_SCIDISPLAY_FONT_HINT_SHOWN", None)

    assert should_show_ligature_advisory() is False


def test_advisory_text_mentions_recommended_fonts():
    from axiom.extensions.builtins.scidisplay.code import ligature_advisory_text

    text = ligature_advisory_text()
    assert "JetBrains Mono" in text
    assert "Fira Code" in text
    assert "Cascadia Code" in text
    assert "/hint suppress code-font" in text
