# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/sources``: what grounded the last reply, and whether its citations hold (ADR-168).

The agent has always computed both halves: the chunks the retriever supplied (``scope.last_retrieved``)
and the verdict of ``check_answer_provenance`` (``scope.last_provenance``). The verdict was recorded on
every turn and rendered nowhere. This reads them back, plus where the retrieval store lives
(``where._retrieval_where``), and states them in plain words.
"""

from __future__ import annotations

from typing import Any

from ..command_registry import CommandContext, McpForm, command
from ..follow_ups import FollowUp, result
from ..where import RetrievalWhere, where_report


def _store(r: RetrievalWhere) -> str:
    if r.kind == "remote-node":
        return f"a remote node ({r.host})" if r.host else "a remote node"
    if r.kind == "local":
        return "the local store"
    return "no retrieval store is configured"


def render_sources(scope: Any, retrieval: RetrievalWhere) -> str:
    chunks = list(getattr(scope, "last_retrieved", None) or [])
    finding = getattr(scope, "last_provenance", None)
    cited = set(getattr(finding, "cited", ()) or ())
    lines: list[str] = []
    if chunks:
        lines.append(f"Sources for the last reply (retrieved from {_store(retrieval)}):")
        for c in chunks:
            key = getattr(c, "citation_key", "?")
            title = getattr(c, "source_title", "") or getattr(c, "source_path", "")
            used = "cited" if key in cited else "retrieved, not cited"
            lines.append(f"  [{key}] {title}  ({getattr(c, 'source_path', '')})")
            lines.append(
                f"       corpus {getattr(c, 'corpus', '?')} · similarity "
                f"{getattr(c, 'similarity', 0.0):.2f} · {used}"
            )
    else:
        lines.append(f"No retrieval in this turn (the store is {_store(retrieval)}).")
    if finding is None:
        lines.append("Provenance: not checked this turn, so no verdict is available.")
    else:
        lines.append(f"Provenance: {finding.standing.value.upper()}. {finding.reason}.")
        if finding.unresolved:
            lines.append(f"  Citations with no source: {', '.join(finding.unresolved)}")
    return "\n".join(lines)


FOLLOW_UPS = {
    "rag_on": FollowUp("/rag on", "retrieval is off for this session; on grounds answers in documents"),
    "where": FollowUp("/where", "see where retrieval and the model run, and why nothing was retrieved"),
    "export": FollowUp("/export", "write the transcript out with its sources"),
    "ask_again": FollowUp("/sources", "re-check after the next reply"),
}


def sources_data(agent: Any) -> dict[str, Any]:
    """What grounded the last reply, as data: the shape ``axiom_chat__sources`` returns."""
    from ..agent import _active_scope

    scope = _active_scope(agent, None)
    retrieval = where_report(agent).retrieval
    finding = getattr(scope, "last_provenance", None)
    cited = set(getattr(finding, "cited", ()) or ())
    chunks = [
        {
            "key": getattr(c, "citation_key", "?"),
            "title": getattr(c, "source_title", "") or "",
            "path": getattr(c, "source_path", "") or "",
            "corpus": getattr(c, "corpus", "?"),
            "similarity": round(float(getattr(c, "similarity", 0.0) or 0.0), 4),
            "cited": getattr(c, "citation_key", "?") in cited,
        }
        for c in (getattr(scope, "last_retrieved", None) or [])
    ]
    return {
        "retrieval": {"kind": retrieval.kind, "host": retrieval.host, "store": _store(retrieval)},
        "sources": chunks,
        "provenance": None
        if finding is None
        else {
            "standing": finding.standing.value,
            "reason": finding.reason,
            "unresolved": list(getattr(finding, "unresolved", ()) or ()),
        },
    }


@command(
    "/sources", "Show what grounded the last reply and whether its citations hold", "Inspect",
    usage="/sources [--json]",
    follow_ups=FOLLOW_UPS,
    expose=frozenset({"chat", "cli", "mcp"}),
    mcp=(McpForm("chat.sources"),),
)
def sources_cmd(ctx: CommandContext, args: list[str]) -> str:
    from ..agent import _active_scope

    scope = _active_scope(ctx.agent, None)
    retrieval = where_report(ctx.agent).retrieval
    text = "\n" + render_sources(scope, retrieval) + "\n"
    if retrieval.kind == "off":
        return result("/sources", text, "rag_on", "where")
    if not getattr(scope, "last_retrieved", None):
        # Ungrounded: say plainly what would ground it, and where to look.
        return result("/sources", text, "where", "ask_again")
    return result("/sources", text, "where", "export")
