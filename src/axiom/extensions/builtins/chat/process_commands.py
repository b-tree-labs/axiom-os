# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/ps`` and ``/stop`` for chat.

Both confine themselves to processes THIS session started (records whose
``session`` equals the chat session id). ``/ps`` only reads. ``/stop`` is the
operator typing the command for a process of their own session, so it acts
(TERM the group, grace, KILL); anything not owned by this session is refused
and pointed at ``<cli> process`` for a deliberate, audited decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from axiom.infra.process_containment import cli_name, terminate
from axiom.infra.process_registry import ProcessRegistry, probe

if TYPE_CHECKING:
    from .agent import ChatAgent


def _session_id(agent: ChatAgent) -> str:
    return str(getattr(getattr(agent, "session", None), "session_id", "") or "")


def cmd_ps(agent: ChatAgent) -> str:
    from axiom.infra.cli_format import Column, table

    cli = cli_name()
    sid = _session_id(agent)
    reg = ProcessRegistry()
    records = reg.records()
    mine = [r for r in records if sid and r.session == sid]
    others = len(records) - len(mine)
    lines = ["", "  Processes started by this session:"]
    if not mine:
        lines.append("    none")
    if mine:
        lines.extend(
            table(
                [(r.id, f"pid {r.pid}", r.kind, "running" if probe(r.pid) else "exited", r.purpose) for r in mine],
                [Column(), Column(), Column(), Column(), Column(wrap=True)],
                indent=4,
            )
        )
    if others:
        lines.append(f"  ({others} more owned by other sessions, agents or services)")
    lines += ["", "  Next:"]
    nexts = [("/stop <id>", "stop one of the above")] if mine else []
    nexts += [
        (f"{cli} process ls", "everything registered on this machine"),
        (f"{cli} process sweep", "look for rogue or runaway processes"),
    ]
    lines.extend(table(nexts, [Column(), Column(wrap=True)], indent=4))
    lines.append("")
    return "\n".join(lines)


def cmd_stop(agent: ChatAgent, args: list[str]) -> str:
    cli = cli_name()
    if not args:
        return "\n  Usage: /stop <id>   (see /ps for ids)\n  Next: /ps\n"
    sid = _session_id(agent)
    reg = ProcessRegistry()
    rec = reg.get(args[0])
    if rec is None:
        return f"\n  No unique process matches {args[0]!r}.\n  Next: /ps\n"
    if not sid or rec.session != sid:
        return (
            f"\n  {rec.id} was not started by this session, so /stop will not touch it.\n"
            f"  Next: {cli} process show {rec.id}   (decide deliberately from a shell)\n"
        )
    res = terminate(
        rec,
        state_dir=reg.state_dir,
        why=f"/stop from chat session {sid}",
        execute=True,
        grace_s=5.0,
        actor="chat-operator",
    )
    if res["action"] == "refused":
        return f"\n  Refused: {res['reason']}\n  Next: /ps   or   {cli} process sweep\n"
    return f"\n  {rec.id}: {res['action']}.\n  Next: /ps   or   {cli} process ls\n"
