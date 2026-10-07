# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi program`` — thin wrappers over the ``program.*`` skills (ADR-056).

Read verbs: ``status`` (the parameterized read), ``render`` (static page),
``validate`` (schema check), ``changes`` (per-consumer deltas), ``ownership``
(current owner + the timeline from the change log). Write verbs — the mutation
surface — are nested nouns: ``person add|edit|remove|reassign``,
``lane add|edit|remove``, ``item add|edit|remove|reassign``, plus ``invite`` /
``redeem``. ``sync`` reconciles the data file against its source.

Each leaf parser names the capability it dispatches (``set_defaults`` →
``capability``); the handler holds zero business logic — it translates flags
into a params dict and dispatches through ``invoke_capability``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from axiom.infra.branding import get_branding
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext, SkillResult

from .skills import bind_default
from .skills.sources import SOURCE_KINDS as _SOURCE_KINDS
from .skills.status import FORMATS as _FORMATS
from .skills.status import SCOPES as _SCOPES


def _add_data(p: argparse.ArgumentParser) -> None:
    p.add_argument("--data", help="path to the program data file (CLI only)")


def _add_json(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="emit the SkillResult as JSON")


def _reads(sub: argparse._SubParsersAction) -> None:
    p_status = sub.add_parser("status", help="items with owner, dates, status, pct, links")
    p_status.set_defaults(capability="program.status")
    p_status.add_argument("--scope", required=True, choices=_SCOPES)
    p_status.add_argument("--key", help="the principal, lane id, or item id the scope reads")
    p_status.add_argument("--fmt", default="brief", choices=_FORMATS)
    _add_data(p_status)
    _add_json(p_status)

    p_render = sub.add_parser("render", help="render the data file to one static status page")
    p_render.set_defaults(capability="program.render")
    _add_data(p_render)
    p_render.add_argument("--out", help="output directory for the page")
    _add_json(p_render)

    p_validate = sub.add_parser("validate", help="check the data file against its schema")
    p_validate.set_defaults(capability="program.validate")
    _add_data(p_validate)
    _add_json(p_validate)

    p_sync = sub.add_parser(
        "sync", help="reconcile the data file against its source, appending the change log"
    )
    p_sync.set_defaults(capability="program.sync")
    p_sync.add_argument("--data", help="path to the program data file (the reconcile target)")
    p_sync.add_argument("--source", help="path to an upstream source file (default: self)")
    p_sync.add_argument(
        "--source-kind",
        dest="source_kind",
        choices=_SOURCE_KINDS,
        help="which feeder to reconcile from: file (self), gitlab, github, or all "
        "(default: the data file's program.feeders, else self-reconcile)",
    )
    _add_json(p_sync)

    p_changes = sub.add_parser(
        "changes", help="what changed since a principal last looked (per-consumer watermark)"
    )
    p_changes.set_defaults(capability="program.changes")
    p_changes.add_argument("--principal", help="the caller's @name:context (default: this actor)")
    p_changes.add_argument(
        "--since", default="last", help="'last' (the watermark, default) or an ISO timestamp"
    )
    p_changes.add_argument(
        "--peek", action="store_true", default=argparse.SUPPRESS,
        help="report without advancing the watermark",
    )
    p_changes.add_argument(
        "--advance", action="store_true", default=argparse.SUPPRESS,
        help="advance the watermark (the CLI default; explicit elsewhere)",
    )
    _add_json(p_changes)

    p_own = sub.add_parser(
        "ownership", help="current owner + the ownership timeline from the change log"
    )
    p_own.set_defaults(capability="program.ownership")
    p_own.add_argument("--scope", required=True, choices=("item", "lane"))
    p_own.add_argument("--key", required=True, help="the item or lane id")
    _add_data(p_own)
    _add_json(p_own)


