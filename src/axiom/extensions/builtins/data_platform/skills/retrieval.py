# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``data.retrieval`` — name a question once, ask it from anywhere.

The defect this closes was measured by walking a real node: the chart API takes
`t_from`/`t_to`, the served telemetry API takes `start`/`end`, and neither
rejects the other's spelling because an undeclared query parameter is ignored.
So a caller who brings the wrong one gets a confident answer to a different
question — I asked for September and received the whole record, 241,947 points,
and it looked exactly like September.

Retyping a retrieval per surface is where that mistake lives. These verbs let it
be typed once.

Five verbs, and the split between them matters:

``save`` / ``rm`` change the catalogue. ``list`` / ``show`` read it. ``dialect``
translates one retrieval into the query parameters a named surface actually
reads — which is the verb an agent wants, because it turns "what do I ask?" into
a fact rather than a guess.

None of them fetch data. A retrieval is a question, and keeping the asking
separate is what lets the same name feed a CSV, an agent and a figure.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..retrievals import DIALECTS, Catalog, NameTaken, Retrieval, as_query


def _catalog(params: dict[str, Any], ctx: SkillContext | None) -> Catalog:
    """Where the catalogue lives.

    Under the state dir, so a retrieval saved by the CLI is the one MCP reads.
    Two stores would mean an agent and a person disagreeing about what a name
    means, which is worse than having no catalogue.
    """
    explicit = params.get("catalog")
    if explicit:
        return Catalog(Path(str(explicit)).expanduser())
    state = getattr(ctx, "state_dir", None) if ctx else None
    root = Path(state).expanduser() if state else Path(os.path.expanduser("~/.axi"))
    return Catalog(root / "retrievals.json")


def _shown(r: Retrieval) -> dict[str, Any]:
    """A retrieval as a reader wants it, with the derived facts spelled out.

    `frozen` and `open_window` are computed, not stored, and a caller that has
    to work them out from the window string will work them out differently.
    """
    return {
        "name": r.name,
        "site": r.site,
        "feed": r.feed,
        "channels": list(r.channels),
        "window": r.window,
        "bucket": r.bucket,
        "note": r.note,
        "frozen": r.frozen,
        "open_window": r.open_window,
    }


def save(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """Name a retrieval, or replace one by name."""
    try:
        r = Retrieval(
            name=str(params.get("name") or ""),
            site=str(params.get("site") or ""),
            feed=str(params.get("feed") or ""),
            channels=tuple(params.get("channels") or ()),
            window=str(params.get("window") or ""),
            bucket=str(params.get("bucket") or ""),
            note=str(params.get("note") or ""),
        )
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    try:
        _catalog(params, ctx).save(r, replace=bool(params.get("replace")))
    except NameTaken as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    note = []
    if r.frozen:
        # Said at save time, not discovered later. A name like `yesterday` over
        # two fixed instants is a lie the day after it is written.
        note.append(
            f"{r.name!r} holds two instants, so it means that one period forever "
            "— a span like 24h or 7d stays true as the record grows"
        )
    return SkillResult(ok=True, value=_shown(r), actions_taken=[f"saved {r.name}", *note])


def list_saved(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    saved = _catalog(params, ctx).list()
    return SkillResult(
        ok=True,
        value={"retrievals": [_shown(r) for r in saved]},
        actions_taken=[f"{len(saved)} saved retrieval(s)"],
    )


def show(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    try:
        r = _catalog(params, ctx).get(str(params.get("name") or ""))
    except KeyError as exc:
        return SkillResult(ok=False, errors=[str(exc).strip("'")])
    return SkillResult(ok=True, value=_shown(r), actions_taken=[r.name])


def remove(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    name = str(params.get("name") or "")
    gone = _catalog(params, ctx).remove(name)
    if not gone:
        return SkillResult(ok=False, errors=[f"no retrieval named {name!r}"])
    return SkillResult(ok=True, value={"removed": name}, actions_taken=[f"removed {name}"])


def dialect(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """The query parameters a named surface actually reads, for one retrieval.

    The verb that earns the whole feature. It refuses an unknown dialect rather
    than guessing: handing a caller parameters a surface ignores is the exact
    failure this exists to end, and a plausible-looking answer would reproduce
    it one layer up.
    """
    try:
        r = _catalog(params, ctx).get(str(params.get("name") or ""))
    except KeyError as exc:
        return SkillResult(ok=False, errors=[str(exc).strip("'")])
    want = str(params.get("dialect") or "")
    try:
        query = as_query(r, dialect=want)
    except ValueError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    unresolved = [k for k in query if k.startswith("_")]
    actions = [f"{r.name} as {want}"]
    if unresolved:
        # A span the surface has no parameter for. Reported, never dropped —
        # emitting `last=24h` at a server that ignores it is how a caller
        # receives the whole record believing it asked for a day.
        actions.append(
            f"{want} has no parameter for {', '.join(unresolved)} — resolve it "
            "yourself; it is reported rather than sent so the server cannot "
            "silently ignore it"
        )
    return SkillResult(
        ok=True, value={"query": query, "dialects": list(DIALECTS)}, actions_taken=actions
    )


__all__ = ["dialect", "list_saved", "remove", "save", "show"]
