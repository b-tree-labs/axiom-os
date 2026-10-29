# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``library`` command: upload, ls, show, search, update, rm, restore.

Every verb is a thin projection of a ``library.*`` skill through
``invoke_capability``, so the authority gate, the action audit and the
capability telemetry see a CLI call exactly as they see an MCP one.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from axiom.infra.cli_format import Column, table
from axiom.infra.paths import get_user_state_dir
from axiom.infra.principal import resolve_principal
from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
from axiom.infra.skills import SkillContext

from . import skills as library_skills


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    cli = _brand_cli()
    p = argparse.ArgumentParser(
        prog=f"{cli} library",
        description="The document library: files you upload are classified, filed and searchable by content",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    up = sub.add_parser("upload", help="file one or more documents")
    up.add_argument("paths", nargs="+")
    up.add_argument("--private", action="store_true", help="only you can see it (default: your site)")
    up.add_argument("--title", default="", help="a display title (single file only)")
    up.add_argument("--tag", action="append", default=[], help="a tag (repeatable)")
    up.add_argument("--collection", default="")

    ls = sub.add_parser("ls", help="list documents you may see, newest first")
    ls.add_argument("q", nargs="?", default="")
    for flag in ("category", "collection", "tag", "visibility", "status"):
        ls.add_argument(f"--{flag}", default="")
    ls.add_argument("--deleted", action="store_true", help="your deleted documents")
    ls.add_argument("--limit", type=int, default=200)

    show = sub.add_parser("show", help="one document")
    show.add_argument("id")

    se = sub.add_parser("search", help="find documents by what they say")
    se.add_argument("q")
    se.add_argument("--limit", type=int, default=20)

    upd = sub.add_parser("update", help="change a document you filed")
    upd.add_argument("id")
    upd.add_argument("--title")
    upd.add_argument("--tags", help="comma-separated; replaces the tag list")
    upd.add_argument("--collection")
    upd.add_argument("--visibility", choices=("site", "private"))

    rm = sub.add_parser("rm", help="remove a document from retrieval and listings (restorable)")
    rm.add_argument("id")
    rs = sub.add_parser("restore", help="bring back a document you removed")
    rs.add_argument("id")

    for sp in (up, ls, show, se, upd, rm, rs):
        sp.add_argument("--json", action="store_true")
    return p


def _ctx() -> SkillContext:
    return SkillContext(
        registry=library_skills.bind_default(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("axi.library"),
        user_prompt=None,
        principal=resolve_principal(),
        surface=CLI_SURFACE,
    )


_COLUMNS = (
    Column("id"),
    Column("category"),
    Column("index"),
    Column("visibility"),
    Column("title", wrap=True),
)


def _cells(item: dict[str, Any]) -> list[str]:
    vis = "private" if item.get("visibility") == "private" else "site"
    return [
        str(item.get("id", ""))[:8],
        str(item.get("category", "")),
        str(item.get("index_status", "")),
        vis,
        str(item.get("title") or item.get("file_name") or ""),
    ]


def _print_items(items: list[dict[str, Any]]) -> None:
    for line in table([_cells(i) for i in items], _COLUMNS):
        print(line)


def _run(ctx: SkillContext, name: str, params: dict[str, Any]):
    return invoke_capability(ctx.registry, name, params, ctx, surface=CLI_SURFACE)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ctx = _ctx()
    out: list[Any] = []
    failed = False

    if args.verb == "upload":
        for path in args.paths:
            params: dict[str, Any] = {"path": path, "visibility": "private" if args.private else "site"}
            if args.title and len(args.paths) == 1:
                params["title"] = args.title
            if args.tag:
                params["tags"] = args.tag
            if args.collection:
                params["collection"] = args.collection
            result = _run(ctx, "library.upload", params)
            if not result.ok:
                failed = True
                for err in result.errors:
                    print(f"{path}: {err}", file=sys.stderr)
                continue
            out.append(result.value)
            if not args.json:
                for line in result.actions_taken or [f"filed {path}"]:
                    print(line)
    else:
        name, params = {
            "ls": ("library.list", lambda: {
                "q": args.q, "category": args.category, "collection": args.collection, "tag": args.tag,
                "visibility": args.visibility, "status": args.status, "deleted": args.deleted, "limit": args.limit,
            }),
            "show": ("library.show", lambda: {"id": args.id}),
            "search": ("library.search", lambda: {"q": args.q, "limit": args.limit}),
            "update": ("library.update", lambda: {
                k: v for k, v in {
                    "id": args.id, "title": args.title, "tags": args.tags,
                    "collection": args.collection, "visibility": args.visibility,
                }.items() if v is not None
            }),
            "rm": ("library.delete", lambda: {"id": args.id}),
            "restore": ("library.restore", lambda: {"id": args.id}),
        }[args.verb]
        result = _run(ctx, name, params())
        if not result.ok:
            for err in result.errors:
                print(err, file=sys.stderr)
            return 2
        value = result.value or {}
        if args.json:
            print(json.dumps(value, indent=2, default=str))
            return 0
        if args.verb == "ls":
            items = value.get("items", [])
            _print_items(items)
            print(f"{len(items)} document(s) at {value.get('site', '')}")
        elif args.verb == "search":
            for hit in value.get("results", []):
                _print_items([hit])
                snippet = " ".join(str(hit.get("snippet", "")).split())[:160]
                if snippet:
                    print(f"          {snippet}")
        else:
            _print_items([value])
            if args.verb == "rm":
                print("removed from retrieval and listings; `restore` brings it back")
        return 0

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    return 1 if failed else 0


__all__ = ["build_parser", "main"]