def _person(sub: argparse._SubParsersAction) -> None:
    person = sub.add_parser("person", help="add/edit/remove/reassign a program member")
    acts = person.add_subparsers(dest="action", required=True)

    add = acts.add_parser("add", help="add a member (principal + lane + role + accounts)")
    add.set_defaults(capability="program.person_add")
    add.add_argument("--principal", required=True, help="@name:context")
    add.add_argument("--lane", action="append", help="lane id (repeatable)")
    add.add_argument("--role", help="the member's program role")
    add.add_argument("--name", help="display name")
    add.add_argument("--drives", help="what the member drives")
    add.add_argument("--account", action="append", help="system=username (repeatable; empty = none)")
    _add_data(add)
    _add_json(add)

    edit = acts.add_parser("edit", help="edit a member's name / drives / accounts")
    edit.set_defaults(capability="program.person_edit")
    edit.add_argument("--principal", required=True, help="@name:context")
    edit.add_argument("--name", help="display name")
    edit.add_argument("--drives", help="what the member drives")
    edit.add_argument("--account", action="append", help="system=username (repeatable; empty = none)")
    _add_data(edit)
    _add_json(edit)

    remove = acts.add_parser("remove", help="remove a member")
    remove.set_defaults(capability="program.person_remove")
    remove.add_argument("--principal", required=True, help="@name:context")
    remove.add_argument("--reassign-to", dest="reassign_to", help="move their items to this member")
    _add_data(remove)
    _add_json(remove)

    reassign = acts.add_parser("reassign", help="change a member's lanes and/or role")
    reassign.set_defaults(capability="program.person_reassign")
    reassign.add_argument("--principal", required=True, help="@name:context")
    reassign.add_argument("--lane", action="append", help="new lane id (repeatable; replaces)")
    reassign.add_argument("--role", help="new program role")
    _add_data(reassign)
    _add_json(reassign)


def _lane(sub: argparse._SubParsersAction) -> None:
    lane = sub.add_parser("lane", help="add/edit/remove a lane")
    acts = lane.add_subparsers(dest="action", required=True)

    add = acts.add_parser("add", help="add a lane")
    add.set_defaults(capability="program.lane_add")
    add.add_argument("--id", required=True, help="lane id")
    add.add_argument("--name", help="lane name")
    add.add_argument("--lead", help="lane lead @name:context")
    add.add_argument("--color", help="lane color token")
    _add_data(add)
    _add_json(add)

    edit = acts.add_parser("edit", help="edit a lane's name/color or change its lead")
    edit.set_defaults(capability="program.lane_edit")
    edit.add_argument("--id", required=True, help="lane id")
    edit.add_argument("--name", help="lane name")
    edit.add_argument("--lead", help="new lane lead @name:context ('' clears it)")
    edit.add_argument("--color", help="lane color token")
    _add_data(edit)
    _add_json(edit)

    remove = acts.add_parser("remove", help="remove a lane")
    remove.set_defaults(capability="program.lane_remove")
    remove.add_argument("--id", required=True, help="lane id")
    remove.add_argument("--reassign-to", dest="reassign_to", help="move its items to this lane")
    _add_data(remove)
    _add_json(remove)


