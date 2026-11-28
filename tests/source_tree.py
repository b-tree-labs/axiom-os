# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The source tree, read and parsed once per test process.

A dozen guards each walk ``src/axiom`` and parse every module (about 4,700 files):
the same work repeated per guard, 20 to 50 seconds each on a loaded runner. They
share these helpers instead, so the tree is listed, read and parsed once for the
whole session (and once per xdist worker). A guard keeps its own judgement; only
the reading is shared.

The trees are shared, so a guard must only READ them (``ast.walk`` and friends):
never set attributes on a node and never transform one.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path


@cache
def py_files(root: Path) -> tuple[Path, ...]:
    """Every ``*.py`` under ``root``, sorted, as ``sorted(root.rglob("*.py"))`` gives."""
    return tuple(sorted(root.rglob("*.py")))


@cache
def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@cache
def tree(path: Path) -> ast.Module:
    """The parsed module. Raises ``SyntaxError`` exactly as ``ast.parse`` would."""
    return ast.parse(text(path))
