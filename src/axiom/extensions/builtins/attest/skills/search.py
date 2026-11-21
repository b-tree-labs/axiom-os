# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.search``: the one search over the log's index, for chat, MCP and the CLI (unit LD-2).

A thin skill over :func:`axiom.extensions.builtins.attest.index.search`, the same function the
Timeline's route and the Log page's search call. The door is a listing: each log is admitted by
the one check inside the search, a refused log is absent, and an unnamed caller or a policy
outage refuses the whole answer with the door's own wire answer.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import doors
from .. import index as log_index
from ._common import fail

#: A skill context's surface, as the door's transport.
_TRANSPORT = {"cli": "cli", "mcp": "mcp", "agent_tool": "agent_tool", "chat": "agent_tool"}


def run(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    query = str(params.get("query") or params.get("q") or "").strip()
    if not query:
        return fail("say what to search for: query is required")
    try:
        limit = int(params.get("limit") or 20)
    except (TypeError, ValueError):
        return fail("limit is a whole number")
    caller = doors.caller_from_ctx(ctx)
    transport = _TRANSPORT.get(str(getattr(ctx, "surface", None) or "cli"), "cli")
    try:
        value = log_index.search(
            caller, query, door_id="attest.search", transport=transport,
            site=params.get("site") or None, logbook=params.get("logbook") or None,
            since=params.get("since"), until=params.get("until"),
            entry_types=params.get("type") or params.get("entry_type"),
            signer=params.get("signer") or None, kinds=params.get("kind"),
            exclude_kinds=params.get("exclude_kind"), interval=params.get("interval") or None,
            limit=limit, state_dir=getattr(ctx, "state_dir", None),
        )  # fmt: skip
    except log_index.SearchRefused as refused:
        return doors._refusal(refused.decision)
    except ValueError as exc:
        return fail(str(exc))
    return SkillResult(ok=True, value=value)


__all__ = ["run"]
