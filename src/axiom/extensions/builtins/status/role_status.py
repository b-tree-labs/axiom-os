# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``status`` on a node with a role: the role's own screen (ADR-164).

A node that declares its functions is asked "is it working?" in the terms of
those functions, not as a list of platform services it does not run. Products
answer for their roles through the ``axiom.node_status`` entry-point group: each
entry point resolves to a callable ``(cfg) -> list[section]`` or ``None`` when
the node is not one it knows. A section is::

    {"title": "Collector",
     "rows": [{"label": "running", "value": "yes, since 09:14", "state": "ok",
               "fix": ""}]}

``state`` is one of ``ok``, ``warn``, ``fail`` or ``info``. A ``fail`` or
``warn`` row should carry ``fix``: the command or step that clears it.
"""

from __future__ import annotations

from typing import Any

STATES = ("ok", "info", "warn", "fail")
_MARK = {"ok": "✓", "info": "·", "warn": "!", "fail": "✗"}


def collect(cfg) -> list[dict[str, Any]]:
    from importlib.metadata import entry_points

    sections: list[dict[str, Any]] = []
    for ep in entry_points(group="axiom.node_status"):
        try:
            provided = ep.load()(cfg)
        except Exception as exc:  # noqa: BLE001 — one provider never blanks the screen
            sections.append(
                {
                    "title": ep.name,
                    "rows": [
                        {
                            "label": "status",
                            "value": f"could not be read ({type(exc).__name__}: {exc})",
                            "state": "warn",
                            "fix": "",
                        }
                    ],
                }
            )
            continue
        for section in provided or ():
            sections.append(section)
    return sections


def worst(sections: list[dict[str, Any]]) -> str:
    rank = {s: i for i, s in enumerate(STATES)}
    seen = [row.get("state", "info") for sec in sections for row in sec.get("rows", ())]
    return max(seen, key=lambda s: rank.get(s, 1), default="info")


def render(cfg, sections: list[dict[str, Any]]) -> str:
    lines = [f"This node's role: {cfg.role or 'node'} ({', '.join(cfg.functions)})", ""]
    if not sections:
        lines.append("Nothing on this node reports status for that role yet.")
    for sec in sections:
        lines.append(sec.get("title", ""))
        rows = list(sec.get("rows", ()))
        width = max((len(str(r.get("label", ""))) for r in rows), default=0)
        for r in rows:
            mark = _MARK.get(r.get("state", "info"), "·")
            lines.append(f"  {mark} {str(r.get('label', '')):<{width}}  {r.get('value', '')}")
            if r.get("fix") and r.get("state") in ("warn", "fail"):
                lines.append(f"      fix: {r['fix']}")
        lines.append("")
    return "\n".join(lines).rstrip()


__all__ = ["STATES", "collect", "render", "worst"]
