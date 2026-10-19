# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Checked LaTeX parsing: a parse that says WHY it did not (ADR-167 D2).

SymPy's LaTeX parser has an ANTLR backend that needs a runtime SymPy does not install. Without it
``parse_latex`` raises, the old ``parse_to_sympy`` swallowed the error and returned ``None``, and
every equation silently skipped verification. Measured on six representative equations: 0 of 6 parsed
in the default environment, 5 of 6 with the backend and the pinned runtime. This module returns a
three-valued result so "the parser is not installed" is never confused with "that is not LaTeX":

- ``ok``: parsed;
- ``unavailable``: the backend is missing; ``reason`` carries the install hint;
- ``unparseable``: the backend ran and rejected the text.

The pinned runtime is the ``math`` extra (``pip install 'axiom-os-lm[math]'``).
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import Any, Literal

from .compute_worker import (
    unknown_macros as _unknown_macros,  # one rule, shared with the isolated worker
)

log = logging.getLogger(__name__)

BACKEND = "antlr"
INSTALL_HINT = "pip install 'axiom-os-lm[math]'  # sympy and antlr4-python3-runtime==4.11.*"

ParseStatus = Literal["ok", "unavailable", "unparseable"]


@dataclass(frozen=True)
class ParseResult:
    expr: Any | None
    status: ParseStatus
    reason: str | None
    backend: str = BACKEND

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@functools.lru_cache(maxsize=1)
def _probe() -> tuple[bool, str]:
    """Whether the ANTLR backend can actually parse (not merely whether a package is present: a
    runtime of the wrong version imports fine and fails on first use)."""
    try:
        from sympy.parsing.latex import parse_latex

        parse_latex("x", backend=BACKEND)
        return True, ""
    except ImportError as exc:
        return False, f"{exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def backend_available() -> bool:
    return _probe()[0]


_warned = False


def warn_once_if_unavailable() -> None:
    """One diagnostic per process, so a missing backend is visible without flooding a log."""
    global _warned
    ok, why = _probe()
    if not ok and not _warned:
        _warned = True
        log.warning(
            "LaTeX parsing is unavailable, so equations will not be verified (%s). Install: %s",
            why,
            INSTALL_HINT,
        )


def parse_latex_checked(text: str) -> ParseResult:
    """Parse LaTeX to a SymPy expression, reporting why on failure. Never raises."""
    ok, why = _probe()
    if not ok:
        warn_once_if_unavailable()
        return ParseResult(None, "unavailable", f"{why}. Install: {INSTALL_HINT}")
    try:
        from sympy.parsing.latex import parse_latex

        expr = parse_latex(text, backend=BACKEND)
        unknown = _unknown_macros(text, expr)
        if unknown:
            return ParseResult(
                None, "unparseable", "unknown command(s): " + ", ".join("\\" + m for m in unknown)
            )
        return ParseResult(expr, "ok", None)
    except ImportError as exc:  # the backend vanished after the probe
        return ParseResult(None, "unavailable", f"{exc}. Install: {INSTALL_HINT}")
    except Exception as exc:  # noqa: BLE001
        return ParseResult(None, "unparseable", f"{type(exc).__name__}: {str(exc)[:200]}")
