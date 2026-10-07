# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Render where each piece of the system prompt came from.

The composer has tracked ``source`` per fragment since it was written, and
there was no way to look at it. Diagnosing two prompt defects today therefore
meant dumping 8,000 characters from a Python one-liner to find one stale
sentence and one extension claiming more than its own tools.

Provenance is the thing worth seeing: which layer, which fragment, who
contributed it, what it costs. A local override reports as ``local-override``,
so an experiment is never mistaken for shipped behaviour.
"""

from __future__ import annotations

from typing import Any, Iterable


def render_provenance(contributions: Iterable[Any]) -> str:
    """A grouped, readable table of prompt fragments and their sources."""
    rows = list(contributions)
    if not rows:
        return (
            "No prompt fragments composed. Nothing is contributing to the "
            "system prompt, which almost certainly means the prompt failed to "
            "build rather than that there is nothing to say."
        )

    by_layer: dict[str, list[Any]] = {}
    for row in rows:
        by_layer.setdefault(row.layer, []).append(row)

    total = sum(int(getattr(row, "tokens", 0) or 0) for row in rows)
    lines: list[str] = [f"System prompt — {len(rows)} fragments, ~{total} tokens", ""]
    for layer, items in by_layer.items():
        layer_tokens = sum(int(getattr(i, "tokens", 0) or 0) for i in items)
        lines.append(f"{layer}  (~{layer_tokens} tokens)")
        for item in items:
            tokens = int(getattr(item, "tokens", 0) or 0)
            marker = " *" if getattr(item, "source", "") == "local-override" else ""
            lines.append(
                f"    {item.name:<28} {tokens:>6}  {item.source}{marker}"
            )
        lines.append("")
    if any(getattr(r, "source", "") == "local-override" for r in rows):
        lines.append("* local override — not shipped behaviour")
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["render_provenance"]
