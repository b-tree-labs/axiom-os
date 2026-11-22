# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The pre-push run leaves model-calling tests to CI (#275).

A test marked ``llm`` waits on a model, and the push gate is the dev loop: a
gate that spends a minute per model call is the gate people skip with
``--no-verify``. CI's unit job runs them, because it passes no ``-m`` and
the default selection does not exclude them.

``-m`` on the command line REPLACES the ``-m`` in pyproject's addopts rather
than adding to it, so the hook cannot just pass ``-m "not llm"``: that would
run integration, install_path and the rest, which CI's unit job never runs.
The expression is the addopts one with ``and not llm`` appended, read from
pyproject so the two cannot drift.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "hooks" / "pre_push_markexpr.py"


def _addopts_expr() -> str:
    addopts = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["addopts"]
    args = shlex.split(addopts)
    return args[args.index("-m") + 1]


def test_the_llm_marker_is_registered():
    markers = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["markers"]
    assert any(m.startswith("llm:") for m in markers)


def test_the_default_selection_still_runs_llm_tests():
    """CI's unit job passes no -m, so the llm tests run there."""
    assert "llm" not in _addopts_expr().replace("live_llm", "")


def test_the_hook_expression_is_the_default_one_without_llm():
    out = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True, cwd=REPO
    ).stdout.strip()
    assert out == f"({_addopts_expr()}) and not llm"


@pytest.mark.parametrize(
    ("markers", "selected"),
    [((), True), (("llm",), False), (("integration",), False), (("llm", "slow"), False), (("slow",), True)],
)
def test_the_expression_selects_what_ci_unit_runs_minus_llm(markers, selected):
    from _pytest.mark.expression import Expression

    out = subprocess.run(
        [sys.executable, str(SCRIPT)], capture_output=True, text=True, check=True, cwd=REPO
    ).stdout.strip()
    assert Expression.compile(out).evaluate(lambda name, **kw: name in markers) is selected
