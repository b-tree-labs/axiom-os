# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi vault`` CLI noun for KEEP.

Per ADR-056: CLI verbs are thin wrappers over skill functions.
``sweep`` is skill-backed and live. list / issue / revoke / audit remain the
Phase-1 stubs from the manifest declaration and still say so when invoked —
a verb that prints "not yet implemented" is honest; one that silently returns 0
is how a heartbeat comes to report success for work nobody did.
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
        choices=["list", "issue", "revoke", "audit", "sweep", "renew", "steward"],
    )
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--env-root", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--horizon-days", type=int, default=None,
        help="Treat credentials expiring within N days as due (default 3).",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would rotate without touching any credential.",
    )
    args = parser.parse_args()

    if args.subcommand == "sweep":
        return _sweep(args)
    if args.subcommand == "renew":
        return _renew(args)
    if args.subcommand == "steward":
        return _steward(args)

    print(
        f"axi vault {args.subcommand or '(no subcommand)'}: not yet "
        "implemented — see prd-axiom-vault §5.2."
    )
    return 0


def _sweep(args) -> int:
    """KEEP's stewardship pass — see skills/sweep.py for the KEEP/secrets split."""
    import json as _json

    from axiom.extensions.builtins.vault.skills import sweep as sweep_skill

    params = {k: v for k, v in (("workspace", args.workspace),
                                ("env_root", args.env_root)) if v}
    result = sweep_skill.run(params, None)
    if args.json:
        print(_json.dumps({"ok": result.ok, "value": result.value,
                           "errors": result.errors}, indent=2))
    else:
        for action in result.actions_taken:
            print(f"  {action}")
        for err in result.errors:
            print(f"  {err}")
        print("KEEP sweep: clean" if result.ok
              else "KEEP sweep: UNMANAGED CREDENTIALS FOUND")
    return 0 if result.ok else 1


def _renew(args) -> int:
    """KEEP's expiry-rotation pass — see skills/renew.py for the policy."""
    import json as _json

    from axiom.extensions.builtins.vault.skills import renew as renew_skill

    params: dict = {"dry_run": bool(args.dry_run)}
    if args.horizon_days is not None:
        params["horizon_days"] = args.horizon_days
    result = renew_skill.run(params, None)
    if args.json:
        print(_json.dumps({"ok": result.ok, "value": result.value,
                           "errors": result.errors}, indent=2))
    else:
        for action in result.actions_taken:
            print(f"  {action}")
        for err in result.errors:
            print(f"  {err}")
        print("KEEP renew: nothing needs a human" if result.ok
              else "KEEP renew: CREDENTIALS NEED ATTENTION")
    return 0 if result.ok else 1


def _steward(args) -> int:
    """KEEP's full stewardship pass: sweep + renew.

    This is the heartbeat surface. Both halves always run — an unmanaged
    credential and an expiring one are independent failures, and stopping at
    the first would hide the second.
    """
    sweep_rc = _sweep(args)
    renew_rc = _renew(args)
    return 0 if (sweep_rc == 0 and renew_rc == 0) else 1


if __name__ == "__main__":
    raise SystemExit(cli())