def _item(sub: argparse._SubParsersAction) -> None:
    item = sub.add_parser("item", help="add/edit/remove/reassign a schedule item")
    acts = item.add_subparsers(dest="action", required=True)

    add = acts.add_parser("add", help="add a schedule item")
    add.set_defaults(capability="program.item_add")
    add.add_argument("--id", required=True, help="item id")
    add.add_argument("--label", required=True, help="item label")
    add.add_argument("--owner", help="owner @name:context (must be a member)")
    add.add_argument("--lane", help="lane id")
    add.add_argument("--date", help="single ISO date (a milestone)")
    add.add_argument("--start", help="span start (ISO date)")
    add.add_argument("--end", help="span end (ISO date)")
    add.add_argument("--status", choices=("proposed", "committed"))
    add.add_argument("--pct", help="percent complete, 0..100")
    add.add_argument("--issue", help="tracker issue reference")
    _add_data(add)
    _add_json(add)

    edit = acts.add_parser("edit", help="edit a schedule item (not its owner)")
    edit.set_defaults(capability="program.item_edit")
    edit.add_argument("--id", required=True, help="item id")
    edit.add_argument("--label", help="item label")
    edit.add_argument("--lane", help="lane id")
    edit.add_argument("--date", help="single ISO date")
    edit.add_argument("--start", help="span start (ISO date)")
    edit.add_argument("--end", help="span end (ISO date)")
    edit.add_argument("--status", choices=("proposed", "committed"))
    edit.add_argument("--pct", help="percent complete, 0..100")
    edit.add_argument("--issue", help="tracker issue reference")
    _add_data(edit)
    _add_json(edit)

    remove = acts.add_parser("remove", help="remove a schedule item")
    remove.set_defaults(capability="program.item_remove")
    remove.add_argument("--id", required=True, help="item id")
    _add_data(remove)
    _add_json(remove)

    reassign = acts.add_parser("reassign", help="change an item's owner (applies the proxy rule)")
    reassign.set_defaults(capability="program.item_reassign")
    reassign.add_argument("--id", required=True, help="item id")
    reassign.add_argument("--owner", required=True, help="new owner @name:context (must be a member)")
    _add_data(reassign)
    _add_json(reassign)


def _invitation(sub: argparse._SubParsersAction) -> None:
    invite = sub.add_parser("invite", help="issue an invitation to join at a lane/role")
    invite.set_defaults(capability="program.invite")
    invite.add_argument("--principal", required=True, help="who the invitation is for (@name:context)")
    invite.add_argument("--lane", required=True, help="the lane they join")
    invite.add_argument("--role", help="the role they join as")
    invite.add_argument("--name", help="display name")
    invite.add_argument("--account", action="append", help="system=username (repeatable; empty = none)")
    invite.add_argument("--expires", help="lifetime, e.g. 90m / 12h / 7d (default 7d)")
    invite.add_argument("--invitations-file", dest="invitations_file", help="override the invitations file")
    _add_data(invite)
    _add_json(invite)

    redeem = sub.add_parser("redeem", help="redeem an invitation and record the membership")
    redeem.set_defaults(capability="program.redeem")
    redeem.add_argument("--code", required=True, help="the invitation code you were given")
    redeem.add_argument("--invitations-file", dest="invitations_file", help="override the invitations file")
    _add_data(redeem)
    _add_json(redeem)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{get_branding().cli_name} program",
        description="Program tracking: read, render, validate, sync, and edit the program data file.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)
    _reads(sub)
    _person(sub)
    _lane(sub)
    _item(sub)
    _invitation(sub)
    return parser


def _ctx() -> SkillContext:
    return SkillContext(
        registry=bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.program"),
        user_prompt=_terminal_prompt if sys.stdin.isatty() else None,
    )


def _terminal_prompt(prompt: str) -> str:
    return input(prompt)


def _emit(result: SkillResult, as_json: bool) -> int:
    if as_json:
        print(
            json.dumps({"ok": result.ok, "value": result.value, "errors": result.errors}, indent=2)
        )
        return result.exit_code
    if not result.ok:
        print("; ".join(result.errors), file=sys.stderr)
        return 1
    print(json.dumps(result.value, indent=2))
    return 0


_NOT_PARAMS = ("verb", "action", "json", "capability")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ctx = _ctx()
    capability = getattr(args, "capability", None) or f"program.{args.verb}"
    params = {
        key: value
        for key, value in vars(args).items()
        if key not in _NOT_PARAMS and value is not None
    }
    result = invoke_capability(
        ctx.registry,
        capability,
        params,
        ctx,
        surface=CLI_SURFACE,
    )
    return _emit(result, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
