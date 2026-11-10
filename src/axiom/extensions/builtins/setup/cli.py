# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``setup``: one command that gets this computer ready, and says what is left.

A thin wrapper (ADR-056): it parses flags, reads an invitation code from a hidden
prompt when asked to, and calls ``setup.run``. The steps live in ``steps.py``.

    <cli> setup                         ask before each change
    <cli> setup --node https://node.example.org
    <cli> setup --yes                   no questions (unattended)
    <cli> setup --dry-run               say what would change, change nothing
    <cli> setup --invitation            sign in with an invitation code, no browser
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sys

from .environment import cli_name, product_name


def build_parser() -> argparse.ArgumentParser:
    cli = cli_name()
    p = argparse.ArgumentParser(
        prog=f"{cli} setup",
        description=(
            f"Get this computer ready to use {product_name()}: upgrade it, start your vault, "
            "point your shell at it, sign in to your node (the key goes straight into the vault), "
            "and connect the AI apps you have. Asks before each change. Run it again any time: "
            "it skips what is done and repairs what drifted."
        ),
        epilog=f"`{cli} doctor` runs the same checks and changes nothing.",
    )
    p.add_argument("--node", default="", help="your node's base address, e.g. https://node.example.org")
    p.add_argument("--site", default="", help="which site the key is for, if your account has several")
    p.add_argument("--yes", "-y", action="store_true", help="answer yes to every change (unattended)")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="show what would change and change nothing")
    p.add_argument("--invitation", action="store_true",
                   help="sign in with an invitation code instead of a browser (asked for, never echoed; "
                        "or piped on standard input)")
    p.add_argument("--only", action="append", default=[], metavar="STEP",
                   help="run only this step (repeatable): upgrade, vault, shell, node, key, mcp, "
                        "or one the product adds")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    return p


def _ctx():
    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skills import SkillContext

    from .skills import bind_default

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    return SkillContext(
        registry=bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.setup"),
        user_prompt=(lambda text: input(text)) if interactive else None,
    )


def _read_invitation() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Invitation code (not shown): ")
    return sys.stdin.readline()


def main(argv: list[str] | None = None) -> int:
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability

    from .detect import summary
    from .environment import resolve_env
    from .runner import render_checklist
    from .steps import Line

    args = build_parser().parse_args(argv)
    cli = cli_name()
    ctx = _ctx()
    seams: dict = {}
    if args.invitation:
        seams["invitation_code"] = _read_invitation().strip()
    params = {"node": args.node, "site": args.site, "yes": args.yes, "dry_run": args.dry_run,
              "only": args.only, "seams": seams}

    if not args.json:
        env = resolve_env(params)
        print(f"{cli} setup: getting this computer ready for {product_name()}")
        print()
        try:
            from .skills.run import detect

            found = detect(params, ctx).value or {}
            print(f"  Found  {summary(found)}")
        except Exception:  # noqa: BLE001 - the steps re-check what they need
            pass
        print(f"  Node   {env.node or '(none yet: pass --node)'}")
        print()
        if args.dry_run:
            print("Dry run: nothing will be changed.")

    result = invoke_capability(ctx.registry, "setup.run", params, ctx, surface=CLI_SURFACE)
    seams.pop("invitation_code", None)
    if not isinstance(result.value, dict):
        for err in result.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 2
    value = result.value
    if args.json:
        print(json.dumps(value, indent=2))
        return result.exit_code

    lines = [Line(**{k: v for k, v in ln.items()}) for ln in value["lines"]]
    print(render_checklist(lines, cli=cli))
    if value.get("resume"):
        # Dispatch leaves the verb's own arguments in sys.argv[1:].
        return _resume(cli, argv if argv is not None else sys.argv[1:])
    return result.exit_code


def _resume(cli: str, argv: list[str]) -> int:
    """Carry on with the steps after an upgrade, on the new code."""
    from .runner import RESUMED_ENV

    exe = shutil.which(cli)
    if not exe:
        print(f"\nUpgraded. Run `{cli} setup` again to finish on the new version.")
        return 0
    print("\nUpgraded. Continuing on the new version ...\n", flush=True)
    os.environ[RESUMED_ENV] = "1"
    os.execv(exe, [exe, "setup", *argv])  # noqa: S606 - our own entry point, same arguments
    return 0  # pragma: no cover - execv does not return


if __name__ == "__main__":
    raise SystemExit(main())
