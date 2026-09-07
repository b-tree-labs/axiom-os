# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for ``axi doctor`` — AI-powered diagnostics.

Usage:
    axi doctor                  Run environment diagnostics
    axi doctor "error message"  Diagnose a specific error
    axi doctor --error "msg"    Same, explicit flag
    axi doctor --watch          Watch mode (not yet implemented)
"""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for ``axi doctor``."""
    parser = argparse.ArgumentParser(
        prog="axi doctor",
        description="AI-powered environment diagnostics for Neutron OS.",
    )
    parser.add_argument(
        "error_context",
        nargs="?",
        default=None,
        help="Optional error message or context to diagnose.",
    )
    parser.add_argument(
        "-e", "--error",
        dest="error_flag",
        default=None,
        help="Error message to diagnose (alternative to positional arg).",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        default=False,
        help="Watch mode — continuously monitor for errors.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point — delegates to the CLI dispatcher's cmd_doctor."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve error context: --error flag takes precedence over positional
    error_context = args.error_flag or args.error_context

    # Import here to avoid circular imports at module level
    from axiom.axiom_cli import cmd_doctor

    return cmd_doctor(error_context)
