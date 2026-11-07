# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``next:`` lines: two or three commands worth running now, filled with real values.

A noun with no verb, a ``--help`` page and a result all end the same way, so
nobody has to open the docs to find the next command. One helper builds those
lines for every caller, from three things this computer already knows:

- the **command space** (:mod:`axiom.cli.command_space`): which verbs exist,
  which only read, and what each parameter holds;
- the **value cache** (:mod:`axiom.cli.completion_values`): the bound site's
  real roles, channels and days; and
- **recent history**: the noun and verb of commands run lately, and nothing
  else. Arguments are never recorded, because an argument can be a question
  typed to chat, a path or a name somebody would not want written down.

A suggestion is printed only when every value it needs is known. A verb whose
required argument has no known value is left out rather than shown with a
``<placeholder>`` the reader has no way to fill (no placeholder without a step
that produces it). Only verbs whose skill declares itself a read are suggested
on their own initiative: a ``next:`` line is something a person runs without
thinking, so it must never change anything.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from axiom.cli import command_space as cs

HISTORY_NAME = "command-history.json"
_HISTORY_MAX = 50
_TOKEN = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


# --------------------------------------------------------------------------- history


def history_path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "cache" / HISTORY_NAME


def _command_path(argv: Sequence[str]) -> list[str]:
    """The noun and verb of ``argv`` (``["data", "show"]``), never an argument."""
    out: list[str] = []
    for token in argv:
        if token.startswith("-") or not _TOKEN.match(token):
            break
        out.append(token)
        if len(out) == 2:
            break
    return out


def record(argv: Sequence[str], *, path: Path | None = None, now: float | None = None) -> None:
    """Remember that ``argv``'s noun and verb ran. Never raises; never records arguments."""
    try:
        cmd = _command_path(argv)
        if not cmd:
            return
        path = path or history_path()
        entries = history(path=path)
        entries = [e for e in entries if e["path"] != cmd]
        entries.insert(0, {"path": cmd, "at": round(now or time.time())})
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".command-history-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(entries[:_HISTORY_MAX], fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001 - remembering is a convenience
        return


def history(*, path: Path | None = None) -> list[dict[str, Any]]:
    try:
        with open(path or history_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in data if isinstance(data, list) else []:
        p = e.get("path") if isinstance(e, dict) else None
        if isinstance(p, list) and all(isinstance(t, str) and _TOKEN.match(t) for t in p):
            out.append({"path": list(p), "at": e.get("at") or 0})
    return out


# --------------------------------------------------------------------------- suggesting


def cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


def lookup_from(cache: Any) -> cs.ValueLookup:
    from axiom.cli.completion_values import values_for

    def lookup(kinds: tuple[str, ...], site: str | None) -> list[str]:
        return values_for(kinds, site=site, cache=cache, refresh=False)

    return lookup


def _verbs(node: cs.Node) -> Iterable[cs.Node]:
    """Every leaf-or-branch verb under ``node`` (one level of sub-verbs deep)."""
    for child in node.children.values():
        yield child
        for grand in child.children.values():
            yield grand


def suggest(
    path: Sequence[str] = (),
    *,
    space: cs.Node | None = None,
    cache: Any = None,
    recent: list[dict[str, Any]] | None = None,
    prefer: Sequence[str] = (),
    exclude: Sequence[Sequence[str]] = (),
    limit: int = 3,
    cli: str | None = None,
) -> list[str]:
    """Up to ``limit`` ready-to-run command lines for someone standing at ``path``.

    ``path`` is ``()`` for the top level, ``("data",)`` for a noun, or a verb's
    path (its siblings are suggested). ``prefer`` names verbs to put first when
    they can be filled. Nothing is returned that needs a value nobody knows.
    """
    from axiom.cli import completion_values

    path = tuple(path)
    cli = cli or cli_name()
    space = space if space is not None else cs.load(build=False)
    cache = cache if cache is not None else completion_values.load()
    recent = recent if recent is not None else history()
    lookup = lookup_from(cache)
    skip = {tuple(p) for p in exclude}

    node = space.find(path)
    if node is None:
        return []
    if not node.children and len(path) > 1:
        node = space.find(path[:-1]) or node
    pool = list(_verbs(node)) if node.path else [v for noun in node.children.values() for v in _verbs(noun)]
    recency = {tuple(e["path"]): i for i, e in enumerate(recent)}

    ranked: list[tuple[tuple[Any, ...], str]] = []
    for verb in pool:
        if verb.path in skip or verb.risk != "read" or (verb.children and not verb.args):
            continue
        example = cs.example_for(verb, lookup)
        if example is None:
            continue
        rel = verb.path[len(node.path):] if node.path else verb.path
        # The verb whose --help this is comes first: its own example is the answer.
        pref = -1 if verb.path == path else (
            prefer.index(rel[0]) if rel and rel[0] in prefer else len(prefer))
        key = (pref, recency.get(verb.path[:2], 99), -len(example.filled), len(verb.path), verb.path)
        ranked.append((key, example.line(cli)))
    ranked.sort(key=lambda kv: kv[0])
    lines: list[str] = []
    if not node.path and space.find(("explore",)) is not None:
        lines.append(f"{cli} explore")
    for _key, line in ranked:
        if line not in lines:
            lines.append(line)
    return lines[:limit]


def block(lines: Sequence[str]) -> str:
    """The one ``next:`` block that ends an answer, or empty when there is nothing to say."""
    lines = [ln for ln in lines if ln]
    if not lines:
        return ""
    return "\n\nnext:\n" + "\n".join(f"  {ln}" for ln in lines[:3])


def footer(path: Sequence[str] = (), **kw: Any) -> str:
    """``block(suggest(path))``, never raising: a hint is never why a command fails."""
    try:
        return block(suggest(path, **kw))
    except Exception:  # noqa: BLE001
        return ""


def help_path(subcommand: str, args: Sequence[str]) -> tuple[str, ...] | None:
    """The command path a ``--help`` request is about, or None when it is not one."""
    if "-h" not in args and "--help" not in args:
        return None
    return (subcommand, *_command_path([a for a in args if a not in ("-h", "--help")])[:1])


__all__ = [
    "HISTORY_NAME",
    "block",
    "footer",
    "help_path",
    "history",
    "history_path",
    "lookup_from",
    "record",
    "suggest",
]
