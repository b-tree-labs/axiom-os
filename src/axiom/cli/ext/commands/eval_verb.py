# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext eval`` — detect and run the extension's eval suite.

Detection order (first hit wins):

1. ``<ext>/evals/promptfooconfig.yaml`` — shell out to
   ``npx promptfoo eval -c <path>`` and mirror its exit code.
2. ``[extension.evals]`` with ``runner = "pytest"`` in the manifest —
   run ``pytest <ext>/evals/ -v`` via the installed interpreter.
3. Neither — print "no eval suite configured" and exit 0.

The file is named ``eval_verb.py`` (not ``eval.py``) to avoid shadowing
the Python builtin when the module is imported under its short name.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

from axiom.cli.ext._output import console
from axiom.cli.ext.provider import CliContext


def _detect_eval_runner(ext_path: Path) -> tuple[str, Path | None]:
    """Decide which eval runner (if any) applies to ``ext_path``.

    Returns ``(runner, path)`` where ``runner`` is one of:

    - ``"promptfoo"`` — ``path`` is the config YAML
    - ``"pytest"`` — ``path`` is the evals directory
    - ``"none"`` — ``path`` is None
    """
    # 1. promptfoo config under evals/
    pf_config = ext_path / "evals" / "promptfooconfig.yaml"
    if pf_config.exists():
        return "promptfoo", pf_config

    # 2. manifest-declared pytest runner
    manifest = ext_path / "axiom-extension.toml"
    if manifest.exists():
        try:
            with manifest.open("rb") as fh:
                data = tomllib.load(fh)
        except Exception:  # noqa: BLE001 — surfaced elsewhere by lint
            return "none", None
        evals_block = data.get("extension", {}).get("evals", {}) or {}
        runner = evals_block.get("runner")
        if runner == "pytest":
            evals_dir = ext_path / "evals"
            if evals_dir.exists():
                return "pytest", evals_dir

    return "none", None


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class EvalProvider:
    """Built-in provider for ``axi ext eval [<path>]``."""

    verb = "eval"
    description = "Detect and run the extension's eval suite (promptfoo or pytest)"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        target = Path(args.path).resolve() if args.path else context.cwd
        runner, rpath = _detect_eval_runner(target)
        con = console()

        if runner == "none":
            # Per spec: not a failure — policy is the caller's call.
            con.print(f"axi ext eval: {target.name}: no eval suite configured")
            return 0

        if runner == "promptfoo":
            cmd = ["npx", "promptfoo", "eval", "-c", str(rpath)]
            con.print(f"axi ext eval: running promptfoo ({rpath})")
            proc = subprocess.run(cmd, cwd=str(target))
            return proc.returncode

        if runner == "pytest":
            cmd = [sys.executable, "-m", "pytest", str(rpath), "-v"]
            con.print(f"axi ext eval: running pytest ({rpath})")
            proc = subprocess.run(cmd, cwd=str(target))
            return proc.returncode

        # Defensive — _detect_eval_runner should not return anything else.
        con.print(f"axi ext eval: unknown runner {runner!r}")
        return 1


__all__ = ["EvalProvider"]
