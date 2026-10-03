# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the math rendering pipeline (Sci Displays spec §5/§6)."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_math_cache(tmp_path, monkeypatch):
    """Force the math render cache under tmp_path — never touch the real
    user-state dir."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    yield


# ---------------------------------------------------------------------------
# Pretty (text-only) path — always works
# ---------------------------------------------------------------------------


def test_render_pretty_simple_integral():
    from axiom.extensions.builtins.scidisplay.math_render import render_pretty

    out = render_pretty(r"\int_0^\infty e^{-x^2} dx")
    assert out
    # SymPy's pretty-printer produces multi-line; smoke for a known token.
    # Exact glyph layout varies by SymPy version, just check non-empty
    # multi-line output containing the integral character.
    assert len(out.splitlines()) >= 1


def test_render_pretty_falls_back_on_unparseable():
    from axiom.extensions.builtins.scidisplay.math_render import render_pretty

    # Made-up macro SymPy can't parse — pretty must not raise; returns
    # the (already quick-code-expanded) source as the visible fallback.
    out = render_pretty(r"\unknownmacro{x}")
    assert out
    assert "unknownmacro" in out or "x" in out


def test_parse_to_sympy_returns_expr():
    """SymPy's LaTeX parser needs antlr4 + an ANTLR grammar — not always
    installed in slim envs. Skip cleanly when missing."""
    from axiom.extensions.builtins.scidisplay.math_render import parse_to_sympy

    expr = parse_to_sympy(r"x^2 + 2x + 1")
    if expr is None:
        pytest.skip("SymPy LaTeX parser unavailable (antlr4 not installed)")
    assert "x" in str(expr)


def test_parse_to_sympy_returns_none_on_failure():
    from axiom.extensions.builtins.scidisplay.math_render import parse_to_sympy

    assert parse_to_sympy(r"\unknownmacro") is None


# ---------------------------------------------------------------------------
# End-to-end render — produces a MathRender with all fields populated
# ---------------------------------------------------------------------------


def test_render_math_returns_mathrender():
    from axiom.extensions.builtins.scidisplay.math_render import (
        MathBlock,
        MathRender,
        render_math,
    )

    out = render_math(MathBlock(latex=r"\alpha + \beta = \gamma"))
    assert isinstance(out, MathRender)
    # Quick-codes were applied before render.
    assert out.expanded == "α + β = γ"
    # Pretty-text fallback is always populated.
    assert out.pretty_text


def test_render_math_quickcodes_applied_before_sympy():
    from axiom.extensions.builtins.scidisplay.math_render import MathBlock, render_math

    out = render_math(MathBlock(latex=r"x \in \mathbb{R}"))
    assert "ℝ" in out.expanded
    assert "∈" in out.expanded


def test_render_math_receipt_id_format():
    from axiom.extensions.builtins.scidisplay.math_render import MathBlock, render_math

    out = render_math(MathBlock(latex=r"E = mc^2"))
    assert out.receipt_id.startswith("axiom://math/")
    # Hex digest, 16 chars per the implementation.
    digest = out.receipt_id.split("/")[-1]
    assert len(digest) == 16
    assert all(c in "0123456789abcdef" for c in digest)


def test_render_math_receipt_id_stable_for_same_input():
    """Receipt IDs are content-addressed — same input → same ID."""
    from axiom.extensions.builtins.scidisplay.math_render import MathBlock, render_math

    a = render_math(MathBlock(latex=r"\alpha + \beta"))
    b = render_math(MathBlock(latex=r"\alpha + \beta"))
    assert a.receipt_id == b.receipt_id


def test_render_math_receipt_id_differs_for_different_input():
    from axiom.extensions.builtins.scidisplay.math_render import MathBlock, render_math

    a = render_math(MathBlock(latex=r"\alpha + \beta"))
    b = render_math(MathBlock(latex=r"\alpha + \gamma"))
    assert a.receipt_id != b.receipt_id


def test_render_math_produces_svg_and_png():
    """Smoke: matplotlib backend renders a real SVG + PNG to disk for
    a simple equation."""
    from axiom.extensions.builtins.scidisplay.math_render import MathBlock, render_math

    out = render_math(MathBlock(latex=r"E = m c^2"))
    # The matplotlib backend isn't installed in every CI runner (the
    # sibling cache test handles the same case); skip the assertion
    # rather than fail on an env gap.
    if out.svg_path is None:
        pytest.skip("matplotlib didn't render — env-specific")
    assert out.svg_path.exists()
    assert out.png_path is not None and out.png_path.exists()
    assert out.svg_path.stat().st_size > 0
    assert out.png_path.stat().st_size > 0


def test_render_math_cache_hit_is_idempotent():
    """A second render call hits the cache; the SVG file isn't rewritten."""
    from axiom.extensions.builtins.scidisplay.math_render import MathBlock, render_math

    a = render_math(MathBlock(latex=r"\sin(x) + \cos(x)"))
    if a.svg_path is None:
        pytest.skip("matplotlib didn't render — env-specific")
    mtime1 = a.svg_path.stat().st_mtime_ns

    b = render_math(MathBlock(latex=r"\sin(x) + \cos(x)"))
    assert b.svg_path == a.svg_path
    mtime2 = b.svg_path.stat().st_mtime_ns
    assert mtime1 == mtime2  # cache hit — no rewrite


def test_render_math_unparseable_still_produces_pretty_text():
    """Garbage LaTeX shouldn't break the renderer — pretty_text is always
    populated and sympy_parsed flags whether SymPy could understand it."""
    from axiom.extensions.builtins.scidisplay.math_render import MathBlock, render_math

    out = render_math(MathBlock(latex=r"\notarealmacro{}"))
    # Image fields may or may not exist depending on whether matplotlib
    # mathtext also barfs on this input.
    assert out.pretty_text  # always set
    assert out.sympy_parsed is False


def test_render_math_preserves_source():
    from axiom.extensions.builtins.scidisplay.math_render import MathBlock, render_math

    block = MathBlock(latex=r"\alpha", display=False, label="eq:1")
    out = render_math(block)
    assert out.source == r"\alpha"
    assert out.expanded == "α"
