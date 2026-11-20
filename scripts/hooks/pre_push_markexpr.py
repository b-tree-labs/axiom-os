#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Print the pre-push hook's marker expression: CI's unit selection without ``llm`` (#275).

``pytest -m`` on the command line replaces the ``-m`` in pyproject's addopts
instead of adding to it, so the hook cannot pass ``-m "not llm"`` alone: that
would also run integration, install_path and the other opt-in categories CI's
unit job never runs. This reads the addopts expression and appends
``and not llm``, so the two selections cannot drift apart.

Prints nothing and exits 1 when pyproject has no addopts ``-m``; the hook then
runs the default selection and says so.
"""

from __future__ import annotations

import shlex
import sys
import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def markexpr(pyproject: Path = PYPROJECT) -> str:
    """``(<addopts -m expression>) and not llm``, or "" when addopts has no ``-m``."""
    ini = tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]
    args = shlex.split(ini.get("addopts", ""))
    if "-m" not in args or args.index("-m") + 1 >= len(args):
        return ""
    return f"({args[args.index('-m') + 1]}) and not llm"


def main() -> int:
    expr = markexpr()
    if not expr:
        return 1
    print(expr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
