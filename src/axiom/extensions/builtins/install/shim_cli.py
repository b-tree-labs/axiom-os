# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axi install-shim — drop a stable ~/.local/bin/axi shim.

Usage:
    axi install-shim             write/refresh the shim idempotently
    axi install-shim --force     overwrite even if another target is installed
    axi install-shim --target /path/to/venv/bin/axi   explicit target
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .shim import (
    path_contains_local_bin,
    resolve_current_axi,
    write_shim,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="axi install-shim",
        description=(
            "Install a stable ~/.local/bin/axi shim so non-interactive SSH "
            "sessions (federation peers) can locate axi without walking the "
            "filesystem."
        ),
    )
    p.add_argument(
        "--target",
        metavar="PATH",
        help="Explicit path to the venv-installed axi binary "
        "(default: auto-detect from sys.argv[0] or $PATH).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing shim even if it points at a different target.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.target:
        target = Path(args.target).resolve()
        if not target.is_file():
            print(f"  error: --target {target} does not exist", file=sys.stderr)
            return 1
    else:
        detected = resolve_current_axi()
        if detected is None:
            print(
                "  error: could not locate the current axi binary. "
                "Pass --target /path/to/.venv/bin/axi explicitly.",
                file=sys.stderr,
            )
            return 1
        target = detected

    result = write_shim(target_axi=target, force=args.force)

    if result.conflict and not args.force:
        print()
        print("  WARNING: ~/.local/bin/axi already points at a different venv:")
        print(f"    existing target: {result.previous_target}")
        print(f"    requested target: {target}")
        print()
        print("  Multiple axi installs are competing for the same shim.")
        print("  Decide which install should serve remote peers, then re-run:")
        print("    axi install-shim --force    # from the winning venv")
        return 2

    if result.written:
        print(f"  wrote shim: {result.path} -> {target}")
    else:
        print(f"  shim up to date: {result.path} -> {target}")

    # PATH guidance — non-fatal.
    local_bin = result.path.parent
    if not path_contains_local_bin(local_bin):
        print()
        print(f"  note: {local_bin} is not on your PATH.")
        print("  Add it so remote peers (and you) can invoke `axi` directly:")
        print("    echo 'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc")
        print("  (or ~/.zshrc, depending on your shell)")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
