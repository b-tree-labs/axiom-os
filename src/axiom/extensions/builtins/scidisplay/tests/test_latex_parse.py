# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Checked LaTeX parsing (ADR-167 D2): a missing parser is reported, never silently skipped.

The corpus needs the ``math`` extra. Locally the corpus skips without it; **on CI it fails**, because
a skipped corpus is the exact silent absence this module exists to prevent.
"""

from __future__ import annotations

import os

import pytest
import sympy

from axiom.extensions.builtins.scidisplay import latex_parse as lp
from axiom.extensions.builtins.scidisplay import math_render

x, k, t, y, n, i = sympy.symbols("x k t y n i")


@pytest.fixture(autouse=True)
def _fresh_probe():
    lp._probe.cache_clear()
    lp._warned = False
    yield
    # A test may have replaced _probe with a plain function (monkeypatch is undone after this runs).
    getattr(lp._probe, "cache_clear", lambda: None)()
    lp._warned = False


def _require_backend():
    if lp.backend_available():
        return
    message = f"the LaTeX parser backend is not installed ({lp.INSTALL_HINT})"
    if os.environ.get("CI"):
        pytest.fail(message)
    pytest.skip(message)


# (latex, expected sympy expression) with the backend installed.
CORPUS = [
    (r"\frac{k-1}{k}", (k - 1) / k),
    (r"1 - \frac{1}{k}", 1 - 1 / k),
    (r"x^2 - 1", x**2 - 1),
    (r"\sqrt{x^2+1}", sympy.sqrt(x**2 + 1)),
    (r"\sin^2 x + \cos^2 x", sympy.sin(x) ** 2 + sympy.cos(x) ** 2),
    (r"\frac{dy}{dx} = -\lambda y", sympy.Eq(sympy.Derivative(y, x), -sympy.Symbol("lambda") * y)),
    (r"\sum_{i=1}^{n} i", sympy.Sum(i, (i, 1, n))),
    (r"\int_0^1 x^2 dx", sympy.Integral(x**2, (x, 0, 1))),
    (r"\alpha + \beta", sympy.Symbol("alpha") + sympy.Symbol("beta")),
    (r"2x + 3", 2 * x + 3),
    (r"\frac{x}{y}", x / y),
    (r"x^{2}", x**2),
    (r"\left(x + 1\right)^2", (x + 1) ** 2),
    (r"\ln x", sympy.log(x)),
    (r"|x|", sympy.Abs(x)),
]


@pytest.mark.parametrize("latex,expected", CORPUS, ids=[c[0] for c in CORPUS])
def test_the_corpus_parses_to_the_expected_expression(latex, expected):
    _require_backend()
    r = lp.parse_latex_checked(latex)
    assert r.status == "ok" and r.reason is None, (latex, r.reason)
    assert _same(r.expr, expected), (latex, r.expr, expected)


def _same(got, want) -> bool:
    """Structurally equal, or (for plain expressions) equal after simplification."""
    if got == want:
        return True
    try:
        return sympy.simplify(got - want) == 0
    except Exception:  # noqa: BLE001 - relations and unevaluated sums are compared structurally only
        return False


# What a SymbolTable must fix (ADR-167 D3). Marked strict so they flip to passing the day it lands.
KNOWN_LIMITATIONS = [
    (r"N_0 e^{-\lambda t}", "euler's constant parses as a plain symbol"),
    (r"\rho_{\text{eff}}", "a multi-letter text subscript parses as a product of letters"),
]


@pytest.mark.parametrize("latex,why", KNOWN_LIMITATIONS, ids=[c[0] for c in KNOWN_LIMITATIONS])
@pytest.mark.xfail(strict=True, reason="needs the caller-supplied SymbolTable (ADR-167 D3)")
def test_notation_that_is_not_mathematics_is_a_known_limit(latex, why):
    _require_backend()
    r = lp.parse_latex_checked(latex)
    assert r.ok
    names = {str(s) for s in r.expr.free_symbols}
    assert "text" not in "".join(names) and "e" not in names, why


def test_an_unknown_command_is_unparseable_not_a_variable_of_that_name():
    _require_backend()
    r = lp.parse_latex_checked(r"\notarealmacro{} + x")
    assert r.status == "unparseable" and "notarealmacro" in r.reason
    # a Greek letter is a legitimate symbol and must still parse
    assert lp.parse_latex_checked(r"\lambda t").ok


def test_text_that_is_not_latex_is_unparseable_not_unavailable():
    _require_backend()
    r = lp.parse_latex_checked(r"\frac{1}{")
    assert r.status == "unparseable" and r.expr is None and r.reason


# ---- the negative control: the missing backend must be visible --------------------------------


def test_a_missing_backend_is_unavailable_with_an_install_hint(monkeypatch):
    monkeypatch.setattr(lp, "_probe", lambda: (False, "No module named 'antlr4'"))
    r = lp.parse_latex_checked(r"\frac{k-1}{k}")
    assert r.status == "unavailable" and r.expr is None
    assert "antlr4" in r.reason and "axiom-os-lm[math]" in r.reason


def test_the_old_silent_none_still_comes_back_but_says_why_once(monkeypatch, caplog):
    monkeypatch.setattr(lp, "_probe", lambda: (False, "No module named 'antlr4'"))
    with caplog.at_level("WARNING"):
        assert math_render.parse_to_sympy(r"x^2") is None
        assert math_render.parse_to_sympy(r"y^2") is None
    warnings = [r for r in caplog.records if "unavailable" in r.getMessage()]
    assert len(warnings) == 1, "one diagnostic per process, not one per equation"
    assert "axiom-os-lm[math]" in warnings[0].getMessage()


def test_an_available_backend_is_not_warned_about(caplog):
    _require_backend()
    with caplog.at_level("WARNING"):
        math_render.parse_to_sympy(r"x^2")
    assert not [r for r in caplog.records if "unavailable" in r.getMessage()]


def test_the_probe_runs_the_parser_not_just_an_import(monkeypatch):
    """A runtime of the wrong version imports fine and fails on first use; the probe must call it."""
    import sympy.parsing.latex as slp

    def boom(text, backend=None):
        raise RuntimeError("generated parser/runtime version mismatch")

    monkeypatch.setattr(slp, "parse_latex", boom)
    lp._probe.cache_clear()
    ok, why = lp._probe()
    assert not ok and "mismatch" in why
