# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi approve`` — answer what an agent is waiting on.

A thin wrapper over the ``approval.*`` skills per ADR-056: no logic lives in
the argparse handlers, so the terminal, ``neut chat`` and an MCP client all run
the same function rather than three that agree for now.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from axiom.infra.orchestrator import skills as approval_skills
from axiom.infra.skills import SkillContext, SkillRegistry


def _brand_cli() -> str:
    """The command the operator actually typed.

    Hardcoding "axi" tells a consumer distribution's operator to run a command
    that does not exist on their machine. The CLI name follows branding; only
    the environment variable is a fixed literal, so a runbook can name it.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 — a label never takes the command down
        return "axi"


def _state_dir() -> Path:
    """The same root every other verb uses, so the queue is one queue."""
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir()


def _context() -> SkillContext:
    registry = SkillRegistry()
    approval_skills.register_all(registry)
    return SkillContext(
        registry=registry,
        state_dir=_state_dir(),
        logger=logging.getLogger("axi.approve"),
        user_prompt=input if sys.stdin.isatty() else None,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=f"{_brand_cli()} approve", description=__doc__)
    sub = p.add_subparsers(dest="action")
    sub.add_parser("list", help="Show actions waiting on a human.")
    ok = sub.add_parser("ok", help="Approve one action.")
    ok.add_argument("action_id")
    no = sub.add_parser("no", help="Reject one action.")
    no.add_argument("action_id")
    no.add_argument("--reason", default="")
    return p


def _print_pending(value: dict) -> None:
    rows = value["pending"]
    if not rows:
        print("Nothing is waiting on you.")
        return
    print(f"{len(rows)} waiting:\n")
    for r in rows:
        params = ", ".join(f"{k}={v!r}" for k, v in sorted(r["params"].items()))
        print(f"  {r['action_id']}  {r['name']}({params})")
        print(f"  {'':12}  proposed {r['created_at']}")
    cli = _brand_cli()
    print(f"\n  {cli} approve ok <id>      {cli} approve no <id> --reason '...'")


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["list"]
    if argv[0] in ("-h", "--help"):
        build_parser().print_help()
        return 0

    args = build_parser().parse_args(argv)
    ctx = _context()

    if args.action == "list":
        result = approval_skills.pending({}, ctx)
        if result.ok:
            _print_pending(result.value)
    elif args.action == "ok":
        result = approval_skills.approve({"action_id": args.action_id}, ctx)
        if result.ok:
            print(f"Approved {args.action_id} as {result.value['decided_by']}.")
    elif args.action == "no":
        result = approval_skills.reject(
            {"action_id": args.action_id, "reason": args.reason}, ctx
        )
        if result.ok:
            print(f"Rejected {args.action_id} as {result.value['decided_by']}.")
    else:
        build_parser().print_help()
        return 0

    for err in result.errors:
        print(err, file=sys.stderr)
    return result.exit_code
