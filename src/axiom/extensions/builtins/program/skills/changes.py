# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.changes`` — what changed since *this* principal last looked.

The per-consumer read of phase 3. Each principal has a watermark — the
change-log position it was last reported up to — and this skill returns the
log entries after it: "what changed since I last looked." It answers with
the same link/field shape as ``status``, so a consumer sees owner, dates,
status, percent and links for each item a change touches, not a bare diff.

Read vs. advance — why a read tool stays a read
-----------------------------------------------
Reporting the deltas is a read. *Advancing* the watermark is a write. The two
are separated so the capability can be projected as a read-only MCP/HTTP tool
without lying:

- The capability declares ``side_effects=False`` → the MCP tool carries
  ``read_only_hint=true`` and HTTP serves it as a ``GET``. That reflects the
  **default**: a call advances nothing.
- On the operator's own ``cli`` surface the default flips to advance — "show
  me what's new and mark it seen" is the natural check-in — and ``--peek``
  suppresses it.
- On every served surface (MCP, web, chat, or an unknown surface) the default
  is peek. Advancing there happens only when the caller explicitly asks
  (``advance=true``), and the write it performs is the caller's *own* local
  reading position — never program state, never anything another consumer
  observes. The HTTP ``GET`` never forwards ``advance`` at all, so a GET is
  unconditionally a read.

``--since`` selects the window: ``last`` (the default) reads from this
principal's stored watermark; an ISO timestamp reads everything after that
instant, ignoring the stored mark for the read bound. Advancing, when it
happens, always catches the watermark up to the current end of the log.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from axiom.infra.skill_dispatch import CLI_SURFACE
from axiom.infra.skills import SkillContext, SkillResult

from ..model import (
    _PRINCIPAL_RE,  # noqa: PLC2701 — the one principal-shape rule, shared
    ProgramData,
    ProgramError,
    load_program,
)
from . import _changelog as cl
from ._source import BAD_REQUEST, default_data_path
from .status import _item_view

SINCE_LAST = "last"


def _refuse(kind: str, message: str) -> SkillResult:
    return SkillResult(ok=False, value={"refused": kind}, errors=[message])


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime | None:
    """Parse an ISO timestamp or date into an aware UTC datetime."""
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            day = date.fromisoformat(value.strip())
        except ValueError:
            return None
        return datetime(day.year, day.month, day.day, tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _should_advance(params: dict[str, Any], ctx: SkillContext) -> bool:
    if params.get("peek"):
        return False
    advance = params.get("advance")
    if advance is not None:
        return bool(advance)
    return getattr(ctx, "surface", None) == CLI_SURFACE


def _load_data(ctx: SkillContext) -> ProgramData | None:
    """The node's own data file, loaded leniently for enrichment only.

    Changes answer from the log, not the data file, so a missing or invalid
    data file does not refuse the read — it just means no program block and
    no current item views.
    """
    try:
        return load_program(default_data_path(ctx), require_listed_owners=False)
    except ProgramError:
        return None


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    principal = params.get("principal") or ctx.principal.handle
    if not isinstance(principal, str) or not _PRINCIPAL_RE.match(principal):
        return _refuse(
            BAD_REQUEST,
            f"{principal!r} is not a principal of the form @name or @name:context",
        )

    since = params.get("since") or SINCE_LAST
    entries = cl.read_changelog(cl.changelog_path(ctx))
    global_max_seq = entries[-1].get("seq", len(entries)) if entries else 0
    global_max_ts = entries[-1].get("ts") if entries else None

    if since == SINCE_LAST:
        mark = cl.read_watermark(cl.watermarks_path(ctx), principal)
        base_seq = mark.get("seq", 0) if isinstance(mark, dict) else 0
        if not isinstance(base_seq, int):
            base_seq = 0
        deltas = [e for e in entries if isinstance(e.get("seq"), int) and e["seq"] > base_seq]
        since_info = {
            "mode": "last",
            "seq": base_seq,
            "ts": mark.get("ts") if isinstance(mark, dict) else None,
        }
    else:
        since_dt = _parse_ts(since)
        if since_dt is None:
            return _refuse(
                BAD_REQUEST,
                f"--since must be {SINCE_LAST!r} or an ISO timestamp, got {since!r}",
            )
        deltas = []
        for entry in entries:
            entry_ts = entry.get("ts")
            parsed = _parse_ts(entry_ts) if isinstance(entry_ts, str) else None
            if parsed is not None and parsed > since_dt:
                deltas.append(entry)
        since_info = {"mode": "explicit", "seq": None, "ts": since}

    data = _load_data(ctx)
    items_by_id = {e.get("id"): e for e in data.schedule} if data is not None else {}

    shaped: list[dict[str, Any]] = []
    for entry in deltas:
        record = {
            "seq": entry.get("seq"),
            "ts": entry.get("ts"),
            "kind": entry.get("kind"),
            "subject_kind": entry.get("subject_kind"),
            "subject": entry.get("subject"),
            "field": entry.get("field"),
            "old": entry.get("old"),
            "new": entry.get("new"),
            "source": entry.get("source"),
        }
        if entry.get("subject_kind") == cl.SUBJECT_ITEM and entry.get("subject") in items_by_id:
            record["item"] = _item_view(items_by_id[entry["subject"]], data, "brief")
        else:
            record["item"] = None
        shaped.append(record)

    advanced = False
    watermark: dict[str, Any] | None = None
    if _should_advance(params, ctx) and entries:
        cl.advance_watermark(
            cl.watermarks_path(ctx),
            principal,
            seq=global_max_seq,
            ts=global_max_ts or _now_iso(),
            at=_now_iso(),
        )
        advanced = True
        watermark = {"seq": global_max_seq, "ts": global_max_ts}
    else:
        current = cl.read_watermark(cl.watermarks_path(ctx), principal)
        if isinstance(current, dict):
            watermark = {"seq": current.get("seq"), "ts": current.get("ts")}

    value: dict[str, Any] = {
        "principal": principal,
        "surface": getattr(ctx, "surface", None),
        "since": since_info,
        "advanced": advanced,
        "watermark": watermark,
        "changes": shaped,
        "count": len(shaped),
        "program": (
            {
                "id": data.program.get("id"),
                "name": data.program.get("name"),
                "as_of": data.program.get("as_of"),
            }
            if data is not None
            else None
        ),
    }
    return SkillResult(ok=True, value=value)
