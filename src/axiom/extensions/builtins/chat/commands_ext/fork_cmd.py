# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/fork``: branch this conversation into a copy and carry on in the copy; the original stays resumable."""

from __future__ import annotations

from ..command_registry import command
from ..follow_ups import FollowUp, result

USAGE = "\n  Usage: /fork [--at N] [title]\n"


def _fork(ctx, args: list[str]) -> str:
    at = None
    rest = list(args)
    if rest and rest[0] == "--at":
        try:
            at = int(rest[1])
        except (IndexError, ValueError):
            return USAGE
        rest = rest[2:]
    session = ctx.agent.session
    if not session.messages:
        return "\n  Nothing to fork yet: this conversation has no messages.\n"
    original = session.session_id
    ctx.store.save(session)  # the fork copies what is on disk, so write what is only in memory first
    try:
        fork = ctx.store.fork(
            original, title=" ".join(rest), at=at, principal=getattr(session, "principal_id", "") or ""
        )
    except ValueError as exc:
        return f"\n  {exc}\n"
    if fork is None:
        return "\n  Could not fork: the session was not found, or it belongs to another principal.\n"
    ctx.agent.session = fork
    return result(
        "/fork",
        f"\n  Forked into {fork.session_id} ({fork.title}), {len(fork.messages)} messages. "
        f"Now chatting in the fork.\n  The original is unchanged: /resume {original}\n",
        ("back", {"original": original}),
        "sessions",
    )


FOLLOW_UPS = {
    "back": FollowUp("/renew {original}", "go back to the original conversation; the fork stays saved"),
    "sessions": FollowUp("/sessions", "see both conversations in the saved list"),
}

command(
    "/fork",
    "Branch this conversation into a copy and continue there",
    "Session",
    usage="/fork [--at N] [title]",
    mutates=True,
    follow_ups=FOLLOW_UPS,
)(_fork)
