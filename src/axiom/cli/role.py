# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi role` — manage user role membership.

Roles drive `axi help` filtering (per `prd-axi-cli.md §Progressive
Disclosure` and the role+intent design from 2026-05-03):

  axi role list                  # show available roles + activated intents
  axi role which                 # show this user's current roles
  axi role add <role>            # add a role
  axi role remove <role>         # remove a role
  axi role set <role> [<role>…]  # replace the role list

Roles persist to `~/.axi/competency.json`. The `axi config` interview
sets them initially; this CLI is for explicit changes after onboarding.
"""

from __future__ import annotations

import argparse
import sys

from axiom.cli.help_engine import (
    DEFAULT_ROLE,
    ROLE_INTENT_MAP,
    ROLES,
    UserCompetency,
    load_competency,
    save_competency,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi role",
        description=(
            "Manage user role membership.  Roles drive which commands "
            "`axi help` surfaces by default."
        ),
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("list", help="Show available roles and the intents each activates")
    sub.add_parser("which", help="Show this user's current role list")

    add_p = sub.add_parser("add", help="Add a role to the user's role list")
    add_p.add_argument("role", choices=ROLES)

    rm_p = sub.add_parser("remove", help="Remove a role from the user's role list")
    rm_p.add_argument("role", choices=ROLES)

    set_p = sub.add_parser("set", help="Replace the user's role list")
    set_p.add_argument("roles", nargs="+", choices=ROLES, metavar="ROLE")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        return 1

    handlers = {
        "list": _cmd_list,
        "which": _cmd_which,
        "add": _cmd_add,
        "remove": _cmd_remove,
        "set": _cmd_set,
    }
    return handlers[args.action](args)


def _cmd_list(_args: argparse.Namespace) -> int:
    print("Available roles:\n")
    width = max(len(r) for r in ROLES)
    for role in ROLES:
        intents = sorted(ROLE_INTENT_MAP.get(role, frozenset()))
        marker = " (default)" if role == DEFAULT_ROLE else ""
        print(f"  {role:<{width}}  intents: {', '.join(intents)}{marker}")
    print()
    print(
        "Add a role with `axi role add <role>`. "
        "Roles stack — `researcher` plus `instructor` activates the "
        "union of their intents."
    )
    return 0


def _cmd_which(_args: argparse.Namespace) -> int:
    c = load_competency()
    print(f"Current roles: {', '.join(c.roles)}")
    print(f"Activated intents: {', '.join(sorted(c.expand_intents()))}")
    print(f"Global competency tier: {c.global_tier}")
    if c.per_extension:
        print("Per-extension competency:")
        for ext, tier in sorted(c.per_extension.items()):
            print(f"  {ext}: {tier}")
    return 0


def _write_with_summary(updated: UserCompetency, action: str) -> int:
    path = save_competency(updated)
    print(f"✓ {action}")
    print(f"  Roles: {', '.join(updated.roles)}")
    print(f"  Intents activated: {', '.join(sorted(updated.expand_intents()))}")
    print(f"  Saved to: {path}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    c = load_competency()
    if args.role in c.roles:
        print(f"Role '{args.role}' already active. (Roles: {', '.join(c.roles)})")
        return 0
    # Adding a non-basic role IS a competency claim — the user is saying
    # "I want to see this surface."  Bump global tier `starter` → `core`
    # so the role's commands actually appear; without this, the user
    # adds a role and sees no change because most role verbs are at
    # `core` tier (which `starter` doesn't reach).
    new_tier = c.global_tier
    if args.role != DEFAULT_ROLE and c.global_tier == "starter":
        new_tier = "core"
    updated = UserCompetency(
        roles=tuple(c.roles) + (args.role,),
        global_tier=new_tier,
        per_extension=dict(c.per_extension),
    )
    msg = f"added role '{args.role}'"
    if new_tier != c.global_tier:
        msg += f" (and bumped tier {c.global_tier} → {new_tier} so the role's commands surface)"
    return _write_with_summary(updated, msg)


def _cmd_remove(args: argparse.Namespace) -> int:
    c = load_competency()
    if args.role not in c.roles:
        print(f"Role '{args.role}' not active. (Roles: {', '.join(c.roles)})")
        return 0
    new_roles = tuple(r for r in c.roles if r != args.role)
    if not new_roles:
        # Always keep at least `basic` so the user retains a usable surface.
        new_roles = (DEFAULT_ROLE,)
        print(f"  (kept '{DEFAULT_ROLE}' so you retain the universal surface)")
    updated = UserCompetency(
        roles=new_roles,
        global_tier=c.global_tier,
        per_extension=dict(c.per_extension),
    )
    return _write_with_summary(updated, f"removed role '{args.role}'")


def _cmd_set(args: argparse.Namespace) -> int:
    c = load_competency()
    new_roles = tuple(dict.fromkeys(args.roles))  # de-dupe, preserve order
    updated = UserCompetency(
        roles=new_roles,
        global_tier=c.global_tier,
        per_extension=dict(c.per_extension),
    )
    return _write_with_summary(updated, f"set roles to: {', '.join(new_roles)}")


if __name__ == "__main__":
    sys.exit(main())
