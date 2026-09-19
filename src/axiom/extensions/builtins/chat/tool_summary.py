# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Describe the tools that are actually registered, for the system prompt.

The base prompt used to enumerate a fixed capability list — documents, signals,
read_file/list_files — written before extensions registered tools of their own.
The tool table is rebuilt every turn from every installed extension; the
sentence describing it was frozen, so the model was told it works on documents
no matter what was installed.

It behaved accordingly: asked to use a tool an extension had registered, it
replied that the tool "is not available in the current environment" while that
tool sat in the table being offered natively, and it reached for `list_files` —
a tool the stale text advertises — on a path it invented.

Generating this from the registry is what makes integration free for every
extension: registering a tool is the whole of it, including for a tool a site
ships in an extension of its own that the platform has never heard of.

The full definitions still go over the wire; this is orientation, not a schema.
So it stays to names and one short line each, and spends its remaining words on
the rule the model broke — that identifiers come from tool results, never from
recall.
"""

from __future__ import annotations

from typing import Any

#: Past this many tools the one-line descriptions are dropped and only names
#: are listed. Never the tools themselves: an assistant told an incomplete list
#: denies holding what it holds, which is the failure this exists to fix.
#: Descriptions are the expendable part — the full schemas are sent anyway.
_DESCRIBE_BELOW = 40


def _describe(tool: Any) -> str:
    text = getattr(tool, "description", "") or ""
    if not isinstance(text, str):
        return ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    return first[:100]


def describe_available_tools(all_tools: dict[str, Any]) -> str:
    """A system-prompt fragment naming the registered tools.

    Returns a non-empty string even with no tools: silence would leave the
    surrounding prompt to imply a capability set, which is the failure this
    replaces.
    """
    if not all_tools:
        return (
            "No tools are registered in this session. Say so plainly if asked "
            "to do something that would need one, rather than describing what "
            "you would have done."
        )

    names = sorted(all_tools)
    # Every name, always. An earlier version capped the list and sorted
    # alphabetically, so `telemetry_*` and a site's `vcu_*` fell off the end —
    # precisely the extension tools this is meant to announce.
    if len(names) <= _DESCRIBE_BELOW:
        lines = []
        for name in names:
            summary = _describe(all_tools[name])
            lines.append(f"- {name}" + (f" — {summary}" if summary else ""))
    else:
        lines = [", ".join(names)]

    return (
        "Tools available to you in this session (this list is generated from "
        "what is actually registered, and is authoritative — if something is "
        "not here, you do not have it):\n"
        + "\n".join(lines)
        + "\n\nUse them rather than describing what you would do. Never invent "
        "an identifier: metric names, file paths, model ids and the like come "
        "from a tool result, not from recall. If you need a name, call the "
        "tool that lists them first. Do not guess an argument that is not in a "
        "tool's schema."
    )


__all__ = ["describe_available_tools"]
