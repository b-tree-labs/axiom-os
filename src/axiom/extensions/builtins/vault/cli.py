# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi vault`` CLI noun for KEEP.

Per ADR-056: CLI verbs are thin wrappers over skill functions. The
vault subcommands (list / issue / revoke / audit) ship as
skill-backed verbs in a follow-up PR; this file is the discovery
shim that satisfies the AEOS manifest declaration.
"""

from __future__ import annotations

import argparse


def cli() -> int:
    """Vault subcommand entrypoint (Phase 1 stub)."""
    parser = argparse.ArgumentParser(
        prog="axi vault",
        description="KEEP vault — capability lifecycle + audit.",
    )
    parser.add_argument(
        "subcommand",
        nargs="?",
        choices=["list", "issue", "revoke", "audit"],
    )
    args = parser.parse_args()
    print(
        f"axi vault {args.subcommand or '(no subcommand)'}: not yet "
        "implemented — see prd-axiom-vault §5.2."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
