# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext test`` — thin pytest wrapper scoped to the extension.

The file is named ``test_verb.py`` (not ``test.py``) because pytest would
otherwise collect this module as a test module itself.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from axiom.cli.ext.provider import CliContext


def run_pytest(ext_path: Path, extra_args: list[str] | None = None) -> int:
    """Invoke pytest against ``ext_path/tests`` with sensible defaults.

    ``axiom-tests`` registers via the ``pytest11`` entry point so fixtures are
    auto-discovered; no explicit ``-p axiom_tests.plugin`` is required.

    The subprocess runs with ``cwd=ext_path`` so relative paths and
    extension-local ``conftest.py`` / ``pytest.ini`` files resolve correctly
    regardless of where ``axi ext test`` was invoked from.
    """
    extra_args = list(extra_args or [])
    tests_dir = ext_path / "tests"
    if not tests_dir.exists():
        print(f"axi ext test: no tests/ directory under {ext_path}")
        return 1

    # If the caller did not supply any positional pytest target, default to
    # the extension's tests/ directory. Detecting this precisely is awkward
    # because pytest flags intermingle with positional targets; we treat the
    # first non-flag token as a target hint and fall back when absent.
    has_target = any(not arg.startswith("-") for arg in extra_args)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--rootdir",
        str(ext_path),
        "-p",
        "no:cacheprovider",
        *extra_args,
    ]
    if not has_target:
        cmd.append(str(tests_dir))

    return subprocess.run(cmd, cwd=str(ext_path)).returncode


class TestProvider:
    """Built-in provider for ``axi ext test [<path>] [-- <pytest-args>...]``."""

    verb = "test"
    description = "Run the extension's tests via the axiom-tests pytest plugin"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument(
            "pytest_args",
            nargs=argparse.REMAINDER,
            help="Extra arguments forwarded to pytest (prefix with ``--``)",
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        # A leading ``--`` may land in either ``args.path`` (when it's the
        # only positional token) or at the head of ``pytest_args`` (when a
        # real path was supplied first). Normalise both cases.
        raw_path = args.path
        extra = list(args.pytest_args or [])
        if raw_path == "--":
            raw_path = None
        if extra and extra[0] == "--":
            extra = extra[1:]

        target = Path(raw_path).resolve() if raw_path else context.cwd
        return run_pytest(target, extra)


__all__ = ["TestProvider", "run_pytest"]
