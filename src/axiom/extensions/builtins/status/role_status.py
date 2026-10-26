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
#: For an output that cannot encode the marks: a Windows node whose output is
#: redirected (a file, a scheduled task) writes cp1252, and printing a mark it
#: cannot encode would crash the one screen meant to say what is wrong.
_ASCII_MARK = {"ok": "+", "info": "-", "warn": "!", "fail": "x"}


def _agents_section(cfg) -> dict[str, Any]:
    """Whether anything agent-like runs here, and the site policy that decides it."""
    try:
        from axiom.infra import node_functions as nf

        policy = cfg.agent_policy
        running = nf.background_services_allowed(cfg)
    except Exception:  # noqa: BLE001
        policy, running = "unknown", False
    if policy in ("none", "assist"):
        value = f"none running (site policy: {policy})"
        if policy == "assist":
            value += "; maintenance proposals come from UT and wait for your approval"
    elif running:
        value = "background upkeep may run (site policy: local)"
    else:
        value = "none running (site policy: local; turn on with features enable background-agents)"
    return {"title": "Agents", "rows": [{"label": "agents", "value": value, "state": "info", "fix": ""}]}


def collect(cfg) -> list[dict[str, Any]]:
    from importlib.metadata import entry_points

    sections: list[dict[str, Any]] = [_agents_section(cfg)]
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


def render(cfg, sections: list[dict[str, Any]], *, marks: dict[str, str] | None = None) -> str:
    lines = [f"This node's role: {cfg.role or 'node'} ({', '.join(cfg.functions)})", ""]
    if not sections:
        lines.append("Nothing on this node reports status for that role yet.")
    for sec in sections:
        lines.append(sec.get("title", ""))
        rows = list(sec.get("rows", ()))
        width = max((len(str(r.get("label", ""))) for r in rows), default=0)
        for r in rows:
            table = marks or _MARK
            mark = table.get(r.get("state", "info"), table["info"])
            lines.append(f"  {mark} {str(r.get('label', '')):<{width}}  {r.get('value', '')}")
            if r.get("fix") and r.get("state") in ("warn", "fail"):
                lines.append(f"      fix: {r['fix']}")
        lines.append("")
    return "\n".join(lines).rstrip()


def emit(cfg, sections: list[dict[str, Any]], stream=None) -> None:
    """Print the screen in marks the output can encode, and never crash on a value."""
    import sys

    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        "".join(_MARK.values()).encode(encoding)
        marks = None
    except (UnicodeEncodeError, LookupError):
        marks = _ASCII_MARK
    text = render(cfg, sections, marks=marks)
    # A value from a node (an error message, a path) can hold characters the
    # output cannot show either; replace those rather than fail.
    stream.write(text.encode(encoding, errors="replace").decode(encoding, errors="replace") + "\n")


__all__ = ["STATES", "collect", "emit", "render", "worst"]
